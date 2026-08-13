"""하루 요약을 만든다 (`daily_report`).

**`ProgramUsageRollup` 과 형제다.** 워터마크·하루 경계·`days_per_run` 상한은 전부
`_RollupBase` 의 것을 그대로 쓴다 — "부팅 시 마지막 생성일 확인 → 밀린 날짜 순차 처리
→ 데이터 없는 날 건너뛰기"를 새로 쓰면 같은 버그를 다시 만든다.

**둘의 차이는 무엇을 세느냐다.** `program_usage_daily` 는 *켜져 있던* 시간(이름 단위
구간 합집합)이고, 여기는 *앞에 놓여 있던* 시간(포어그라운드)이다. 게임을 켜 두고 자리를
비운 두 시간은 앞에서는 세지고 여기서는 세지 않는다.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..config.loader import RollupSettings, UsageSettings
from ..logging_setup import get_logger
from ..storage.hot import Database
from ..storage.rollup import _RollupBase, _day_key, _local_day_bounds, _union

log = get_logger(__name__)

DAILY_REPORT_COLUMNS = (
    "day", "total_s", "observed_s", "by_category", "top_apps", "by_slot", "built_at",
)

# Top 5. 늘리려면 config 로 빼야 하지만, 그 전에 "5 로 부족하다"는 실사용 근거가 있어야
# 한다 — 지금 늘리면 근거 없이 화면만 길어진다.
TOP_APPS = 5

# 분류에 없는 이름이 가는 곳. 카테고리 매핑이 비어 있어도 총 시간·Top 5·시간대는
# 그대로 나와야 하므로, 빠뜨린 이름을 버리지 않고 여기로 모은다.
OTHER = "기타"


def categorize(name: str, categories: dict[str, tuple[str, ...]]) -> str:
    """이름 → 카테고리. 없으면 `기타`.

    **역방향 dict 를 미리 만들지 않고 매번 훑는다.** 카테고리는 많아야 수십 개고 이
    함수는 하루 한 번, 이름 수십 개에만 불린다. 캐시를 두면 YAML 을 고쳤을 때 언제
    무효화되는지가 새 질문이 되는데, 그 비용이 훑는 비용보다 크다.
    """
    for category, names in categories.items():
        if name in names:
            return category
    return OTHER


def slot_of(hour: int, slots: dict[str, tuple[int, int]]) -> str | None:
    """로컬 시각(0~23) → 시간대 이름. 어느 구간에도 없으면 None.

    구간이 24시간을 다 덮지 않아도 되게 `None` 을 허용한다 — 사용자가 관심 있는
    시간대만 정의하는 것도 정당한 설정이다.
    """
    for label, (lo, hi) in slots.items():
        if lo <= hour < hi:
            return label
    return None


class DailyReportRollup(_RollupBase):
    """포어그라운드 시간을 하루 요약으로 접는다.

    **원본은 `process_metrics` 다** (2026-08-13 측정으로 정함). 설계 초안은 이미 접혀
    있는 `process_5m` 을 쓰려 했지만, 그 테이블은 `ProcessRollup(top_n=40)` 이 CPU 상위
    40 ∪ RSS 상위 40 만 남긴 것이라 **포어그라운드 여부가 선정에 들어가지 않는다.**
    실측(접힌 79버킷 안)에서 chrome·league of legends·discord 는 손실 0% 인데
    `windowsterminal` 은 11.6%, `rainbowsix_be` 는 90.1% 를 잃었다 — 무거운 프로그램은
    멀쩡하고 **가벼운 앱만 조용히 깎인다.** 터미널로 두 시간 작업한 날이 리포트에서
    사라지는 셈이라, 하필 이 리포트가 답하려는 질문에서 제일 나쁜 방향이다.

    그 대가로 원본이 지워지기 전에 접어야 한다 — `retention` 이 이 롤업의 워터마크로
    `process_metrics` 를 붙잡는다.

    **하루가 끝난 뒤에만 접는다.** 진행 중인 날을 접으면 부분값이 확정으로 남는다.
    """

    name = "daily_report"
    state_name = "daily_report"
    bucket_s = 86400
    source_table = "process_metrics"

    def __init__(self, db: Database, settings: RollupSettings, usage: UsageSettings) -> None:
        self.db = db
        self.settings = settings
        self.usage = usage
        self.interval_s = settings.daily_report_interval_s

    # ---------------------------------------------------------------- 관측 세션

    def _observed(self, day_lo: float, day_hi: float) -> float:
        """그날 Argus 가 관측한 시간(초). 리포트 비율의 분모다.

        **`ProgramUsageRollup._sessions` 와 같은 기준이어야 한다** — 두 화면이 "관측
        시간"을 다르게 말하면 사용자는 어느 쪽이 맞는지 알 수 없다. 그래서 계산을
        베끼지 않고 그 메서드를 그대로 부른다.
        """
        from ..storage.rollup import ProgramUsageRollup

        sessions = ProgramUsageRollup(self.db, self.settings)._sessions(day_lo, day_hi)
        window = _union([(max(s, day_lo), min(e, day_hi)) for s, e in sessions])
        return sum(e - s for s, e in window if e > s)

    # ------------------------------------------------------------------- 접기

    def _pending_days(self, now: float) -> list[str]:
        """접어야 할 날짜들. 오늘은 넣지 않는다 — 아직 끝나지 않았다."""
        from datetime import date, timedelta

        start_ts = self.watermark()
        if start_ts is None:
            rows = self.db.query("SELECT MIN(ts) AS lo FROM process_metrics")
            if not rows or rows[0]["lo"] is None:
                return []
            start_ts = float(rows[0]["lo"])

        cursor = date.fromisoformat(_day_key(start_ts))
        today = date.fromisoformat(_day_key(now))
        days: list[str] = []
        while cursor < today and len(days) < self.settings.daily_report_days_per_run:
            days.append(cursor.isoformat())
            cursor += timedelta(days=1)
        return days

    def _coverage(self, day_lo: float, day_hi: float, observed: float) -> float:
        """원본이 그날 관측 시간의 몇 할을 덮는가 (0~1).

        **잘려나간 날을 영구 저장하지 않기 위한 장치다.** `process_metrics` 는 하루면
        지워지는데 `daily_report` 는 영구 보존이라, 밀린 과거를 그대로 접으면 "그날
        0.8시간 썼다"는 거짓 요약이 굳는다 — 원본이 없으니 나중에 고칠 수도 없다.

        관측 세션(`_observed`)은 원본이 지워져도 줄지 않으므로(그건 `system_events` 가
        근거다) 둘의 비가 곧 원본의 온전함이다.
        """
        if observed <= 0:
            return 0.0
        rows = self.db.query(
            "SELECT DISTINCT ts FROM process_metrics WHERE ts >= ? AND ts < ? ORDER BY ts",
            (day_lo, day_hi),
        )
        ts = [float(r["ts"]) for r in rows]
        if len(ts) < 2:
            return 0.0
        cap = self.settings.daily_report_gap_cap_s
        covered = sum(min(ts[i + 1] - ts[i], cap) for i in range(len(ts) - 1))
        return covered / observed

    def _foreground_seconds(self, day_lo: float, day_hi: float) -> tuple[dict[str, float], dict[int, float]]:
        """(이름 → 초, 로컬 시각 → 초).

        **표본 수를 그대로 초로 세지 않는다.** 수집 주기는 흔들리고(스로틀이 걸리면
        ×10 까지 간다) 그러면 같은 1분이 6표본으로도 60표본으로도 남는다. 그래서 이웃한
        표본 사이의 **간격**을 더한다.

        간격에는 상한을 둔다 — 상한이 없으면 Argus 가 꺼져 있던 공백이 통째로
        사용시간이 된다(실측: 상한 없이 361.6시간, 실제 13.8시간). 상한값 자체는 결과를
        거의 바꾸지 않는다(간격의 98.6% 가 정확히 1.0초).
        """
        rows = self.db.query(
            "SELECT ts, name FROM process_metrics "
            "WHERE foreground = 1 AND name IS NOT NULL AND ts >= ? AND ts < ? "
            "ORDER BY ts",
            (day_lo, day_hi),
        )

        cap = self.settings.daily_report_gap_cap_s
        by_name: dict[str, float] = {}
        by_hour: dict[int, float] = {}

        # **같은 시각의 행이 여럿이어도 시간이 부풀지 않는다.** 같은 이름의 프로세스가
        # 여럿이면(크롬 탭) 한 순간에 행이 여러 개 남을 수 있는데, **간격을 더하는
        # 방식이라 그것들 사이의 간격이 0 이라 저절로 한 번만 세어진다.**
        #
        # 중복을 미리 걸러내는 코드를 따로 뒀다가 뺐다 — 값에 아무 영향이 없어서
        # 무력화해도 테스트가 조용했다(2026-08-13 mutation). 검증할 수 없는 방어는
        # 있으나 마나이고, 이 성질은 `test_one_moment_counts_once` 가 지킨다.
        seen = [(float(row["ts"]), row["name"]) for row in rows]

        from datetime import datetime

        for i, (ts, name) in enumerate(seen):
            # 마지막 표본은 다음 간격을 모른다. 상한만큼 세면 하루 끝에 없는 시간이
            # 붙으므로, 앞 간격과 같다고 보는 편이 안전하다 — 없으면 버린다.
            if i + 1 < len(seen):
                gap = min(seen[i + 1][0] - ts, cap)
            elif i > 0:
                gap = min(ts - seen[i - 1][0], cap)
            else:
                continue
            by_name[name] = by_name.get(name, 0.0) + gap
            hour = datetime.fromtimestamp(ts).hour
            by_hour[hour] = by_hour.get(hour, 0.0) + gap

        return by_name, by_hour

    def run_once(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        days = self._pending_days(now)
        if not days:
            return 0

        out: list[tuple[Any, ...]] = []
        for day in days:
            day_lo, day_hi = _local_day_bounds(day)
            observed = self._observed(day_lo, day_hi)
            if observed <= 0:
                continue  # 그날은 Argus 가 돌지 않았다. 행을 만들지 않는다

            coverage = self._coverage(day_lo, day_hi, observed)
            if coverage < self.settings.daily_report_min_coverage:
                # 원본이 이미 잘려 나갔다. **부분값으로 만든 요약은 영구히 남고 나중에
                # 고칠 수 없으므로**(원본이 없다) 아예 만들지 않는다. 조회 쪽이
                # "기록 없음"으로 표시한다.
                log.info(
                    "일일 리포트 건너뜀 — 원본이 부족하다",
                    extra={"day": day, "coverage": round(coverage, 3)},
                )
                continue

            by_name, by_hour = self._foreground_seconds(day_lo, day_hi)
            if not by_name:
                continue  # 관측은 했는데 포어그라운드가 하나도 없다(원격·잠금 상태)

            by_category: dict[str, float] = {}
            for name, seconds in by_name.items():
                key = categorize(name, self.usage.categories)
                by_category[key] = by_category.get(key, 0.0) + seconds

            by_slot: dict[str, float] = {}
            for hour, seconds in by_hour.items():
                label = slot_of(hour, self.usage.slots)
                if label is not None:
                    by_slot[label] = by_slot.get(label, 0.0) + seconds

            top = sorted(by_name.items(), key=lambda kv: -kv[1])[:TOP_APPS]
            out.append(
                (
                    day,
                    round(sum(by_name.values()), 1),
                    round(observed, 1),
                    json.dumps({k: round(v, 1) for k, v in by_category.items()}, ensure_ascii=False),
                    json.dumps(
                        [
                            {
                                "name": n,
                                "seconds": round(s, 1),
                                "category": categorize(n, self.usage.categories),
                            }
                            for n, s in top
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps({k: round(v, 1) for k, v in by_slot.items()}, ensure_ascii=False),
                    time.time(),
                )
            )

        if out:
            placeholders = ", ".join("?" * len(DAILY_REPORT_COLUMNS))
            with self.db._lock:  # noqa: SLF001
                self.db.conn.executemany(
                    f"INSERT OR REPLACE INTO daily_report "
                    f"({', '.join(DAILY_REPORT_COLUMNS)}) VALUES ({placeholders})",
                    out,
                )
                self.db.conn.commit()

        # **행을 만들지 못한 날도 워터마크는 넘긴다.** 그러지 않으면 Argus 가 꺼져
        # 있던 하루에서 영원히 멈춘다.
        self._set_watermark(_local_day_bounds(days[-1])[1])
        if out:
            log.debug("일일 리포트", extra={"days": len(out)})
        return len(out)


if __name__ == "__main__":  # 스모크: python -m argus.report.builder
    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="INFO")
    settings = load_settings()

    with Database() as db:
        rollup = DailyReportRollup(db, settings.rollup, settings.usage)
        before = db.query("SELECT COUNT(*) AS c FROM daily_report")[0]["c"]
        mark_before = rollup.watermark()
        pending = rollup._pending_days(time.time())
        started = time.perf_counter()
        made = rollup.run_once()
        elapsed = (time.perf_counter() - started) * 1000

        print(f"  밀린 날짜 : {len(pending)}")
        print(f"  접은 날짜 : {made} ({elapsed:.0f}ms)")
        print(f"  daily_report: {before} -> {db.query('SELECT COUNT(*) AS c FROM daily_report')[0]['c']}행")
        print(f"  워터마크  : {mark_before} -> {rollup.watermark()}")

        rows = db.query("SELECT * FROM daily_report ORDER BY day DESC LIMIT 3")
        for row in rows:
            total_h = row["total_s"] / 3600
            obs_h = row["observed_s"] / 3600
            print(f"  {row['day']}  포어그라운드 {total_h:5.1f}h / 관측 {obs_h:5.1f}h")
            print(f"    카테고리 {row['by_category']}")
            print(f"    시간대   {row['by_slot']}")

        # **"못 접었다"와 "접기를 거부했다"는 다르다.** 원본이 잘려 나간 날을 건너뛰는
        # 것은 설계된 동작이므로 실패가 아니다(그걸 [FAIL] 로 부르면 진짜 고장과
        # 구분되지 않는다). 원본이 없는 DB 도 마찬가지다. 여기서 볼 것은 밀린 날이
        # 있었는데 **워터마크가 제자리인 경우** 하나뿐이다 — 그러면 다음 틱도 같은
        # 날에서 멈춰 영영 진행하지 못한다.
        stuck = bool(pending) and rollup.watermark() == mark_before
        print("[FAIL] 밀린 날짜가 있는데 워터마크가 그대로다" if stuck else "[OK]")

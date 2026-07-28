"""착수 조건 판정 — "지금 시작해도 되는가"에 데이터가 답하게 한다.

`PLAN.md` 의 남은 작업 중 여럿이 "데이터가 며칠 쌓인 뒤"를 전제한다. 그런데 **날짜를
세는 것은 답이 아니다.** 하루 종일 게임만 한 3일과 유휴 위주의 3일은 같은 3일이 아니고,
사건 발생률도 사용 패턴에 따라 몇 배씩 흔들린다. 달력이 아니라 표본이 기준이어야 한다.

    .venv\\Scripts\\python.exe tools\\readiness.py

각 작업의 착수 조건을 실제 데이터에서 세어 [OK]/[대기] 로 찍는다. 부족하면 무엇이 얼마나
부족한지 말한다. **가상환경으로 실행해야 한다** — 웜 스토어를 읽는 데 DuckDB 가 필요하다.

원본(1초)은 24시간 창이라 며칠을 기다려도 늘지 않는다. 그래서 판정은 롤업 계층만 본다.
그 롤업이 **두 곳에 나뉘어 산다는 것**이 2026-07-29 에 문제가 됐다 — 이 도구가
`process_5m`(SQLite)만 세고 있었는데 이틀 지난 날짜는 Parquet 으로 옮겨지며 SQLite 에서
지워진다. 그래서 "3일 이상 관측된 프로세스"가 영원히 0종이었다. 지금은 두 계층을 합쳐
읽는 `argus.storage.history` 를 거친다.

**날짜를 세지 않는다 — 관측 시간을 센다.** 2시간 켜 둔 날과 12시간 켜 둔 날이 똑같이
"1일"이면 판정이 거짓말이 된다. 같은 이유로 프로세스 지문은 "며칠 보였나"가 아니라
"몇 버킷 쌓였나"까지 본다.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.paths import db_path  # noqa: E402
from argus.storage import history  # noqa: E402

# ---------------------------------------------------------------- 판정 기준
#
# 여기 숫자는 탐지 임계값이 아니라 **작업 착수 기준**이라 config 로 빼지 않는다.
# 사용자가 튜닝할 대상이 아니고, 근거가 바뀌면 숫자가 아니라 이 주석이 바뀌어야 한다.

# ② 알람 품질: 정해야 하는 것이 "warning 인데 점수 낮은 것"의 하한선이라, 판단 재료는
# 전체 사건 수가 아니라 warning 이상 표본 수다. 4건으로 선을 그으면 그 4건에 과적합된다.
MIN_ACTIONABLE_INCIDENTS = 10

# 하루만 보면 그날의 사용 패턴을 전체로 착각한다. 실측 첫날 사건 10건은 거의 전부 게임
# 세션에서 나왔다 — 유휴 위주의 날이 섞여야 "게임 아닌 날의 점수 분포"를 알 수 있다.
MIN_DISTINCT_DAY_KINDS = 2

# 하루 중 GPU 가 이만큼 돌아간 시간이 이 이상이면 '고부하 날'로 본다. 게임·렌더링을
# 구분하려는 것이 아니라 **부하 있는 날과 없는 날을 가르는 것**이 목적이라 느슨하게 잡는다.
BUSY_GPU_UTIL = 50.0
BUSY_MINUTES = 30

# 이만큼은 관측돼야 '하루'로 센다. Argus 는 PC 가 켜져 있을 때만 도므로 관측 시간은
# 사용 시간이고, 날마다 크게 다르다(실측 3일: 10.4h / 11.3h / 2.1h). 2시간짜리 날을
# 1일로 세면 "3일 관측"이 실질 하루치가 된다.
#
# 6시간인 이유: 하루의 1/4 이고, PC 사용은 시간대별로 성격이 갈리는데(업무 낮 · 게임 밤)
# 이 정도는 돼야 한 구간을 온전히 덮는다. 실측 분포에서 4~10시간 어디로 잡아도 판정이
# 같아 경계가 민감하지 않다 — 표본이 늘어 경계에 걸리는 날이 나오면 그때 다시 본다.
MIN_DAY_HOURS = 6.0

# Phase 6 프로세스 지문: p95/p99 를 세우려면 한 프로세스가 여러 날에 걸쳐 관측돼야 한다.
# 하루치로 만든 지문은 그날 하루의 습관이다.
FINGERPRINT_MIN_DAYS = 3
FINGERPRINT_MIN_PROCS = 15

# 날짜만으로는 부족하다. 3일에 걸쳐 보였어도 매번 5분씩이면 표본이 15개다.
# p99 를 세우려면 최소 100 표본은 있어야 하고, 5분 버킷으로 100개면 8.3시간이다.
FINGERPRINT_MIN_BUCKETS = 100

# Phase 4-B 레짐: GMM + HMM 이 요일 효과까지 잡으려면 주말과 평일이 모두 들어와야 한다.
REGIME_MIN_DAYS = 7


@dataclass
class Check:
    """조건 하나의 판정 결과."""

    label: str
    ok: bool
    detail: str


@dataclass
class Readiness:
    """한 작업의 착수 준비 상태."""

    name: str
    note: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _connect() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        print(f"[FAIL] DB 를 찾지 못했다: {path}", file=sys.stderr)
        raise SystemExit(1)
    # 상주 인스턴스가 쓰고 있으므로 읽기 전용으로만 연다.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class Day:
    """관측된 하루."""

    kind: str  # "고부하" | "유휴 위주"
    hours: float

    @property
    def counts(self) -> bool:
        """'며칠' 을 셀 때 이 날을 한 날로 칠지."""
        return self.hours >= MIN_DAY_HOURS


def _days() -> dict[str, Day]:
    """관측된 날짜별 성격과 관측 시간.

    고부하/유휴를 GPU 로 가르는 이유는 이 PC 에서 부하 있는 날을 가장 잘 가르는 신호이기
    때문이다. GPU 가 없는 PC 에서는 이 판정이 전부 '유휴'가 되는데, 그때는 사용 패턴
    다양성을 다른 축으로 봐야 한다 — 지금은 그 경우를 만나면 그렇게 말하고 넘어간다.

    세는 일은 저장소가 한다. 롤업 전 구간을 파이썬으로 올리면 데이터가 쌓일수록 느려지고,
    여기서 필요한 것은 날짜별 카운트뿐이다.
    """
    busy = history.busy_minutes("gpu_util_mean", BUSY_GPU_UTIL)
    return {
        day: Day("고부하" if busy.get(day, 0) >= BUSY_MINUTES else "유휴 위주", cov.hours)
        for day, cov in history.coverage("metrics").items()
    }


def _counted(days: dict[str, Day]) -> list[str]:
    return [d for d, v in days.items() if v.counts]


def _day_summary(days: dict[str, Day]) -> str:
    parts = []
    for d, v in days.items():
        mark = "" if v.counts else f", {MIN_DAY_HOURS:g}h 미만"
        parts.append(f"{d[5:]}({v.kind} {v.hours:.1f}h{mark})")
    return ", ".join(parts)


def _has_gpu() -> bool:
    return history.has_column_data("gpu_util_mean")


def check_alarm_quality(conn: sqlite3.Connection, days: dict[str, Day]) -> Readiness:
    """② 심각도-점수 게이트."""
    rows = conn.execute(
        "SELECT severity, peak_score FROM incidents WHERE severity IN ('warning','critical')"
    ).fetchall()
    scored = [r["peak_score"] for r in rows if r["peak_score"] is not None]

    # 사건은 그날 켜져 있던 시간에 비례해 나오므로, 짧은 날도 표본으로는 유효하다.
    # 여기서만 MIN_DAY_HOURS 를 적용하지 않는 이유다.
    distinct = sorted({v.kind for v in days.values()})
    result = Readiness(
        "② 알람 품질 — 심각도·점수 역전",
        "발송 게이트의 점수 하한을 정한다. 판단 재료는 warning 이상 표본이다.",
    )
    result.checks.append(
        Check(
            f"warning 이상 사건 {MIN_ACTIONABLE_INCIDENTS}건 이상",
            len(rows) >= MIN_ACTIONABLE_INCIDENTS,
            f"현재 {len(rows)}건"
            + (f" (점수 {min(scored):.2f}~{max(scored):.2f})" if scored else ""),
        )
    )
    result.checks.append(
        Check(
            "고부하 날과 유휴 위주 날이 모두 포함",
            len(distinct) >= MIN_DISTINCT_DAY_KINDS,
            f"관측 {len(days)}일: " + _day_summary(days),
        )
    )
    return result


def check_fingerprint(days: dict[str, Day]) -> Readiness:
    """Phase 6 프로세스 지문."""
    index = history.process_day_index()
    # 짧게만 켜 둔 날은 날짜로 치지 않는다 — 날을 세는 규칙은 한 곳이어야 한다.
    counted = set(_counted(days))

    def day_count(per_day: dict[str, int]) -> int:
        return len([d for d in per_day if d in counted])

    ready = [
        name
        for name, per_day in index.items()
        if day_count(per_day) >= FINGERPRINT_MIN_DAYS
        and sum(per_day.values()) >= FINGERPRINT_MIN_BUCKETS
    ]
    # 어느 조건에서 걸리는지 보여야 무엇을 더 기다릴지 안다.
    by_days = [n for n, p in index.items() if day_count(p) >= FINGERPRINT_MIN_DAYS]
    by_buckets = [n for n, p in index.items() if sum(p.values()) >= FINGERPRINT_MIN_BUCKETS]

    result = Readiness(
        "Phase 6 — 프로세스 지문",
        f"프로세스명별 p50/p95/p99 를 세운다. {FINGERPRINT_MIN_DAYS}일 이상 관측되고 "
        f"누적 {FINGERPRINT_MIN_BUCKETS}버킷 이상 쌓인 것만 지문이 된다.",
    )
    result.checks.append(
        Check(
            f"조건을 만족하는 프로세스 {FINGERPRINT_MIN_PROCS}종 이상",
            len(ready) >= FINGERPRINT_MIN_PROCS,
            f"현재 {len(ready)}종 / 전체 {len(index)}종 "
            f"(날짜 조건만: {len(by_days)}종, 표본 조건만: {len(by_buckets)}종)",
        )
    )
    return result


def check_regime(days: dict[str, Day]) -> Readiness:
    """Phase 4-B 레짐 추론."""
    counted = _counted(days)
    result = Readiness(
        "Phase 4-B — 레짐 추론",
        "GMM + HMM. 요일 효과를 잡으려면 주말과 평일이 모두 들어와야 한다.",
    )
    result.checks.append(
        Check(
            f"롤업 관측 {REGIME_MIN_DAYS}일 이상 ({MIN_DAY_HOURS:g}시간 이상 관측된 날만)",
            len(counted) >= REGIME_MIN_DAYS,
            f"현재 {len(counted)}일"
            + (f" (관측은 {len(days)}일)" if len(days) != len(counted) else ""),
        )
    )
    return result


def main() -> int:
    conn = _connect()
    try:
        days = _days()
        if not days:
            print("[대기] 롤업 데이터가 아직 없다. 상주 인스턴스가 도는지 먼저 확인할 것.")
            return 0

        if not _has_gpu():
            print("  참고: GPU 지표가 없어 '고부하/유휴' 구분이 성립하지 않는다.")
            print("        사용 패턴 다양성은 사람이 판단할 것.\n")

        reports = [
            check_alarm_quality(conn, days),
            check_fingerprint(days),
            check_regime(days),
        ]
    finally:
        conn.close()

    for report in reports:
        mark = "[OK]" if report.ok else "[대기]"
        print(f"{mark} {report.name}")
        print(f"      {report.note}")
        for check in report.checks:
            print(f"      {'v' if check.ok else 'x'} {check.label}")
            print(f"        └ {check.detail}")
        print()

    ready = [r.name for r in reports if r.ok]
    print("착수 가능:", ", ".join(ready) if ready else "없음 — 더 모을 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

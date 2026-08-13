"""보존 정책 — 오래된 원본 데이터를 지운다.

이게 없으면 DB 가 무한히 자란다. 1Hz 수집 × 프로세스 수십 개면 하루 수백만 행이다.

**VACUUM 은 하지 않는다.** 삭제로 비워진 페이지는 SQLite 가 재사용하므로 파일 크기는
일정 수준에서 안정된다. VACUUM 은 DB 전체를 다시 쓰는 무거운 작업이라, 우리가 관측
대상인 디스크에 큰 IO 를 만들어 스스로 이상을 유발한다.

**삭제는 롤업 워터마크를 넘지 못한다.** 원본이 1분 집계로 접히기 전에 지워지면 그
데이터는 영구히 사라진다 — 삭제는 되돌릴 수 없고, 장기 데이터는 이 경로로만 남는다.
롤업이 멈춰 있으면(예외·설정 오류) 삭제도 함께 멈추고 DB 가 커지는데, **그게 맞다.**
디스크가 차는 것은 눈에 보이고 고칠 수 있지만, 지워진 2주치는 되돌릴 방법이 없다.

**결함 주입 구간은 기한이 지나도 지우지 않는다.** 롤업은 이 원본을 대신하지 못한다 —
`process_5m` 은 이름 단위로 접혀 **`pid` 가 없고**, 귀인 채점은 정답을 PID 집합으로
매칭하므로 롤업만 남으면 채점 자체가 불가능해진다. 2026-07-29 에 이것이 드러났다:
주입 24건 중 22건이 `process_metrics` 보존(24시간)에 밀려 "구간에 프로세스 메트릭이
없음"으로 전부 채점에서 빠졌고, 남은 2건으로는 DoD 85% 를 판정할 수 없었다
(2건으로 나올 수 있는 값은 0·50·100% 뿐이다). **평가 세트가 스스로 증발하는 구조는
리플레이 하네스의 전제를 깬다.**
"""

from __future__ import annotations

import json
import time

from ..config.loader import RetentionSettings
from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from .hot import Database

log = get_logger(__name__)

# 결함 주입 구간을 보존해야 하는 테이블. 채점이 이것들을 **함께** 읽는다 —
# `process_metrics` 는 기여도를, `process_events` 는 정답 PID 트리(`descendants`)를,
# `metrics_raw`·`gpu_metrics` 는 병목 분류를 만든다. 하나만 빠져도 채점은 성립하지 않는다.
#
# 전역 지표 둘은 처음에 빠져 있었고, 그 결과가 2026-08-02 에 드러났다. 07-30 주입 배치의
# `process_metrics` 는 보호되어 그대로 남았는데 `metrics_raw` 는 지워져, `analyze_incident()`
# 가 병목을 관측하지 못하고 자원을 기본값으로 되돌렸다 — **제품 경로 귀인이 0/7 = 0%** 로
# 나왔다. 탐지가 퇴행한 것이 아니라 채점할 데이터의 절반이 없었다. 저장 당시의 사건 제목
# ("경합 — 컨텍스트 스위치 8.1σ")은 남아 있었으므로 그때는 분류가 되고 있었다.
#
# 보호 비용은 작다. 주입 구간은 12분짜리 몇 개뿐이고 이 둘은 1초 간격이라 구간당 수백 행이다.
FAULT_PROTECTED = ("process_metrics", "process_events", "metrics_raw", "gpu_metrics")


def _planned_duration_s(params: str | None) -> float:
    """주입기가 라벨에 남긴 계획 지속 시간. 없거나 못 읽으면 0."""
    if not params:
        return 0.0
    try:
        limits = (json.loads(params) or {}).get("limits") or {}
        return max(0.0, float(limits.get("duration_s") or 0.0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """겹치는 구간을 합친다. 주입은 연달아 돌리므로 여유(`fault_guard_s`)까지 더하면
    대부분 서로 겹친다 — 합치지 않으면 DELETE 문에 같은 뜻의 조건이 수십 개 붙는다."""
    if not windows:
        return []
    ordered = sorted(windows)
    out = [ordered[0]]
    for lo, hi in ordered[1:]:
        last_lo, last_hi = out[-1]
        if lo <= last_hi:
            out[-1] = (last_lo, max(last_hi, hi))
        else:
            out.append((lo, hi))
    return out


class Retention(Component):
    """주기적으로 보존 기한이 지난 행을 삭제한다."""

    name = "retention"

    def __init__(self, db: Database, settings: RetentionSettings) -> None:
        self.db = db
        self.settings = settings
        self.interval_s = settings.interval_s

    def _rules(self) -> list[tuple[str, float, tuple[str, ...]]]:
        """(테이블, 보존 초, 이 원본을 접는 롤업들의 이름) 목록.

        세 번째 값이 있는 테이블은 **그 롤업들이 전부** 접기 전에는 지우지 않는다.

        롤업 이름을 테이블마다 따로 적는 것이 중요하다. 처음에는 "롤업 워터마크"
        하나만 보게 해 뒀는데, 그 워터마크는 `metrics_1m` 것이었고 `process_metrics` 는
        1분 롤업이 접지 않는다. **접히지도 않은 채 "롤업이 지나갔으니 안전하다"는
        이유로 지워지고 있었다** — 보호 장치가 헛돌았다.

        **하나가 아니라 목록인 이유**는 `process_metrics` 를 읽는 롤업이 2026-08-13 에
        둘이 됐기 때문이다(`process_5m` · `daily_report`). 하나만 보면 다른 하나가
        접기 전에 원본이 사라진다 — 위와 정확히 같은 사고가 다시 난다.
        """
        s = self.settings
        return [
            ("metrics_raw", s.raw_hours * 3600, ("metrics_1m",)),
            ("gpu_metrics", s.raw_hours * 3600, ("metrics_1m",)),
            # 일일 리포트가 포어그라운드 시간을 여기서 센다. `process_5m` 은 상위 N 만
            # 남기므로 그걸로 대신할 수 없다(가벼운 앱이 최대 90% 깎인다 — 017 참조).
            ("process_metrics", s.process_hours * 3600, ("process_5m", "daily_report")),
            ("net_connections", s.network_hours * 3600, ("net_activity_5m",)),
            # 프로그램 사용시간 롤업이 이 원본을 접는다. 접기 전에 지워지면 그 날짜의
            # 사용시간은 **영구히 복원 불가능**하다 — 다른 테이블에 같은 정보가 없다
            # (`process_5m` 은 존재 여부만, 수명 합계는 이중계산이다).
            ("process_events", s.events_days * 86400, ("program_usage_daily",)),
            ("self_telemetry", s.self_telemetry_days * 86400, ()),
            # 시스템 사건은 양이 적고(하루 몇 건) 진단 가치가 커서 오래 남긴다.
            # 절전 공백 기록은 나중에 베이스라인이 그 구간을 제외하는 근거가 된다.
            ("system_events", s.events_days * 86400, ()),
        ]

    def _fault_windows(self, now: float) -> list[tuple[float, float]]:
        """지켜야 할 결함 주입 구간. 앞뒤로 `fault_guard_s` 만큼 넓힌다."""
        guard = float(getattr(self.settings, "fault_guard_s", 0.0) or 0.0)
        if guard <= 0:
            return []
        try:
            rows = self.db.query("SELECT ts_start, ts_end, params FROM fault_injections")
        except Exception:
            # 주입 테이블이 없는 DB(마이그레이션 이전). 지킬 구간도 없다.
            return []

        windows = []
        for row in rows:
            start = float(row["ts_start"])
            # **진행 중인 주입은 `ts_end` 가 아직 없다.** NULL 을 건너뛰면 지금 돌고 있는
            # 주입의 앞부분이 정리에 지워진다 — 12분짜리 주입 도중에 5분 주기가 두 번 돈다.
            #
            # 그러나 `now` 를 그대로 쓰면 **닫히지 못한 라벨의 구간이 영원히 자란다.**
            # 주입기는 종료 시 `finally` 에서 라벨을 닫지만 전원이 끊기면 그 코드가 돌지
            # 않는다 — 2026-07-30 16:25 에 실제로 그렇게 되어 #47 이 열린 채 남았고, 두
            # 시간 뒤 보호 구간이 2시간 24분으로 자라 그 구간의 정리가 통째로 멈췄다.
            # 행만 보고는 "지금 도는 중"과 "죽은 뒤 남은 것"을 구분할 수 없으므로,
            # **주입기가 라벨에 남긴 계획 길이를 상한으로 쓴다.** 계획을 넘겨 도는 주입은
            # 없다(길이가 곧 종료 조건이다). 계획이 없는 라벨(`manual`)은 끝이 열려
            # 있는 것이 의도이므로 기존대로 `now` 를 쓴다.
            if row["ts_end"] is not None:
                end = float(row["ts_end"])
            else:
                planned = _planned_duration_s(row["params"])
                end = min(now, start + planned) if planned else now
            windows.append((start - guard, end + guard))
        return _merge(windows)

    def _watermarks(self) -> dict[str, float]:
        """롤업별로 접기를 마친 시각."""
        try:
            rows = self.db.query("SELECT name, watermark_ts FROM rollup_state")
        except Exception:
            # 마이그레이션 이전 DB. 이 경우 원본을 지키는 쪽이 안전하다.
            return {}
        return {row["name"]: float(row["watermark_ts"]) for row in rows}

    def _delete_chunked(self, table: str, where: str, params: list[float]) -> int:
        """조건에 맞는 행을 **락을 놓아 가며** 지운다. 지운 총 행 수를 돌려준다.

        **`db._lock` 은 읽기·쓰기를 함께 덮는 전역 락이다.** DELETE 하나가 끝날 때까지
        놓지 않으면 그 시간이 그대로 수집 쓰기의 지연이 된다 — 실측 17일에서 10초 넘는
        쓰기 지연 17건 중 12건이 보존 정리·지문 갱신 근처였다.

        한 번에 지우면 락 보유가 **밀린 양에 비례한다.** 평소는 몇 초치라 짧지만, PC 를
        며칠 껐거나 롤업이 늦으면 수십만 행이 한 트랜잭션에 들어간다. 사본 실측:
        `net_connections` 606,320행을 한 번에 지우면 779ms, 2,000행씩 나누면 최악 113ms.
        나누는 쪽이 전체로는 조금 더 걸리지만(1,240ms) **그건 수집을 멈추지 않는 시간**이다.

        틱당 상한을 두는 이유: 큰 백로그를 한 틱에 다 지우려 하면 그 틱이 길어진다.
        남은 것은 다음 틱(기본 300초)이 가져간다 — 지우는 일은 급하지 않다.
        """
        chunk = int(getattr(self.settings, "delete_chunk_rows", 0) or 0)
        if chunk <= 0:
            # 0 은 "나누지 않는다"(옛 동작). 설정으로 되돌릴 수 있게 남긴다.
            with self.db._lock:  # noqa: SLF001 - 같은 커넥션을 쓰는 내부 협력
                cursor = self.db.conn.execute(f"DELETE FROM {table}{where}", tuple(params))
                self.db.conn.commit()
            return max(0, cursor.rowcount)

        max_rows = int(getattr(self.settings, "delete_max_rows_per_tick", 0) or 0)
        sql = (
            f"DELETE FROM {table} WHERE rowid IN "
            f"(SELECT rowid FROM {table}{where} LIMIT {chunk})"
        )
        total = 0
        while True:
            with self.db._lock:  # noqa: SLF001
                cursor = self.db.conn.execute(sql, tuple(params))
                self.db.conn.commit()
            removed = max(0, cursor.rowcount)
            total += removed
            if removed < chunk:
                break            # 더 지울 것이 없다
            if max_rows and total >= max_rows:
                log.debug(
                    "정리할 양이 많아 다음 틱으로 넘긴다",
                    extra={"table": table, "deleted": total},
                )
                break
        return total

    def purge_once(self) -> dict[str, int]:
        """한 번 정리하고 테이블별 삭제 행 수를 돌려준다."""
        now = time.time()
        deleted: dict[str, int] = {}
        watermarks = self._watermarks()
        fault_windows = self._fault_windows(now)

        waiting = [
            t for t, _, rollups in self._rules() if any(r not in watermarks for r in rollups)
        ]
        if waiting:
            # 첫 기동 직후에는 정상이지만, 계속 이 상태면 롤업이 죽은 것이고 DB 가 자란다.
            log.warning("롤업 워터마크가 없어 원본 정리를 건너뛴다", extra={"tables": waiting})

        for table, keep_seconds, rollups in self._rules():
            cutoff = now - keep_seconds
            if rollups:
                marks = [watermarks.get(r) for r in rollups]
                if any(m is None for m in marks):
                    continue  # 아직 접지 않은 롤업이 있다. 원본을 건드리지 않는다
                # **가장 뒤처진 롤업에 맞춘다.** 하나라도 아직 그 구간을 접지 않았으면
                # 지워서는 안 된다 — 앞선 롤업 기준으로 지우면 뒤처진 쪽은 영영 못 접는다.
                cutoff = min(cutoff, *[m for m in marks if m is not None])
            where = " WHERE ts < ?"
            params: list[float] = [cutoff]
            if table in FAULT_PROTECTED:
                for lo, hi in fault_windows:
                    where += " AND NOT (ts >= ? AND ts <= ?)"
                    params += [lo, hi]

            try:
                removed = self._delete_chunked(table, where, params)
                if removed > 0:
                    deleted[table] = removed
            except Exception:
                # 한 테이블 정리 실패가 나머지를 막지 않게 한다.
                log.exception("보존 정리 실패", extra={"table": table})
        if deleted:
            # 지킨 구간 수를 함께 남긴다. 이게 0 인데 주입이 있다면 보호가 헛돌고 있는
            # 것이고, 그건 채점 데이터가 조용히 사라지는 중이라는 뜻이다.
            log.info(
                "보존 정리",
                extra={
                    "deleted": deleted,
                    "db_bytes": self.db.size_bytes(),
                    "fault_windows_held": len(fault_windows),
                },
            )
        return deleted

    def tick(self) -> None:
        self.purge_once()


if __name__ == "__main__":  # 스모크: python -m argus.storage.retention
    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="INFO")
    with Database() as db:
        retention = Retention(db, load_settings().retention)
        old_ts = time.time() - 400 * 86400  # 확실히 기한이 지난 시각

        # 1) 롤업이 아직 그 구간을 접지 않았으면 지우면 안 된다.
        db.insert_many("metrics_raw", ("ts", "cpu_total"), [(old_ts, 1.0)])
        saved = db.query("SELECT watermark_ts FROM rollup_state WHERE name='metrics_1m'")
        with db._lock:  # noqa: SLF001
            db.conn.execute(
                "INSERT INTO rollup_state (name, watermark_ts, updated_at) "
                "VALUES ('metrics_1m', ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "watermark_ts=excluded.watermark_ts",
                (old_ts - 1, time.time()),
            )
            db.conn.commit()
        retention.purge_once()
        held = db.query("SELECT COUNT(*) AS c FROM metrics_raw WHERE ts < ?", (old_ts + 1,))[0]["c"]
        print(f"  워터마크 이전(미집계) 행 보존: {held}행 남음")

        # 2) 롤업이 지나간 뒤에는 지운다.
        with db._lock:  # noqa: SLF001
            db.conn.execute(
                "UPDATE rollup_state SET watermark_ts=? WHERE name='metrics_1m'", (time.time(),)
            )
            db.conn.commit()
        deleted = retention.purge_once()
        after = db.query("SELECT COUNT(*) AS c FROM metrics_raw WHERE ts < ?", (old_ts + 1,))[0]["c"]

        # 실제 워터마크를 되돌려 놓는다. 스모크가 운영 상태를 바꾸면 안 된다 —
        # 워터마크를 현재 시각으로 남기고 나가면 그 뒤로 롤업이 과거를 영영 건너뛴다.
        with db._lock:  # noqa: SLF001
            if saved:
                db.conn.execute(
                    "UPDATE rollup_state SET watermark_ts=? WHERE name='metrics_1m'",
                    (saved[0]["watermark_ts"],),
                )
            else:
                db.conn.execute("DELETE FROM rollup_state WHERE name='metrics_1m'")
            db.conn.commit()

        # 3) 결함 주입 구간은 기한이 지나고 롤업이 끝났어도 지킨다.
        #    **두 행의 조건을 똑같이 맞추는 것이 이 검증의 전부다.** 기한·워터마크·테이블이
        #    모두 같고 다른 것은 "주입 구간 안이냐"뿐이어야, 보호 절을 지웠을 때 실제로
        #    실패한다. 한쪽만 조건이 달랐다면 보호가 없어도 통과했을 것이다.
        fault_start, fault_end = old_ts - 300, old_ts + 300
        inside, outside = old_ts, old_ts - 100_000  # 바깥은 guard(900초) 밖이다
        # 주입 구간 밖이지만 여유 안. **`fault_guard_s` 가 0 이 되면 이 행만 조용히
        # 사라진다** — 구간은 지켜지는데 비교 창(주입 150초 전)이 없어져 채점이 틀어지고,
        # 위 두 행만 보는 검증은 그걸 통과시킨다.
        in_guard = fault_start - 600
        with db._lock:  # noqa: SLF001
            saved_proc = db.conn.execute(
                "SELECT watermark_ts FROM rollup_state WHERE name='process_5m'"
            ).fetchone()
            fault_row = db.conn.execute(
                "INSERT INTO fault_injections (scenario, ts_start, ts_end, completed) "
                "VALUES ('__smoke__', ?, ?, 1)",
                (fault_start, fault_end),
            ).lastrowid
            db.conn.execute(
                "INSERT INTO rollup_state (name, watermark_ts, updated_at) "
                "VALUES ('process_5m', ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "watermark_ts=excluded.watermark_ts",
                (time.time(), time.time()),
            )
            db.conn.commit()
        db.insert_many(
            "process_metrics", ("ts", "pid", "name"),
            [
                (inside, 999_001, "__smoke__"),
                (outside, 999_002, "__smoke__"),
                (in_guard, 999_003, "__smoke__"),
            ],
        )

        retention.purge_once()
        kept = db.query(
            "SELECT COUNT(*) AS c FROM process_metrics WHERE name='__smoke__' AND ts=?", (inside,)
        )[0]["c"]
        purged = db.query(
            "SELECT COUNT(*) AS c FROM process_metrics WHERE name='__smoke__' AND ts=?", (outside,)
        )[0]["c"]
        guarded = db.query(
            "SELECT COUNT(*) AS c FROM process_metrics WHERE name='__smoke__' AND ts=?", (in_guard,)
        )[0]["c"]

        with db._lock:  # noqa: SLF001
            db.conn.execute("DELETE FROM process_metrics WHERE name='__smoke__'")
            db.conn.execute("DELETE FROM fault_injections WHERE id=?", (fault_row,))
            if saved_proc:
                db.conn.execute(
                    "UPDATE rollup_state SET watermark_ts=? WHERE name='process_5m'",
                    (saved_proc["watermark_ts"],),
                )
            else:
                db.conn.execute("DELETE FROM rollup_state WHERE name='process_5m'")
            db.conn.commit()

        print(
            f"  주입 구간 안 보존: {kept}행 / 여유 안 보존: {guarded}행 / "
            f"구간 밖 삭제됨: {'예' if not purged else '아니오'}"
        )

        # 4) 진행 중인 주입(`ts_end` 가 아직 NULL)도 구간으로 잡아야 한다. 12분짜리 주입
        #    도중에 정리가 두 번 돌기 때문에, 여기서 NULL 을 건너뛰면 방금 만든 표본의
        #    앞부분이 지워진다. DB 를 건드리지 않고 구간 계산만 확인한다.
        now = time.time()
        with db._lock:  # noqa: SLF001
            running = db.conn.execute(
                "INSERT INTO fault_injections (scenario, ts_start, completed) "
                "VALUES ('__smoke_running__', ?, 0)",
                (now - 120,),
            ).lastrowid
            db.conn.commit()
        windows = retention._fault_windows(now)  # noqa: SLF001
        covers_running = any(lo <= now - 120 and hi >= now for lo, hi in windows)
        with db._lock:  # noqa: SLF001
            db.conn.execute("DELETE FROM fault_injections WHERE id=?", (running,))
            db.conn.commit()
        print(f"  진행 중 주입 구간 인식: {'예' if covers_running else '아니오'}")

        print(f"  삭제 내역: {deleted or '(없음)'}")
        print(f"  DB 크기: {db.size_bytes():,} bytes")
        if kept != 1:
            print("[FAIL] 결함 주입 구간이 삭제됐다 — 귀인 채점의 근거가 사라지는 경로다")
            raise SystemExit(1)
        if not covers_running:
            print("[FAIL] 진행 중인 주입이 구간으로 잡히지 않는다 — 주입 도중 표본이 지워진다")
            raise SystemExit(1)
        if guarded != 1:
            print("[FAIL] 주입 구간 앞 여유가 지켜지지 않았다 — 채점의 비교 창이 사라진다")
            raise SystemExit(1)
        if purged != 0:
            print("[FAIL] 주입 구간 밖의 만료 행이 남았다 — 이 검증은 보호를 확인하지 못한다")
            raise SystemExit(1)
        if held != 1:
            print("[FAIL] 롤업 전 원본이 삭제됐다 — 장기 데이터가 영구 손실되는 경로다")
            raise SystemExit(1)
        if after != 0:
            print("[FAIL] 기한이 지나고 롤업도 끝난 행이 남아 있다")
            raise SystemExit(1)
    print("[OK] storage.retention")

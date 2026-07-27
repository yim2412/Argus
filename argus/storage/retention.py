"""보존 정책 — 오래된 원본 데이터를 지운다.

이게 없으면 DB 가 무한히 자란다. 1Hz 수집 × 프로세스 수십 개면 하루 수백만 행이다.

**VACUUM 은 하지 않는다.** 삭제로 비워진 페이지는 SQLite 가 재사용하므로 파일 크기는
일정 수준에서 안정된다. VACUUM 은 DB 전체를 다시 쓰는 무거운 작업이라, 우리가 관측
대상인 디스크에 큰 IO 를 만들어 스스로 이상을 유발한다.

**삭제는 롤업 워터마크를 넘지 못한다.** 원본이 1분 집계로 접히기 전에 지워지면 그
데이터는 영구히 사라진다 — 삭제는 되돌릴 수 없고, 장기 데이터는 이 경로로만 남는다.
롤업이 멈춰 있으면(예외·설정 오류) 삭제도 함께 멈추고 DB 가 커지는데, **그게 맞다.**
디스크가 차는 것은 눈에 보이고 고칠 수 있지만, 지워진 2주치는 되돌릴 방법이 없다.
"""

from __future__ import annotations

import time

from ..config.loader import RetentionSettings
from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from .hot import Database

log = get_logger(__name__)


class Retention(Component):
    """주기적으로 보존 기한이 지난 행을 삭제한다."""

    name = "retention"

    def __init__(self, db: Database, settings: RetentionSettings) -> None:
        self.db = db
        self.settings = settings
        self.interval_s = settings.interval_s

    def _rules(self) -> list[tuple[str, float, str | None]]:
        """(테이블, 보존 초, 이 원본을 접는 롤업의 이름) 목록.

        세 번째 값이 있는 테이블은 **그 롤업이** 접기 전에는 지우지 않는다.

        롤업 이름을 테이블마다 따로 적는 것이 중요하다. 처음에는 "롤업 워터마크"
        하나만 보게 해 뒀는데, 그 워터마크는 `metrics_1m` 것이었고 `process_metrics` 는
        1분 롤업이 접지 않는다. **접히지도 않은 채 "롤업이 지나갔으니 안전하다"는
        이유로 지워지고 있었다** — 보호 장치가 헛돌았다.
        """
        s = self.settings
        return [
            ("metrics_raw", s.raw_hours * 3600, "metrics_1m"),
            ("gpu_metrics", s.raw_hours * 3600, "metrics_1m"),
            ("process_metrics", s.process_hours * 3600, "process_5m"),
            ("net_connections", s.network_hours * 3600, None),
            ("process_events", s.events_days * 86400, None),
            ("self_telemetry", s.self_telemetry_days * 86400, None),
            # 시스템 사건은 양이 적고(하루 몇 건) 진단 가치가 커서 오래 남긴다.
            # 절전 공백 기록은 나중에 베이스라인이 그 구간을 제외하는 근거가 된다.
            ("system_events", s.events_days * 86400, None),
        ]

    def _watermarks(self) -> dict[str, float]:
        """롤업별로 접기를 마친 시각."""
        try:
            rows = self.db.query("SELECT name, watermark_ts FROM rollup_state")
        except Exception:
            # 마이그레이션 이전 DB. 이 경우 원본을 지키는 쪽이 안전하다.
            return {}
        return {row["name"]: float(row["watermark_ts"]) for row in rows}

    def purge_once(self) -> dict[str, int]:
        """한 번 정리하고 테이블별 삭제 행 수를 돌려준다."""
        now = time.time()
        deleted: dict[str, int] = {}
        watermarks = self._watermarks()

        waiting = [t for t, _, rollup in self._rules() if rollup and rollup not in watermarks]
        if waiting:
            # 첫 기동 직후에는 정상이지만, 계속 이 상태면 롤업이 죽은 것이고 DB 가 자란다.
            log.warning("롤업 워터마크가 없어 원본 정리를 건너뛴다", extra={"tables": waiting})

        for table, keep_seconds, rollup in self._rules():
            cutoff = now - keep_seconds
            if rollup is not None:
                watermark = watermarks.get(rollup)
                if watermark is None:
                    continue
                cutoff = min(cutoff, watermark)
            try:
                with self.db._lock:  # noqa: SLF001 - 같은 커넥션을 쓰는 내부 협력
                    cursor = self.db.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                    self.db.conn.commit()
                if cursor.rowcount > 0:
                    deleted[table] = cursor.rowcount
            except Exception:
                # 한 테이블 정리 실패가 나머지를 막지 않게 한다.
                log.exception("보존 정리 실패", extra={"table": table})
        if deleted:
            log.info("보존 정리", extra={"deleted": deleted, "db_bytes": self.db.size_bytes()})
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

        print(f"  삭제 내역: {deleted or '(없음)'}")
        print(f"  DB 크기: {db.size_bytes():,} bytes")
        if held != 1:
            print("[FAIL] 롤업 전 원본이 삭제됐다 — 장기 데이터가 영구 손실되는 경로다")
            raise SystemExit(1)
        if after != 0:
            print("[FAIL] 기한이 지나고 롤업도 끝난 행이 남아 있다")
            raise SystemExit(1)
    print("[OK] storage.retention")

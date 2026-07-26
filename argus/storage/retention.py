"""보존 정책 — 오래된 원본 데이터를 지운다.

이게 없으면 DB 가 무한히 자란다. 1Hz 수집 × 프로세스 수십 개면 하루 수백만 행이다.

**VACUUM 은 하지 않는다.** 삭제로 비워진 페이지는 SQLite 가 재사용하므로 파일 크기는
일정 수준에서 안정된다. VACUUM 은 DB 전체를 다시 쓰는 무거운 작업이라, 우리가 관측
대상인 디스크에 큰 IO 를 만들어 스스로 이상을 유발한다.

Phase 1 은 "오래되면 삭제"까지만 한다. 1분 집계로 다운샘플해 장기 보존하는 것은
웜 스토어(Parquet)와 함께 이후 단계에서 붙인다.
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

    def _rules(self) -> list[tuple[str, float]]:
        """(테이블, 보존 초) 목록."""
        s = self.settings
        return [
            ("metrics_raw", s.raw_hours * 3600),
            ("gpu_metrics", s.raw_hours * 3600),
            ("process_metrics", s.process_hours * 3600),
            ("net_connections", s.network_hours * 3600),
            ("process_events", s.events_days * 86400),
            ("self_telemetry", s.self_telemetry_days * 86400),
            # 시스템 사건은 양이 적고(하루 몇 건) 진단 가치가 커서 오래 남긴다.
            # 절전 공백 기록은 나중에 베이스라인이 그 구간을 제외하는 근거가 된다.
            ("system_events", s.events_days * 86400),
        ]

    def purge_once(self) -> dict[str, int]:
        """한 번 정리하고 테이블별 삭제 행 수를 돌려준다."""
        now = time.time()
        deleted: dict[str, int] = {}
        for table, keep_seconds in self._rules():
            cutoff = now - keep_seconds
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
        old_ts = time.time() - 400 * 86400  # 확실히 기한이 지난 시각
        db.insert_many("metrics_raw", ("ts", "cpu_total"), [(old_ts, 1.0)])
        before = db.query("SELECT COUNT(*) AS c FROM metrics_raw WHERE ts < ?", (old_ts + 1,))[0]["c"]

        retention = Retention(db, load_settings().retention)
        deleted = retention.purge_once()

        after = db.query("SELECT COUNT(*) AS c FROM metrics_raw WHERE ts < ?", (old_ts + 1,))[0]["c"]
        print(f"  기한 지난 행: {before} -> {after}")
        print(f"  삭제 내역: {deleted or '(없음)'}")
        print(f"  DB 크기: {db.size_bytes():,} bytes")
        if after != 0:
            print("[FAIL] 기한이 지난 행이 남아 있다")
            raise SystemExit(1)
    print("[OK] storage.retention")

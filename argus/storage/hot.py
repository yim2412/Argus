"""핫 저장소 — SQLite (WAL).

**마이그레이션을 처음부터 넣는 이유**: 배포하고 나면 스키마 변경이 곧 "남의 PC 에 있는
DB 를 고치는 일"이 된다. 나중에 붙이면 이미 배포된 버전들을 소급해서 다뤄야 하므로
첫 테이블을 만들 때 함께 들어가야 한다.

스키마 정본은 `migrations/NNN_*.sql` 파일들이다. 별도의 전체 스키마 파일을 두지 않는
이유는 두 벌이 되면 반드시 어긋나기 때문이다. 현재 스키마가 궁금하면 마이그레이션을
순서대로 읽으면 된다.

**스레드 안전성**: 커넥션 하나를 락으로 감싸 공유한다. Phase 1 에서 배치 writer 스레드가
쓰기를 전담하게 되면 경합은 사실상 사라진다.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import __version__
from ..logging_setup import get_logger
from ..paths import db_path, resource_path

log = get_logger(__name__)

_MIGRATION_PATTERN = re.compile(r"^(\d{3})_.+\.sql$")


def migration_files() -> list[tuple[int, Path]]:
    """`migrations/` 안의 마이그레이션을 번호 순으로."""
    directory = resource_path("storage/migrations")
    found: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        match = _MIGRATION_PATTERN.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


class Database:
    """SQLite 커넥션 래퍼."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 생명주기

    def open(self) -> "Database":
        if self._conn is not None:
            return self

        # check_same_thread=False + 자체 락. 커넥션을 스레드 간에 공유하기 위함.
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row

        # WAL: 읽기와 쓰기가 서로를 막지 않는다. 대시보드가 읽는 동안 수집이 계속돼야 한다.
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL: 매 커밋마다 fsync 하지 않는다. OS 크래시 시 최근 몇 건을 잃을 수 있으나
        #         메트릭 데이터는 그 정도 손실이 허용된다. 대신 쓰기 비용이 크게 준다.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        # 임시 테이블·정렬을 메모리에서. 디스크 IO 를 만들지 않기 위함(우리가 관측 대상이다).
        conn.execute("PRAGMA temp_store=MEMORY")

        self._conn = conn
        self._migrate()
        return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA optimize")
                    self._conn.commit()
                finally:
                    self._conn.close()
                    self._conn = None

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.open() 을 먼저 호출해야 합니다")
        return self._conn

    # ------------------------------------------------------------------ 마이그레이션

    def schema_version(self) -> int:
        with self._lock:
            row = self.conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _migrate(self) -> None:
        current = self.schema_version()
        pending = [(v, p) for v, p in migration_files() if v > current]
        if not pending:
            return

        log.info(
            "스키마 마이그레이션",
            extra={"from": current, "to": pending[-1][0], "count": len(pending)},
        )
        for version, path in pending:
            sql = path.read_text(encoding="utf-8")
            with self._lock:
                try:
                    self.conn.executescript(sql)
                    # user_version 은 파라미터 바인딩을 못 받는다. 값은 파일명에서 온
                    # 정수라 주입 위험은 없다.
                    self.conn.execute(f"PRAGMA user_version={int(version)}")
                    self.conn.commit()
                except sqlite3.Error:
                    self.conn.rollback()
                    log.exception("마이그레이션 실패", extra={"version": version, "file": path.name})
                    raise
            log.debug("마이그레이션 적용", extra={"version": version, "file": path.name})

        self.set_meta("app_version", __version__)
        self.set_meta("migrated_at", str(time.time()))

    # ------------------------------------------------------------------ 조작

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def insert_many(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
        """배치 삽입. 소요 시간(ms)이 아니라 삽입 행 수를 돌려준다."""
        rows = list(rows)
        if not rows:
            return 0
        placeholders = ", ".join("?" * len(columns))
        column_list = ", ".join(columns)
        sql = f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})"
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()
        return len(rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def size_bytes(self) -> int:
        """DB 파일 크기 (WAL·SHM 포함). 보존 정책·진단용."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total


if __name__ == "__main__":  # 스모크: python -m argus.storage.hot
    from ..logging_setup import setup

    setup(level="INFO")
    with Database() as db:
        print(f"  파일      : {db.path}")
        print(f"  스키마    : v{db.schema_version()}")
        print(f"  마이그레이션: {[p.name for _, p in migration_files()]}")

        journal = db.query("PRAGMA journal_mode")[0][0]
        print(f"  journal   : {journal}")

        n = db.insert_many(
            "self_telemetry",
            ["ts", "cpu_percent", "rss_mb", "throttle_level"],
            [(time.time(), 0.5, 42.0, 0)],
        )
        count = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]
        print(f"  삽입      : {n}행 (누적 {count}행)")
        print(f"  크기      : {db.size_bytes():,} bytes")

        if journal.lower() != "wal":
            print(f"[FAIL] WAL 모드가 적용되지 않았습니다 (journal_mode={journal})")
            raise SystemExit(1)
    print("[OK] storage.hot")

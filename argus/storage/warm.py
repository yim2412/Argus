"""웜 스토어 — 1분 집계를 Parquet 일별 파티션으로 내보내고 DuckDB 로 조회한다.

**왜 SQLite 에서 곧장 Parquet 으로 가지 않는가.** Parquet 은 append 를 지원하지 않는다.
한 줄을 더하려면 파일 전체를 다시 써야 한다. 그래서 1분마다 직접 쓸 수 없고, 구조가
2단이 된다.

    metrics_raw(초, 24h)  →  metrics_1m(SQLite, 최근 며칠)  →  warm/date=.../metrics.parquet

**완전히 끝난 날짜만** 내보낸다. 그 날짜에 더 들어올 데이터가 없어야 파일이 불변이 되고,
불변이어야 재작성도, 쓰는 도중 크래시도 없다. 오늘·어제를 건드리지 않는 이유가 이것이다.

내보내기 순서도 뒤집으면 안 된다 — **파일을 먼저 쓰고, 검증하고, 그 다음 SQLite 에서
지운다.** 반대로 하면 쓰기가 실패했을 때 데이터가 사라진다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config.loader import WarmSettings
from ..logging_setup import get_logger
from ..paths import data_dir
from ..runtime.supervisor import Component
from .hot import Database
from .rollup import COLUMNS as ROLLUP_COLUMNS
from .rollup import PROCESS_COLUMNS

log = get_logger(__name__)


def warm_dir() -> Path:
    path = data_dir() / "warm"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Source:
    """내보낼 대상 하나. 종류가 늘어도 이 표만 고치면 된다."""

    kind: str
    table: str
    ts_column: str
    columns: tuple[str, ...]


SOURCES: dict[str, Source] = {
    "metrics": Source("metrics", "metrics_1m", "ts_min", ROLLUP_COLUMNS),
    "process": Source("process", "process_5m", "ts_5m", PROCESS_COLUMNS),
}


def partition_path(date_key: str, kind: str = "metrics") -> Path:
    """`warm/date=YYYY-MM-DD/<kind>.parquet`.

    Hive 스타일 디렉터리명을 쓰는 이유: DuckDB 가 `hive_partitioning` 으로 경로에서
    날짜를 컬럼으로 복원해 준다. 날짜 필터가 파일을 열지 않고 걸린다.
    """
    return warm_dir() / f"date={date_key}" / f"{kind}.parquet"


def _day_bounds(day: date) -> tuple[float, float]:
    """로컬 시각 기준 하루의 [시작, 끝) epoch.

    UTC 가 아니라 로컬인 이유: 이 데이터의 소비자는 사람이고, "어제 밤에 왜 느렸나"의
    '어제'는 그 사람의 달력이다.
    """
    start = datetime(day.year, day.month, day.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


class WarmStore:
    """Parquet 내보내기와 DuckDB 조회. 컴포넌트가 아니라 순수 로직이다."""

    def __init__(self, db: Database, settings: WarmSettings) -> None:
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------ 내보내기

    def exportable_dates(self, kind: str = "metrics", now: float | None = None) -> list[str]:
        """아직 안 내보냈고, 이미 끝난 날짜들."""
        source = SOURCES[kind]
        now = now if now is not None else time.time()
        cutoff = datetime.fromtimestamp(now).date() - timedelta(days=self.settings.export_after_days)

        rows = self.db.query(
            f"SELECT MIN({source.ts_column}) AS lo, MAX({source.ts_column}) AS hi "
            f"FROM {source.table}"
        )
        if not rows or rows[0]["lo"] is None:
            return []
        lo = datetime.fromtimestamp(rows[0]["lo"]).date()
        hi = datetime.fromtimestamp(rows[0]["hi"]).date()

        done = {
            r["date_key"]
            for r in self.db.query("SELECT date_key FROM warm_exports WHERE kind = ?", (kind,))
        }
        out: list[str] = []
        day = lo
        while day <= hi and day <= cutoff:
            key = day.isoformat()
            if key not in done:
                out.append(key)
            day += timedelta(days=1)
        return out

    def export_date(self, date_key: str, kind: str = "metrics") -> int:
        """하루치를 Parquet 으로 쓴다. 쓴 행 수를 돌려준다."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        source = SOURCES[kind]
        day = date.fromisoformat(date_key)
        start, end = _day_bounds(day)
        rows = self.db.query(
            f"SELECT {', '.join(source.columns)} FROM {source.table} "
            f"WHERE {source.ts_column} >= ? AND {source.ts_column} < ? ORDER BY {source.ts_column}",
            (start, end),
        )
        if not rows:
            return 0

        table = pa.table({name: [row[name] for row in rows] for name in source.columns})
        target = partition_path(date_key, kind)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 임시 파일에 쓰고 원자적으로 옮긴다. 쓰는 도중 죽어도 반쪽 파일이
        # 파티션 자리에 남지 않는다 — DuckDB 가 그걸 읽으면 조회 전체가 깨진다.
        temp = target.with_suffix(".parquet.tmp")
        pq.write_table(table, temp, compression=self.settings.compression)
        temp.replace(target)

        size = target.stat().st_size
        with self.db._lock:  # noqa: SLF001
            self.db.conn.execute(
                "INSERT INTO warm_exports (date_key, kind, path, row_count, bytes, exported_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(date_key, kind) DO UPDATE SET "
                "path=excluded.path, row_count=excluded.row_count, bytes=excluded.bytes, "
                "exported_at=excluded.exported_at",
                (date_key, kind, str(target), len(rows), size, time.time()),
            )
            self.db.conn.commit()

        if self.settings.purge_after_export:
            # 파일이 실제로 읽히는지 확인한 뒤에 지운다. 쓰기 성공과 읽기 가능은 다르다.
            verified = self._verify(target, len(rows))
            if verified:
                with self.db._lock:  # noqa: SLF001
                    self.db.conn.execute(
                        f"DELETE FROM {source.table} "
                        f"WHERE {source.ts_column} >= ? AND {source.ts_column} < ?",
                        (start, end),
                    )
                    self.db.conn.commit()
            else:
                log.error(
                    "Parquet 검증 실패 — SQLite 원본을 남긴다",
                    extra={"date": date_key, "kind": kind},
                )

        log.info("웜 내보내기", extra={"date": date_key, "kind": kind, "rows": len(rows), "bytes": size})
        return len(rows)

    def _verify(self, path: Path, expected_rows: int) -> bool:
        try:
            import pyarrow.parquet as pq

            return pq.ParquetFile(path).metadata.num_rows == expected_rows
        except Exception:
            log.exception("Parquet 읽기 검증 실패", extra={"path": str(path)})
            return False

    def export_pending(self, now: float | None = None) -> dict[str, int]:
        """모든 종류의 밀린 날짜를 내보낸다. 키는 `YYYY-MM-DD/<kind>`."""
        result: dict[str, int] = {}
        for kind in SOURCES:
            for date_key in self.exportable_dates(kind, now):
                try:
                    result[f"{date_key}/{kind}"] = self.export_date(date_key, kind)
                except Exception:
                    # 하루가 실패해도 나머지 날짜·종류는 내보낸다.
                    log.exception("웜 내보내기 실패", extra={"date": date_key, "kind": kind})
        return result

    # ------------------------------------------------------------ 조회

    def query(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """DuckDB 로 웜 스토어를 조회한다.

        SQL 안에서 `warm`(1분 지표)과 `warm_process`(5분 프로세스)를 테이블처럼 쓴다.
        **종류별로 뷰를 나누는 이유**: 두 Parquet 은 스키마가 다르다. 한 뷰로 묶으면
        컬럼이 맞지 않아 조회 자체가 실패한다. 나눠 두면 필요할 때 조인하면 된다.

        존재하지 않는 종류의 뷰는 만들지 않는다 — 파티션이 하나도 없는 경로를
        `read_parquet` 에 주면 오류가 난다.
        """
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            for kind, view in (("metrics", "warm"), ("process", "warm_process")):
                if not self.has_partitions(kind):
                    continue
                pattern = str(warm_dir() / "**" / f"{kind}.parquet").replace("\\", "/")
                con.execute(
                    f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{pattern}', "
                    "hive_partitioning = true)"
                )
            return con.execute(sql, params or []).fetchall()
        finally:
            con.close()

    def has_partitions(self, kind: str = "metrics") -> bool:
        return any(warm_dir().glob(f"date=*/{kind}.parquet"))


class WarmExporter(Component):
    """주기적으로 끝난 날짜를 내보내는 컴포넌트."""

    name = "warm"

    def __init__(self, db: Database, settings: WarmSettings) -> None:
        self.store = WarmStore(db, settings)
        self.interval_s = settings.interval_s

    def tick(self) -> None:
        self.store.export_pending()


if __name__ == "__main__":  # 스모크: python -m argus.storage.warm
    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="INFO")
    settings = load_settings()

    with Database() as db:
        store = WarmStore(db, settings.warm)
        print(f"  웜 디렉터리 : {warm_dir()}")
        for kind in SOURCES:
            pending = store.exportable_dates(kind)
            print(f"  내보낼 날짜 ({kind:<7}): {pending or '(없음 — 끝난 날짜가 아직 없다)'}")

        exported = store.export_pending()
        print(f"  내보냄      : {exported or '(없음)'}")

        rows = db.query(
            "SELECT date_key, kind, row_count, bytes FROM warm_exports ORDER BY date_key, kind"
        )
        for row in rows:
            print(
                f"    {row['date_key']} {row['kind']:<8} {row['row_count']:>6}행  "
                f"{row['bytes']:,} bytes"
            )

        if store.has_partitions("metrics") or store.has_partitions("process"):
            if store.has_partitions("metrics"):
                print(f"  DuckDB(지표) : {store.query('SELECT count(*) FROM warm')}")
            if store.has_partitions("process"):
                print(
                    "  DuckDB(프로세스): "
                    f"{store.query('SELECT count(*), count(DISTINCT name) FROM warm_process')}"
                )
        else:
            # 파티션이 없어도 DuckDB 자체는 확인해 둔다. 배포 후 여기서 처음
            # 깨지면 이유를 찾기 어렵다.
            import duckdb

            assert duckdb.connect(":memory:").execute("SELECT 1").fetchone() == (1,)
            print("  DuckDB 조회 : (파티션 없음 — 엔진 동작만 확인)")
    print("[OK] storage.warm")

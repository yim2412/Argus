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

import sqlite3
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

#: 한 번에 읽어 한 row group 으로 쓰는 행 수. 이 값이 곧 내보내기 중 메모리 상한이다.
#: 너무 작으면 row group 이 잘게 쪼개져 조회가 느려지고, 너무 크면 예전 문제로 돌아간다.
EXPORT_CHUNK_ROWS = 50_000

#: 내보낸 원본을 지울 때 한 문장이 다루는 행 수. 하루치를 한 번에 지우면 그동안
#: 락을 붙들어 수집 쓰기가 멈춘다(보존 정리와 같은 이유 — `fd31f70`).
PURGE_CHUNK_ROWS = 20_000


def _record_batch(chunk: list[sqlite3.Row], schema: Any) -> Any:
    """SQLite 행 묶음 → Arrow RecordBatch. **열 방향으로 뒤집는 자리다.**

    sqlite3 는 행 단위로 주고 Parquet 은 열 단위로 저장한다. 그 전환을 청크
    크기만큼만 하는 것이 이 함수의 존재 이유다 — 예전에는 하루치 전체를 한 번에
    뒤집었고, 그 순간 같은 데이터가 메모리에 두 벌 있었다.
    """
    import pyarrow as pa

    columns = [
        pa.array([row[name] for row in chunk], type=field.type)
        for name, field in zip(schema.names, schema)
    ]
    return pa.RecordBatch.from_arrays(columns, schema=schema)


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


def has_partitions(kind: str = "metrics") -> bool:
    return any(warm_dir().glob(f"date=*/{kind}.parquet"))


def partition_days(kind: str = "metrics") -> list[str]:
    """웜에 파티션이 있는 날짜들 (`YYYY-MM-DD`).

    파일명에서 읽는다 — 내용을 열지 않으므로 날짜 목록만 필요한 쪽이 DuckDB 를
    켜지 않아도 된다. 관측자를 무겁게 만들지 않기 위한 것이다.
    """
    days = []
    for path in warm_dir().glob(f"date=*/{kind}.parquet"):
        key = path.parent.name.removeprefix("date=")
        try:
            date.fromisoformat(key)
        except ValueError:
            continue  # 사람이 만든 디렉터리일 수 있다. 조용히 건너뛴다.
        days.append(key)
    return sorted(days)


def _inline_params(sql: str, params: list[Any]) -> str:
    """`?` 를 값으로 치환한다. **숫자만 허용한다.**

    **DuckDB 는 파라미터를 바인딩하면 `read_parquet` 의 필터 푸시다운을 하지 못해
    Parquet 을 통째로 메모리에 올린다.** 2026-08-04 실측 — 지문 빌드의 웜 조회가
    바인딩이면 private **+416MB / 0.36초**, 리터럴이면 **+18MB / 0.06초**이고
    결과는 1,396칸 전부 같았다. 메모리 23배, 속도 6배 차이가 값의 표현 방식 하나에서 났다.

    이것이 상주가 예산 300MB 를 1.7배 넘긴 원인이었다. 기동 10초 만에 private 이
    506MB 로 뛰었고, 그 커밋된 메모리가 워킹셋에 들어올 때마다 RSS 가 700MB 까지
    올라 스로틀이 걸렸다. 스로틀은 증상이고 원인은 여기였다.

    **숫자 외에는 거부한다.** 문자열을 박으면 인젝션 경로가 생기고, 조용히 바인딩으로
    되돌아가면 이 문제가 되살아난다 — 느려지는 것보다 터지는 것이 낫다.
    (SQLite 쪽은 그대로 바인딩한다. 이 문제는 DuckDB + Parquet 조합에서만 난다.)
    """
    if not params:
        return sql
    chunks = sql.split("?")
    if len(chunks) - 1 != len(params):
        raise ValueError(
            f"플레이스홀더 {len(chunks) - 1}개와 값 {len(params)}개가 맞지 않는다"
        )
    out = [chunks[0]]
    for value, tail in zip(params, chunks[1:]):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(
                f"웜 조회 파라미터는 숫자만 가능하다 (받은 값: {value!r}). "
                "문자열이 필요하면 값을 검증한 뒤 SQL 에 직접 넣어라 — "
                "바인딩으로 돌아가면 Parquet 전체를 메모리에 올린다."
            )
        out.append(repr(float(value)))
        out.append(tail)
    return "".join(out)


def query(sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
    """DuckDB 로 웜 스토어를 조회한다.

    SQL 안에서 `warm`(1분 지표)과 `warm_process`(5분 프로세스)를 테이블처럼 쓴다.
    **종류별로 뷰를 나누는 이유**: 두 Parquet 은 스키마가 다르다. 한 뷰로 묶으면
    컬럼이 맞지 않아 조회 자체가 실패한다. 나눠 두면 필요할 때 조인하면 된다.

    존재하지 않는 종류의 뷰는 만들지 않는다 — 파티션이 하나도 없는 경로를
    `read_parquet` 에 주면 오류가 난다.

    **`Database` 를 받지 않는다.** 웜 조회는 SQLite 를 전혀 건드리지 않는데 메서드로
    두면 호출자가 쓰지도 않을 커넥션을 열어야 했다. 대시보드·도구가 웜만 읽는 경우가
    실제로 생겨 모듈 함수로 옮겼다.
    """
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        for kind, view in (("metrics", "warm"), ("process", "warm_process")):
            if not has_partitions(kind):
                continue
            pattern = str(warm_dir() / "**" / f"{kind}.parquet").replace("\\", "/")
            con.execute(
                f"CREATE VIEW {view} AS SELECT * FROM read_parquet('{pattern}', "
                # **`union_by_name` 이 없으면 스키마가 바뀐 날 과거가 통째로 사라진다.**
                # 컬럼을 추가하면 그 이전 파티션에는 그 컬럼이 없고, DuckDB 는 여러
                # 파일을 위치 기준으로 합치다 `Binder Error` 로 터진다. 2026-08-03 에
                # `gpu_clock_sm_*` 을 넣은 뒤 타임라인의 웜 구간(이틀 지난 날짜)이
                # 그렇게 조용히 비어 있었다 — 예외가 UI 안에서 삼켜져 화면만 비었다.
                # 이름 기준으로 합치면 없는 컬럼은 NULL 로 들어온다.
                "hive_partitioning = true, union_by_name = true)"
            )
        # **값을 SQL 에 박아 넣는다.** 이유는 `_inline_params` 에 있다 —
        # 바인딩하면 Parquet 을 통째로 메모리에 올린다(실측 +416MB).
        return con.execute(_inline_params(sql, list(params or []))).fetchall()
    finally:
        con.close()


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
        """하루치를 Parquet 으로 쓴다. 쓴 행 수를 돌려준다.

        **하루치를 통째로 메모리에 올리지 않는다.** 예전에는 `db.query()` 로 전부
        읽어 리스트를 만들고 그것으로 pyarrow 테이블을 또 한 벌 만들었다. 그동안
        (1) rss 가 예산 300MB 를 넘어 스로틀이 걸리고(실측 피크 745MB), (2) 조회가
        끝날 때까지 `db._lock` 을 붙들어 **수집 쓰기가 멈췄다** — `DB 쓰기가 느리다`
        경고의 출처 중 하나가 여기다.

        읽기는 별도 읽기전용 커넥션으로 뗀다. **WAL 을 택한 이유가 "읽기와 쓰기가
        서로를 막지 않는다"인데 전역 락이 그걸 무효화하고 있었다.** 내보내는 구간은
        이미 끝난 날짜라 읽는 도중 바뀌지 않으므로 일관성 문제도 없다.
        """
        import pyarrow.parquet as pq

        source = SOURCES[kind]
        day = date.fromisoformat(date_key)
        start, end = _day_bounds(day)

        target = partition_path(date_key, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 임시 파일에 쓰고 원자적으로 옮긴다. 쓰는 도중 죽어도 반쪽 파일이
        # 파티션 자리에 남지 않는다 — DuckDB 가 그걸 읽으면 조회 전체가 깨진다.
        temp = target.with_suffix(".parquet.tmp")

        schema = self._arrow_schema(source)
        written = 0
        writer: Any = None
        reader = self._read_connection()
        try:
            cursor = reader.execute(
                f"SELECT {', '.join(source.columns)} FROM {source.table} "
                f"WHERE {source.ts_column} >= ? AND {source.ts_column} < ? "
                f"ORDER BY {source.ts_column}",
                (start, end),
            )
            while True:
                chunk = cursor.fetchmany(EXPORT_CHUNK_ROWS)
                if not chunk:
                    break
                if writer is None:
                    writer = pq.ParquetWriter(
                        temp, schema, compression=self.settings.compression
                    )
                writer.write_batch(_record_batch(chunk, schema))
                written += len(chunk)
        finally:
            if writer is not None:
                writer.close()
            reader.close()

        if not written:
            temp.unlink(missing_ok=True)
            return 0

        temp.replace(target)
        size = target.stat().st_size
        with self.db._lock:  # noqa: SLF001
            self.db.conn.execute(
                "INSERT INTO warm_exports (date_key, kind, path, row_count, bytes, exported_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(date_key, kind) DO UPDATE SET "
                "path=excluded.path, row_count=excluded.row_count, bytes=excluded.bytes, "
                "exported_at=excluded.exported_at",
                (date_key, kind, str(target), written, size, time.time()),
            )
            self.db.conn.commit()

        if self.settings.purge_after_export:
            # 파일이 실제로 읽히는지 확인한 뒤에 지운다. 쓰기 성공과 읽기 가능은 다르다.
            if self._verify(target, written):
                self._purge(source, start, end)
            else:
                log.error(
                    "Parquet 검증 실패 — SQLite 원본을 남긴다",
                    extra={"date": date_key, "kind": kind},
                )

        log.info("웜 내보내기", extra={"date": date_key, "kind": kind, "rows": written, "bytes": size})
        return written

    def _purge(self, source: Source, start: float, end: float) -> None:
        """내보낸 구간을 SQLite 에서 지운다. **나눠 지운다.**

        하루치를 한 문장으로 지우면 그동안 락을 붙들어 수집 쓰기가 멈춘다 —
        보존 정리를 청크로 쪼갠 것(`fd31f70`)과 같은 이유이고, 같은 처방이다.
        """
        while True:
            with self.db._lock:  # noqa: SLF001
                cursor = self.db.conn.execute(
                    f"DELETE FROM {source.table} WHERE rowid IN ("
                    f"  SELECT rowid FROM {source.table}"
                    f"  WHERE {source.ts_column} >= ? AND {source.ts_column} < ?"
                    f"  LIMIT ?)",
                    (start, end, PURGE_CHUNK_ROWS),
                )
                self.db.conn.commit()
            if cursor.rowcount < PURGE_CHUNK_ROWS:
                return

    def _read_connection(self) -> sqlite3.Connection:
        """내보내기 전용 **읽기 커넥션**.

        상주의 쓰기 커넥션과 락을 공유하지 않는다. WAL 이라 읽기가 쓰기를 막지
        않는데, 전역 락 하나가 그 이점을 통째로 무효화하고 있었다.
        """
        connection = sqlite3.connect(
            f"file:{self.db.path}?mode=ro", uri=True, timeout=30.0
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _arrow_schema(self, source: Source) -> Any:
        """SQLite 선언 타입 → Arrow 스키마.

        **첫 청크로 추론하지 않는다.** 그 청크에서 어떤 컬럼이 전부 NULL 이면
        `null` 타입이 잡히고 **다음 청크에서 캐스팅이 깨진다** — 데이터가 드문
        날에만 나는 실패라 평소에는 아무 신호가 없다. 파일 전체를 한 번에 쓰던
        예전 코드에는 없던 위험이라, 스트리밍으로 바꾸면서 같이 막는다.
        """
        import pyarrow as pa

        declared = {
            row["name"]: (row["type"] or "").upper()
            for row in self.db.query(f"PRAGMA table_info({source.table})")
        }
        fields = []
        for name in source.columns:
            sql_type = declared.get(name, "")
            if "INT" in sql_type:
                arrow_type = pa.int64()
            elif "REAL" in sql_type or "FLOA" in sql_type or "DOUB" in sql_type:
                arrow_type = pa.float64()
            else:
                arrow_type = pa.string()
            # **전부 nullable 이다.** 수집기 하나가 죽어도 나머지는 계속되므로
            # (수집 규칙 1) 어떤 컬럼이든 비어 있을 수 있다.
            fields.append(pa.field(name, arrow_type, nullable=True))
        return pa.schema(fields)

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
        return query(sql, params)

    def has_partitions(self, kind: str = "metrics") -> bool:
        return has_partitions(kind)


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

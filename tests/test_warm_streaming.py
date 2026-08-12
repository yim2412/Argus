"""웜 내보내기는 하루치를 통째로 메모리에 올리지 않는다.

**여기 있는 것은 전부 조용히 깨지는 종류다.** 결과 파일은 어느 쪽이든 멀쩡히
만들어지고 행 수도 맞는다 — 무거워지거나, 그동안 수집 쓰기가 멈추거나, 데이터가
드문 날에만 타입이 달라질 뿐이다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("pyarrow")

from argus.config.loader import WarmSettings  # noqa: E402
from argus.storage import warm as warm_mod  # noqa: E402
from argus.storage.hot import Database  # noqa: E402

#: 테스트가 쓰는 청크 크기. **`EXPORT_CHUNK_ROWS` 를 가져다 쓰지 않는다** — 기댓값을
#: 검증 대상에서 가져오면 그 상수를 1 로 바꿔도 양쪽이 함께 바뀌어 통과한다
#: (`READABLE_PLOT_PX` 와 같은 이유).
CHUNK = 25


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(warm_mod, "warm_dir", lambda: tmp_path / "warm")
    monkeypatch.setattr(warm_mod, "EXPORT_CHUNK_ROWS", CHUNK)
    db = Database(tmp_path / "t.db").open()
    settings = WarmSettings()
    settings.purge_after_export = False
    yield warm_mod.WarmStore(db, settings), db
    db.close()


def _rows(db: Database, count: int, *, day: str = "2026-08-01", gpu=None) -> None:
    """`metrics_1m` 에 `count` 행. `gpu=None` 이면 그 컬럼이 전부 NULL 인 날이 된다."""
    from datetime import datetime

    base = datetime.fromisoformat(f"{day}T01:00:00").timestamp()
    columns = ("ts_min", "sample_count", "cpu_mean", "gpu_clock_sm_mean")
    db.insert_many(
        "metrics_1m",
        columns,
        [(base + i * 60, 60, float(i), gpu) for i in range(count)],
    )


def test_all_rows_survive_chunk_boundaries(store) -> None:
    """**청크 경계에서 행이 새면 조용하다** — 파일은 멀쩡히 만들어지고 조금 적을 뿐이다."""
    import pyarrow.parquet as pq

    warm_store, db = store
    total = CHUNK * 2 + 7  # 경계에 딱 맞지 않게. 딱 맞으면 off-by-one 이 안 드러난다
    _rows(db, total, gpu=1500.0)

    written = warm_store.export_date("2026-08-01", "metrics")

    assert written == total, f"{total}행을 넣었는데 {written}행을 썼다"
    table = pq.read_table(warm_mod.partition_path("2026-08-01", "metrics"))
    assert table.num_rows == total
    assert table.column("cpu_mean").to_pylist() == [float(i) for i in range(total)]


def test_all_null_column_keeps_its_real_type(store) -> None:
    """**전부 NULL 인 컬럼도 제 타입으로 쓴다.**

    첫 청크로 타입을 추론하면 그런 컬럼이 `null` 타입이 되고, 같은 컬럼이 날마다
    다른 타입인 파일들이 생긴다. 실측(2026-08-12): 07-27 을 재내보내니 옛 구현은
    `gpu_clock_sm_mean` 을 `null` 로 썼고 다른 날 파일들은 `double` 이었다.
    **데이터가 드문 날에만 나므로 평소에는 아무 신호가 없다.**
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    warm_store, db = store
    _rows(db, 5, gpu=None)  # gpu_clock_sm_mean 이 그날 전부 NULL

    warm_store.export_date("2026-08-01", "metrics")

    schema = pq.read_schema(warm_mod.partition_path("2026-08-01", "metrics"))
    kind = schema.field("gpu_clock_sm_mean").type
    assert not pa.types.is_null(kind), "전부 NULL 이라고 타입까지 null 로 썼다"
    assert pa.types.is_floating(kind), f"REAL 컬럼인데 {kind} 로 썼다"


def test_export_does_not_hold_the_write_lock(store) -> None:
    """**읽는 동안 `db._lock` 을 붙들지 않는다.**

    WAL 을 택한 이유가 "읽기와 쓰기가 서로를 막지 않는다"인데 전역 락이 그걸
    무효화하고 있었다. 붙들면 그동안 수집 쓰기가 통째로 멈춘다 — 결과 파일은
    똑같이 나오므로 **이 테스트 말고는 알 방법이 없다.**

    청크를 쓰는 순간마다 락이 비어 있는지 본다(타이밍이 아니라 지점으로 확인한다).
    """
    warm_store, db = store
    _rows(db, CHUNK * 2, gpu=1.0)

    seen: list[bool] = []
    original = warm_mod._record_batch  # noqa: SLF001

    def spy(chunk, schema):
        free = db._lock.acquire(blocking=False)  # noqa: SLF001
        seen.append(free)
        if free:
            db._lock.release()  # noqa: SLF001
        return original(chunk, schema)

    warm_mod._record_batch = spy  # noqa: SLF001
    try:
        warm_store.export_date("2026-08-01", "metrics")
    finally:
        warm_mod._record_batch = original  # noqa: SLF001

    assert seen, "청크 경로를 아예 타지 않았다 — 테스트가 아무것도 안 봤다"
    assert all(seen), f"내보내는 동안 쓰기 락이 잡혀 있었다: {seen}"


def test_purge_deletes_in_chunks(tmp_path, monkeypatch) -> None:
    """**내보낸 원본을 한 문장으로 지우지 않는다.**

    하루치를 한 번에 지우면 그동안 락을 붙들어 수집 쓰기가 멈춘다(보존 정리를
    청크로 쪼갠 `fd31f70` 과 같은 이유).
    """
    monkeypatch.setattr(warm_mod, "warm_dir", lambda: tmp_path / "warm")
    monkeypatch.setattr(warm_mod, "EXPORT_CHUNK_ROWS", CHUNK)
    monkeypatch.setattr(warm_mod, "PURGE_CHUNK_ROWS", 10)

    db = Database(tmp_path / "t.db").open()
    settings = WarmSettings()
    settings.purge_after_export = True
    warm_store = warm_mod.WarmStore(db, settings)
    _rows(db, 35, gpu=1.0)

    # `sqlite3.Connection` 은 불변 타입이라 메서드를 갈아 끼울 수 없다. 커넥션을
    # 감싸 DELETE 문만 세고 나머지는 그대로 흘려보낸다.
    statements: list[str] = []
    real_conn = db.conn

    class _ConnSpy:
        def execute(self, sql, *args):
            if sql.strip().upper().startswith("DELETE"):
                statements.append(sql)
            return real_conn.execute(sql, *args)

        def __getattr__(self, key):
            return getattr(real_conn, key)

    monkeypatch.setattr(type(db), "conn", property(lambda self: _ConnSpy()))
    warm_store.export_date("2026-08-01", "metrics")

    left = db.query("SELECT COUNT(*) AS n FROM metrics_1m")[0]["n"]
    db.close()

    assert left == 0, f"내보내고도 원본 {left}행이 남았다"
    assert len(statements) >= 4, (
        f"35행을 10행씩 지우면 DELETE 가 4번 이상이어야 하는데 {len(statements)}번이다"
    )

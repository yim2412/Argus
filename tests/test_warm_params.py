"""웜(DuckDB) 조회는 값을 바인딩하지 않고 SQL 에 박는다.

**DuckDB 는 파라미터를 바인딩하면 `read_parquet` 의 필터 푸시다운을 못 해 Parquet 을
통째로 메모리에 올린다.** 2026-08-04 실측 — 지문 빌드의 웜 조회가 바인딩이면
private +416MB / 0.36초, 리터럴이면 +18MB / 0.06초, 결과는 1,396칸 전부 동일.

상주가 예산 300MB 를 1.7배 넘긴 원인이 이것이었다. 기동 10초 만에 private 506MB 가
확정되고, 그 메모리가 워킹셋에 들어올 때마다 RSS 700MB 로 스로틀이 걸렸다.

**조용히 되돌아가는 종류다.** 바인딩으로 되돌려도 결과는 똑같이 나오고 테스트도
전부 통과한다 — 느려지고 무거워질 뿐이다. 그래서 "무엇을 넘겼는가"를 직접 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.storage import warm  # noqa: E402


def test_numbers_are_inlined():
    sql = warm._inline_params("SELECT * FROM t WHERE a > ? AND b < ?", [1.5, 20])  # noqa: SLF001
    assert "?" not in sql, "플레이스홀더가 남았다 — 바인딩 경로로 간다"
    assert "1.5" in sql and "20.0" in sql


def test_no_params_is_untouched():
    sql = "SELECT 1"
    assert warm._inline_params(sql, []) is sql  # noqa: SLF001


def test_strings_are_rejected():
    """문자열을 박으면 인젝션 경로가 생긴다. 조용히 바인딩으로 돌아가지도 않는다 —
    그러면 이 문제가 되살아나므로, 느려지는 것보다 터지는 것이 낫다."""
    with pytest.raises(TypeError):
        warm._inline_params("SELECT * FROM t WHERE name = ?", ["'; DROP TABLE t; --"])  # noqa: SLF001


def test_booleans_are_rejected():
    """`bool` 은 `int` 의 하위형이라 검사를 그냥 통과한다. 값이 `True` 면 `1.0` 이 되는데,
    그건 호출자가 의도한 것이 아닐 가능성이 높다."""
    with pytest.raises(TypeError):
        warm._inline_params("SELECT ?", [True])  # noqa: SLF001


def test_count_mismatch_raises():
    with pytest.raises(ValueError):
        warm._inline_params("SELECT * FROM t WHERE a > ? AND b < ?", [1.0])  # noqa: SLF001
    with pytest.raises(ValueError):
        warm._inline_params("SELECT * FROM t WHERE a > ?", [1.0, 2.0])  # noqa: SLF001


def test_inlined_values_survive_a_round_trip():
    """박아 넣은 값이 실제로 같은 결과를 내는가. 표현이 깨지면(지수 표기 등)
    조용히 다른 구간을 읽게 된다 — epoch 는 1.7e9 라 이게 실제 위험이다."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (1785000000.0), (1785999999.5)) AS v(ts)")
        lo, hi = 1785000000.0, 1785999999.5
        inlined = warm._inline_params("SELECT COUNT(*) FROM t WHERE ts >= ? AND ts <= ?",  # noqa: SLF001
                                      [lo, hi])
        bound = con.execute("SELECT COUNT(*) FROM t WHERE ts >= ? AND ts <= ?", [lo, hi]).fetchone()
        assert con.execute(inlined).fetchone() == bound
    finally:
        con.close()


def test_partitions_with_different_columns_can_be_read(tmp_path, monkeypatch):
    """**스키마가 바뀐 날 과거가 통째로 사라지지 않는가.**

    컬럼을 추가하면 그 이전 파티션에는 그 컬럼이 없다. `union_by_name` 없이는 DuckDB 가
    파일들을 위치 기준으로 합치다 `Binder Error` 로 터진다. 2026-08-03 에
    `gpu_clock_sm_*` 을 넣은 뒤 타임라인의 웜 구간이 그렇게 비어 있었다 — 예외가 UI
    안에서 삼켜져 **화면만 조용히 빈** 상태로 하루를 넘겼다.

    `PLAN.md` 가 "기존 파티션에는 이 두 컬럼이 없다"고 미리 적어 뒀는데도 못 잡았다.
    적어 두는 것으로는 안 되고 테스트가 있어야 한다.
    """
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    old = tmp_path / "date=2026-07-27"
    new = tmp_path / "date=2026-07-28"
    old.mkdir()
    new.mkdir()
    # 옛 파티션: 컬럼 2개
    pq.write_table(pa.table({"ts_min": [100.0], "cpu_mean": [10.0]}), old / "metrics.parquet")
    # 새 파티션: 컬럼이 하나 늘었다
    pq.write_table(
        pa.table({"ts_min": [200.0], "cpu_mean": [20.0], "gpu_clock_sm_mean": [1800.0]}),
        new / "metrics.parquet",
    )

    monkeypatch.setattr(warm, "warm_dir", lambda: tmp_path)
    monkeypatch.setattr(warm, "has_partitions", lambda kind: kind == "metrics")

    rows = warm.query("SELECT ts_min, cpu_mean, gpu_clock_sm_mean FROM warm ORDER BY ts_min")
    assert len(rows) == 2, "스키마가 다른 파티션을 합쳐 읽지 못했다"
    assert rows[0][2] is None, "옛 파티션의 없는 컬럼은 NULL 이어야 한다"
    assert rows[1][2] == 1800.0


def test_query_passes_no_params_to_duckdb(monkeypatch):
    """**배선 — DuckDB 에 실제로 무엇을 넘기는가.**

    `_inline_params` 를 잘 만들어 두고 `query()` 가 여전히 바인딩하면 아무것도 달라지지
    않는다. 결과는 똑같이 나오므로 다른 테스트로는 절대 잡히지 않는다.
    """
    seen: list[tuple] = []

    class FakeCon:
        def execute(self, sql, *args):
            seen.append((sql, args))
            return self

        def fetchall(self):
            return []

        def close(self):
            pass

    fake = FakeCon()
    monkeypatch.setattr(warm, "has_partitions", lambda kind: False)
    import duckdb

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: fake)

    warm.query("SELECT * FROM warm WHERE ts_min >= ? AND ts_min < ?", [100.0, 200.0])

    assert seen, "쿼리가 실행되지 않았다"
    sql, args = seen[-1]
    assert "?" not in sql, "플레이스홀더가 그대로 넘어갔다 — 바인딩 경로다"
    assert args == (), f"DuckDB 에 파라미터를 넘겼다: {args}"
    assert "100.0" in sql and "200.0" in sql

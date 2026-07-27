"""1분 롤업과 보존 정책의 결합을 고정한다.

여기서 지키는 것은 하나다. **접히기 전의 원본은 절대 지워지지 않는다.**
삭제는 되돌릴 수 없고, 장기 데이터(레짐 학습·ML 학습창)는 이 경로로만 남는다.
이 테스트가 깨지면 며칠치 데이터가 조용히 사라지는 버그가 들어온 것이다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from argus.config.loader import RetentionSettings, RollupSettings, WarmSettings
from argus.storage.hot import Database
from argus.storage.retention import Retention
from argus.storage.rollup import BUCKET_S, Rollup, bucket_of, _p95, _std


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _seed(database: Database, start: int, minutes: int, cpu=lambda i: 20.0) -> None:
    rows = []
    for minute in range(minutes):
        for second in range(60):
            ts = start + minute * 60 + second
            rows.append((ts, cpu(minute * 60 + second), 50.0, 30.0, 0.1))
    database.insert_many(
        "metrics_raw", ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"), rows
    )


# ---------------------------------------------------------------- 집계 정확성


def test_percentile_and_std() -> None:
    values = [float(v) for v in range(1, 101)]
    # 최근접 순위(nearest-rank). 보간하지 않는 이유는 관측된 값만 남기기 위해서다 —
    # "응답시간 p95 = 95.05ms" 처럼 실제로 없던 값을 만들지 않는다.
    assert _p95(values) == 95.0
    assert _std([5.0, 5.0, 5.0]) == 0.0
    assert _std([]) is None
    # 표본이 하나면 흔들림이 없다고 본다(추정이 아니라 관측 구간의 산포다).
    assert _std([7.0]) == 0.0


def test_rollup_aggregates_one_bucket(db: Database) -> None:
    start = bucket_of(time.time() - 3600)
    _seed(db, start, 1, cpu=lambda i: float(i))  # 0..59

    rollup = Rollup(db, RollupSettings())
    assert rollup.run_once() == 1

    row = db.query("SELECT * FROM metrics_1m")[0]
    assert row["sample_count"] == 60
    assert row["cpu_mean"] == pytest.approx(29.5)
    assert row["cpu_max"] == 59.0
    assert row["cpu_p95"] == 56.0
    assert row["cpu_std"] > 0
    # 코어 불균형 = 가장 바쁜 코어 - 전체 평균
    assert row["cpu_imbalance_mean"] == pytest.approx(50.0 - 29.5)


def test_rollup_is_idempotent(db: Database) -> None:
    """같은 버킷을 다시 접어도 결과가 같아야 한다.

    크래시로 워터마크가 뒤로 갔을 때 중복 키로 죽거나 값이 달라지면,
    복구가 곧 데이터 오염이 된다.
    """
    start = bucket_of(time.time() - 3600)
    _seed(db, start, 3)

    rollup = Rollup(db, RollupSettings())
    rollup.run_once()
    first = db.query("SELECT * FROM metrics_1m ORDER BY ts_min")

    rollup._set_watermark(start)  # 워터마크를 되감는다
    rollup.run_once()
    second = db.query("SELECT * FROM metrics_1m ORDER BY ts_min")

    assert len(first) == len(second) == 3
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_current_minute_is_not_rolled_up(db: Database) -> None:
    """진행 중인 분을 접으면 반쪽짜리 통계가 영구히 남는다."""
    now = time.time()
    # 과거 10분 ~ 미래 2분. 뒤쪽 버킷은 아직 데이터가 들어오는 중인 셈이다.
    _seed(db, bucket_of(now - 600), 12)

    Rollup(db, RollupSettings()).run_once(now=now)
    latest = db.query("SELECT MAX(ts_min) AS hi FROM metrics_1m")[0]["hi"]
    assert latest is not None
    assert latest <= bucket_of(now - RollupSettings().lag_s)


# ---------------------------------------------------------------- 보존 결합


def test_retention_never_outruns_rollup(db: Database) -> None:
    """접히지 않은 원본은 보존 기한이 지났어도 남아야 한다."""
    old = bucket_of(time.time() - 40 * 3600)  # raw_hours(24) 를 훨씬 넘긴 시각
    _seed(db, old, 5)

    retention = Retention(db, RetentionSettings())
    retention.purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM metrics_raw")[0]["c"] == 300, (
        "롤업이 한 번도 돌지 않았는데 원본이 지워졌다 — 그 구간은 어디에도 남지 않는다"
    )

    Rollup(db, RollupSettings()).run_once()
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 5

    retention.purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM metrics_raw")[0]["c"] == 0
    # 접힌 결과는 남는다. 이게 장기 데이터의 실체다.
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 5


def test_retention_stops_at_watermark_midway(db: Database) -> None:
    """워터마크가 구간 중간에 있으면 딱 거기까지만 지운다."""
    old = bucket_of(time.time() - 40 * 3600)
    _seed(db, old, 6)

    rollup = Rollup(db, RollupSettings(max_buckets_per_run=3))
    rollup.run_once()
    assert rollup.watermark() == old + 3 * BUCKET_S

    Retention(db, RetentionSettings()).purge_once()
    remaining = db.query("SELECT MIN(ts) AS lo, COUNT(*) AS c FROM metrics_raw")[0]
    assert remaining["c"] == 180
    assert remaining["lo"] >= old + 3 * BUCKET_S


# ---------------------------------------------------------------- 웜 스토어


def test_warm_export_roundtrip(db: Database, tmp_path: Path, monkeypatch) -> None:
    """Parquet 으로 나간 뒤 DuckDB 로 다시 읽히고, 그 다음에야 SQLite 에서 지워진다."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    import argus.storage.warm as warm_module

    monkeypatch.setattr(warm_module, "warm_dir", lambda: tmp_path / "warm")

    # 이틀 전 하루치. "끝난 날짜"여야 내보내진다.
    start = bucket_of(time.time() - 2 * 86400)
    _seed(db, start, 10)
    Rollup(db, RollupSettings()).run_once()
    before = db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"]
    assert before == 10

    store = warm_module.WarmStore(db, WarmSettings())
    dates = store.exportable_dates()
    assert dates, "이틀 전 날짜가 내보내기 대상이 아니다"
    exported = store.export_pending()
    assert sum(exported.values()) == 10

    # 파일이 실제로 생겼고 DuckDB 로 읽힌다
    assert store.has_partitions()
    count, lo, hi = store.query("SELECT count(*), min(ts_min), max(ts_min) FROM warm")[0]
    assert count == 10
    assert lo == start

    # 내보낸 뒤에는 SQLite 에서 비워진다(중복 보관하지 않는다)
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 0
    # 두 번 내보내지 않는다
    assert store.export_pending() == {}

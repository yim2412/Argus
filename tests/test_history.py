"""핫(SQLite)과 웜(Parquet)을 합쳐 읽는 계층을 고정한다.

여기서 지키는 것은 하나다. **한 날짜는 한 번만 세어진다.**

롤업은 두 곳에 나뉘어 사는데(최근 이틀은 SQLite, 그 이전은 Parquet), 합치는 규칙이
깨져도 **예외가 나지 않는다.** 그날이 조용히 두 배로 세어지고, 고부하 판정과 프로세스
지문 통계가 함께 틀어질 뿐이다. 2026-07-29 에 백필이 이미 내보낸 07-27 을 SQLite 에
되살려 실제로 그 상태가 만들어졌다.

`python -m argus.storage.history` 스모크는 이걸 대신하지 못한다. 실제 DB 에 의존하므로
다른 PC 에서는 돌지 않고, 무엇보다 **중복이 우연히 없는 날에는 통과하면서 아무것도
검증하지 않는다.**
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from argus.config.loader import RollupSettings, WarmSettings
from argus.storage import history
from argus.storage.hot import Database
from argus.storage.rollup import Rollup, bucket_of


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch, db: Database):
    """웜 디렉터리와 핫 DB 경로를 임시 위치로 돌린다."""
    import argus.storage.warm as warm_module

    monkeypatch.setattr(warm_module, "warm_dir", lambda: tmp_path / "warm")
    monkeypatch.setattr(history, "db_path", lambda: db.path)
    return warm_module


def _day_start(days_ago: int) -> int:
    """`days_ago` 일 전 00:00 (로컬)."""
    day = (datetime.now() - timedelta(days=days_ago)).date()
    return int(datetime(day.year, day.month, day.day).timestamp())


def _seed_minutes(database: Database, start: int, minutes: int) -> None:
    rows = []
    for minute in range(minutes):
        for second in range(60):
            ts = start + minute * 60 + second
            rows.append((ts, 20.0, 50.0, 30.0, 0.1))
    database.insert_many(
        "metrics_raw", ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"), rows
    )


def _fold(database: Database) -> None:
    """끝까지 접는다.

    `run_once` 는 `max_buckets_per_run` 만큼만 처리하므로, 며칠 간격을 두고 시드하면
    한 번으로는 최근 구간까지 닿지 않는다.
    """
    rollup = Rollup(database, RollupSettings())
    now = time.time()
    for _ in range(1000):
        if rollup._pending_range(now) is None:  # noqa: SLF001
            return
        rollup.run_once(now)
    raise AssertionError("롤업이 끝나지 않는다")


def _export(database: Database) -> dict:
    from argus.storage.warm import WarmStore

    return WarmStore(database, WarmSettings()).export_pending()


# ---------------------------------------------------------------- 병합 규칙


def test_exported_day_stays_visible(wired, db: Database) -> None:
    """내보낸 날짜는 SQLite 에서 사라져도 계속 보인다.

    이게 안 되면 아무리 오래 돌려도 이틀치밖에 못 본다 — readiness 가 "3일 이상 관측된
    프로세스"를 영원히 0종으로 보고했던 원인이다.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    start = _day_start(2) + 3600
    _seed_minutes(db, start, 10)
    _fold(db)
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 10

    assert _export(db), "이틀 전 날짜가 내보내지지 않았다"
    # SQLite 에서는 비었다
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 0

    cov = history.coverage("metrics")
    assert len(cov) == 1, f"내보낸 날짜가 보이지 않는다: {cov}"
    day = next(iter(cov.values()))
    assert day.buckets == 10
    assert day.source == "warm"


def test_duplicate_day_counted_once(wired, db: Database) -> None:
    """같은 날짜가 양쪽에 있으면 웜만 센다.

    백필이 이미 내보낸 날짜를 SQLite 에 되살리면 이 상태가 된다. 그냥 더하면 그날
    버킷 수가 두 배가 되고, 관측 시간·지문 표본이 함께 부풀려진다.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    start = _day_start(2) + 3600
    _seed_minutes(db, start, 10)
    _fold(db)
    _export(db)

    # 백필이 한 것과 같은 상태를 만든다 — 내보낸 날짜를 SQLite 에 되살린다.
    # **웜(10버킷)과 다른 개수(5버킷)로 되살리는 것이 핵심이다.** 같은 개수면 어느 쪽이
    # 채택됐는지 값으로 구분되지 않아, 규칙이 깨져도 테스트가 통과한다.
    db.insert_many(
        "metrics_1m", ("ts_min", "sample_count"), [(start + i * 60, 60) for i in range(5)]
    )
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 5

    cov = history.coverage("metrics")
    assert len(cov) == 1
    day = next(iter(cov.values()))
    assert day.buckets == 10, f"핫이 웜을 이겼다 — 정본 규칙이 뒤집혔다 ({day.buckets}버킷)"
    assert day.source == "warm"


def test_rollup_range_joins_both_without_overlap(wired, db: Database) -> None:
    """두 계층이 시간순으로, 겹침 없이 이어진다."""
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    # 두 구간을 함께 시드한 뒤 한 번에 접는다. 롤업은 워터마크 이후만 접으므로
    # 접고 나서 과거를 끼워 넣으면 그 구간은 영영 접히지 않는다(실제 수집은 시간순이라
    # 일어나지 않는 상황이다 — `tools/backfill_rollup.py` 참조).
    old = _day_start(2) + 3600
    recent = bucket_of(time.time() - 1800)
    _seed_minutes(db, old, 5)
    _seed_minutes(db, recent, 5)
    _fold(db)

    # 끝난 날짜만 나간다 — 오늘 것은 핫에 남는다.
    assert _export(db), "이틀 전 날짜가 내보내지지 않았다"
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 5

    rows = history.rollup_range(old - 86400)
    stamps = [r["ts_min"] for r in rows]
    assert len(stamps) == 10, f"두 계층이 다 오지 않았다: {len(stamps)}"
    assert stamps == sorted(stamps), "시간순이 아니다"
    assert len(stamps) == len(set(stamps)), "같은 분이 두 번 들어 있다"
    # 컬럼이 유실되면 대시보드가 조용히 빈 그래프를 그린다.
    assert "cpu_mean" in rows[0] and "ts_min" in rows[0]


def test_rollup_range_respects_bounds(wired, db: Database) -> None:
    """구간 밖은 가져오지 않는다."""
    start = bucket_of(time.time() - 7200)
    _seed_minutes(db, start, 10)
    _fold(db)

    rows = history.rollup_range(start + 300, start + 600)
    assert [r["ts_min"] for r in rows] == [start + 300, start + 360, start + 420,
                                            start + 480, start + 540]


# ---------------------------------------------------------------- 관측 시간


def test_short_day_is_not_a_day(wired, db: Database) -> None:
    """짧게만 켜 둔 날은 '하루'로 세지 않는다.

    2시간 켜 둔 날과 12시간 켜 둔 날이 같은 1일이면 착수 판정이 거짓말이 된다.
    """
    # 진행 중 버킷은 접히지 않으므로 시드 끝을 현재에서 충분히 떼어 놓는다.
    start = bucket_of(time.time() - 7200 - 600)
    _seed_minutes(db, start, 120)  # 2시간
    _fold(db)

    cov = history.coverage("metrics")
    assert sum(c.buckets for c in cov.values()) == 120
    assert abs(sum(c.hours for c in cov.values()) - 2.0) < 0.05

    assert history.observed_days("metrics", min_hours=6.0) == []
    assert len(history.observed_days("metrics", min_hours=1.0)) == 1


def test_process_index_sums_buckets_per_day(wired, db: Database) -> None:
    """프로세스별로 날짜와 버킷 수가 함께 나온다.

    며칠 보였는지만으로는 지문을 세울 수 있는지 알 수 없다 — 3일에 걸쳐 보였어도
    매번 5분씩이면 표본이 15개다.
    """
    from argus.storage.rollup import ProcessRollup

    # **날짜 경계를 밟지 않는 시각에서 시작한다.** 예전에는 `time.time() - 7200` 이었는데,
    # 새벽 0~2시에 돌리면 그 2시간 전이 **어제**라 30분치가 이틀로 갈리고 마지막 단언이
    # 깨졌다(2026-08-16 01:3x 실측: `{'08-15': 3, '08-16': 3}`). 테스트가 시계를 타면
    # "어젯밤엔 됐는데"가 생기고, 그 시간에 작업하던 사람은 자기 변경을 의심하게 된다.
    # 어제 정오는 항상 과거이고 30분을 더해도 같은 날이다.
    noon_yesterday = (
        datetime.fromtimestamp(time.time()).replace(hour=12, minute=0, second=0, microsecond=0)
        - timedelta(days=1)
    ).timestamp()
    start = bucket_of(noon_yesterday, 300)
    rows = []
    for bucket in range(6):  # 30분
        for second in range(0, 300, 10):
            rows.append((start + bucket * 300 + second, 100 + bucket, "chrome", 5.0, 100.0, 10))
    db.insert_many(
        "process_metrics", ("ts", "pid", "name", "cpu_percent", "rss_mb", "handles"), rows
    )
    ProcessRollup(db, RollupSettings(), top_n=40).run_once()

    index = history.process_day_index()
    assert "chrome" in index
    assert sum(index["chrome"].values()) == 6, index["chrome"]
    assert len(index["chrome"]) == 1


def test_busy_minutes_also_dedupes(wired, db: Database) -> None:
    """고부하 판정도 같은 병합 규칙을 탄다.

    `coverage` 만 중복을 걸러도 소용없다 — 고부하 분 수가 두 배로 세어지면 유휴 위주의
    날이 '고부하'로 뒤집힌다.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    start = _day_start(2) + 3600
    rows = []
    for minute in range(10):
        for second in range(60):
            # gpu_util 은 롤업이 평균을 내므로 전 초를 같은 값으로 채운다.
            rows.append((start + minute * 60 + second, 20.0, 50.0, 30.0, 0.1))
    db.insert_many(
        "metrics_raw", ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"), rows
    )
    db.insert_many(
        "gpu_metrics",
        ("ts", "gpu_index", "util_percent", "temp_c"),
        [(start + m * 60 + s, 0, 80.0, 60.0) for m in range(10) for s in range(60)],
    )
    _fold(db)
    _export(db)

    # 백필이 되살린 상태. 웜(고부하 10분)과 다른 개수로 넣어 어느 쪽이 채택됐는지 본다.
    db.insert_many(
        "metrics_1m",
        ("ts_min", "sample_count", "gpu_util_mean"),
        [(start + i * 60, 60, 80.0) for i in range(3)],
    )

    busy = history.busy_minutes("gpu_util_mean", 50.0)
    assert sum(busy.values()) == 10, f"핫이 웜을 이겼다 — 고부하 판정이 뒤집힌다: {busy}"


def test_busy_minutes_rejects_unknown_column(wired, db: Database) -> None:
    """컬럼명이 SQL 에 그대로 들어가므로 스키마에 있는 것만 받는다."""
    with pytest.raises(ValueError):
        history.busy_minutes("1=1; DROP TABLE metrics_1m", 0.0)
    with pytest.raises(ValueError):
        history.has_column_data("nope")


def test_has_column_data_sees_warm(wired, db: Database) -> None:
    """웜에만 남은 지표도 '관측됐다'로 답한다.

    GPU 없는 PC 를 가려내는 판정이라, 핫만 보면 이틀 뒤 GPU 가 사라진 것처럼 보인다.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    start = _day_start(2) + 3600
    _seed_minutes(db, start, 5)
    db.insert_many(
        "gpu_metrics",
        ("ts", "gpu_index", "util_percent", "temp_c"),
        [(start + m * 60 + s, 0, 80.0, 60.0) for m in range(5) for s in range(60)],
    )
    _fold(db)
    _export(db)
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 0

    assert history.has_column_data("gpu_util_mean") is True


def test_empty_stores_do_not_raise(wired, db: Database) -> None:
    """데이터가 없어도 답한다. 첫 실행 직후가 가장 잘 깨진다."""
    assert history.coverage("metrics") == {}
    assert history.observed_days("metrics") == []
    assert history.process_day_index() == {}
    assert history.rollup_range(0.0) == []
    assert history.span("metrics") is None
    assert history.busy_minutes("gpu_util_mean", 50.0) == {}
    assert history.has_column_data("gpu_util_mean") is False

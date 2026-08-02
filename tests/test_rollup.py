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


def test_rollup_keeps_gpu_clock(db: Database) -> None:
    """**GPU SM 클럭이 롤업에 남아야 한다.**

    원본(`gpu_metrics`)은 24시간만 보존되므로, 접지 않으면 매일 사라진다. 등급의
    "현재 손실" 축은 며칠치 비교를 요구하는데 그때 과거를 되살릴 방법이 없다.

    `min` 을 함께 두는 이유: 스로틀은 클럭이 **떨어지는** 것이라 평균만 보면 짧고
    깊은 하락이 묻힌다. 여기서도 1,900 이 58초, 1,200 이 2초라 평균은 1,876 이지만
    실제로 물린 지점은 1,200 이다.
    """
    start = bucket_of(time.time() - 3600)
    _seed(db, start, 1)
    db.insert_many(
        "gpu_metrics",
        ("ts", "gpu_index", "util_percent", "temp_c", "clock_sm_mhz"),
        [(start + s, 0, 95.0, 84.0, 1200.0 if s < 2 else 1900.0) for s in range(60)],
    )

    assert Rollup(db, RollupSettings()).run_once() == 1

    row = db.query("SELECT * FROM metrics_1m")[0]
    assert row["gpu_clock_sm_min"] == 1200.0, "가장 깎였을 때가 남지 않는다"
    assert row["gpu_clock_sm_mean"] == pytest.approx(1876.667, abs=0.01)


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
    dates = store.exportable_dates("metrics")
    assert dates, "이틀 전 날짜가 내보내기 대상이 아니다"
    exported = store.export_pending()
    assert sum(exported.values()) == 10

    # 파일이 실제로 생겼고 DuckDB 로 읽힌다
    assert store.has_partitions("metrics")
    count, lo, hi = store.query("SELECT count(*), min(ts_min), max(ts_min) FROM warm")[0]
    assert count == 10
    assert lo == start

    # 내보낸 뒤에는 SQLite 에서 비워진다(중복 보관하지 않는다)
    assert db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"] == 0
    # 두 번 내보내지 않는다
    assert store.export_pending() == {}


def test_warm_keeps_schemas_apart(db: Database, tmp_path: Path, monkeypatch) -> None:
    """지표와 프로세스는 스키마가 다르므로 뷰를 나눠야 한다.

    한 뷰로 묶으면 컬럼이 맞지 않아 조회 자체가 실패한다.
    """
    pytest.importorskip("pyarrow")
    pytest.importorskip("duckdb")

    from argus.storage.rollup import ProcessRollup
    import argus.storage.warm as warm_module

    monkeypatch.setattr(warm_module, "warm_dir", lambda: tmp_path / "warm")

    start = bucket_of(time.time() - 2 * 86400, 300)
    _seed(db, start, 10)
    _seed_processes(db, start, 10)
    Rollup(db, RollupSettings()).run_once()
    ProcessRollup(db, RollupSettings()).run_once()

    store = warm_module.WarmStore(db, WarmSettings())
    exported = store.export_pending()
    assert any(k.endswith("/metrics") for k in exported)
    assert any(k.endswith("/process") for k in exported)
    assert store.has_partitions("metrics") and store.has_partitions("process")

    # 각자의 뷰로 읽힌다
    assert store.query("SELECT count(*) FROM warm")[0][0] == 10
    names = store.query("SELECT DISTINCT name FROM warm_process ORDER BY name")
    assert [n[0] for n in names] == ["chrome", "python"]


# ---------------------------------------------------------------- 프로세스 롤업


def _seed_processes(database: Database, start: int, minutes: int) -> None:
    rows = []
    for minute in range(minutes):
        for second in range(0, 60, 2):
            ts = start + minute * 60 + second
            # chrome 은 프로세스 3개, python 은 1개
            for pid in (10, 11, 12):
                rows.append((ts, pid, "chrome", 5.0, 300.0, 0.0, 1000.0, 200, 20, 0))
            rows.append((ts, 20, "python", 40.0, 100.0, 0.0, 0.0, 50, 5, 1))
    database.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        rows,
    )


def test_process_rollup_sums_across_pids(db: Database) -> None:
    """한 프로그램의 프로세스 여럿은 **시각별로 먼저 합친** 뒤 분위수를 낸다.

    표본을 그냥 모아 분위수를 내면 "개별 프로세스의 p95"가 되는데, 지문에 필요한 것은
    "크롬 전체가 평소 얼마나 쓰나"다. 크롬 탭이 30개면 둘은 30배 다르다.
    """
    from argus.storage.rollup import ProcessRollup

    start = bucket_of(time.time() - 7200, 300)
    _seed_processes(db, start, 5)

    rollup = ProcessRollup(db, RollupSettings(), top_n=10)
    assert rollup.run_once() > 0

    chrome = db.query("SELECT * FROM process_5m WHERE name='chrome' ORDER BY ts_5m")[0]
    assert chrome["pid_count"] == 3
    assert chrome["cpu_p50"] == pytest.approx(15.0), "프로세스 3개 × 5% = 15%"
    assert chrome["rss_p95"] == pytest.approx(900.0)

    python = db.query("SELECT * FROM process_5m WHERE name='python' ORDER BY ts_5m")[0]
    assert python["cpu_p50"] == pytest.approx(40.0)
    assert python["foreground_ratio"] == pytest.approx(1.0)


def test_process_rollup_caps_rows_per_bucket(db: Database) -> None:
    """버킷당 행 수에 상한이 있어야 한다.

    조건 필터로 거르면 프로세스가 500개인 PC 에서 저장량이 예측 불가로 늘어난다.
    배포 대상의 하드웨어를 가정하지 않으려면 상한이 필요하다.
    """
    from argus.storage.rollup import ProcessRollup

    start = bucket_of(time.time() - 7200, 300)
    rows = []
    for second in range(0, 60, 2):
        for index in range(100):  # 프로그램 100개
            rows.append((start + second, 1000 + index, f"prog{index}", float(index), 10.0, 0, 0, 10, 2, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        rows,
    )

    ProcessRollup(db, RollupSettings(), top_n=10).run_once()
    kept = db.query("SELECT COUNT(*) AS c FROM process_5m")[0]["c"]
    # CPU 상위 10 + RSS 상위 10 의 합집합이므로 20 을 넘지 않는다
    assert 10 <= kept <= 20, f"상한이 지켜지지 않았다: {kept}행"


def test_network_rollup_counts_without_storing_addresses(db: Database) -> None:
    """개별 원격 주소를 저장하지 않는다.

    두 이유가 겹친다. 신호가 되지 않고(신규 주소가 시간당 87건 — 소음이다),
    개인정보다(원본은 72시간 뒤 사라지는데 집계가 영구 보관하면 보존 정책을 우회한다).
    필요한 신호인 "단시간 다수 신규 대상"은 개수만으로 잡힌다.
    """
    from argus.storage.rollup import NetworkRollup

    start = bucket_of(time.time() - 7200, 300)
    rows = []
    for tick in range(0, 300, 30):
        for octet in range(5):
            rows.append((start + tick, 10, "chrome", "10.0.0.1", 1000, f"93.184.{octet}.1", 443, "ESTABLISHED", 2))
        rows.append((start + tick, 20, "server", "0.0.0.0", 8080, None, None, "LISTEN", 2))
    db.insert_many(
        "net_connections",
        ("ts", "pid", "name", "laddr", "lport", "raddr", "rport", "status", "family"),
        rows,
    )

    assert NetworkRollup(db, RollupSettings()).run_once() > 0

    # 컬럼 이름이 아니라 **저장된 값**을 본다. 이름은 바꿀 수 있어도 값은 못 속인다.
    stored = " ".join(
        str(value)
        for row in db.query("SELECT * FROM net_activity_5m")
        for value in dict(row).values()
    )
    assert "93.184." not in stored, "집계에 원격 주소가 새어 들어갔다"

    chrome = db.query("SELECT * FROM net_activity_5m WHERE name='chrome'")[0]
    assert chrome["distinct_remotes"] == 5
    assert chrome["distinct_ports"] == 1

    server = db.query("SELECT * FROM net_activity_5m WHERE name='server'")[0]
    assert server["listen_count"] > 0, "LISTEN 은 서비스를 새로 여는 신호라 세어야 한다"


def test_retention_waits_for_network_rollup(db: Database) -> None:
    from argus.storage.rollup import NetworkRollup

    old = bucket_of(time.time() - 100 * 3600, 300)  # network_hours(72) 를 넘긴 시각
    db.insert_many(
        "net_connections",
        ("ts", "pid", "name", "laddr", "lport", "raddr", "rport", "status", "family"),
        [(old + i, 10, "chrome", "10.0.0.1", 1000, "93.184.0.1", 443, "ESTABLISHED", 2)
         for i in range(0, 300, 30)],
    )

    Retention(db, RetentionSettings()).purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM net_connections")[0]["c"] > 0

    NetworkRollup(db, RollupSettings()).run_once()
    Retention(db, RetentionSettings()).purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM net_connections")[0]["c"] == 0
    assert db.query("SELECT COUNT(*) AS c FROM net_activity_5m")[0]["c"] > 0


def test_retention_uses_the_rollup_that_folds_it(db: Database) -> None:
    """`process_metrics` 는 **프로세스 롤업의** 워터마크를 봐야 한다.

    처음에는 워터마크가 하나뿐이었고 그것은 `metrics_1m` 것이었다. 프로세스 데이터는
    1분 롤업이 접지 않는데도 "롤업이 지나갔으니 안전하다"는 이유로 지워지고 있었다 —
    보호 장치가 헛돌았다.
    """
    from argus.storage.rollup import ProcessRollup

    old = bucket_of(time.time() - 40 * 3600, 300)
    _seed_processes(db, old, 5)
    _seed(db, old, 5)

    # 1분 롤업만 돌린다 — 프로세스는 아직 접히지 않았다
    Rollup(db, RollupSettings()).run_once()
    Retention(db, RetentionSettings()).purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"] > 0, (
        "프로세스 롤업이 돌지 않았는데 원본이 지워졌다"
    )

    # 프로세스 롤업이 지나간 뒤에는 지운다
    ProcessRollup(db, RollupSettings()).run_once()
    Retention(db, RetentionSettings()).purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"] == 0
    assert db.query("SELECT COUNT(*) AS c FROM process_5m")[0]["c"] > 0

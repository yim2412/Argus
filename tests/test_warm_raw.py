"""초 단위 원본을 웜에 두고 거기서 리플레이한다.

**왜 이게 필요했나.** 룰은 초 단위 표본에 30초 이상의 지속 조건으로 도는데 원본
보존이 24시간이라, 2026-09-08 의 "부하는 평소와 같은데 신호 0건"을 확인하려던
시점에 이미 원본이 없었다. 결함 주입 구간만 영구 보존되는 구조는 **미탐 조사에
쓸모가 없다** — 미탐은 정의상 아무것도 기록되지 않은 구간이다.

여기 있는 것은 대부분 **조용히 깨지는 종류**다. 파일은 멀쩡히 만들어지고 행 수도
맞는다. 틀어지는 것은 순서·워터마크·보호 여부라 실행 중에는 아무 신호가 없다.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")

from argus.config.loader import RetentionSettings, WarmSettings  # noqa: E402
from argus.eval.replay import Replayer, Window  # noqa: E402
from argus.storage import warm as warm_mod  # noqa: E402
from argus.storage.hot import Database  # noqa: E402
from argus.storage.replay_db import WarmReplayDatabase  # noqa: E402
from argus.storage.retention import Retention  # noqa: E402

DAY = "2026-08-01"
BASE = datetime.fromisoformat(DAY + "T12:00:00").timestamp()
TICKS = 40
PROCS_PER_TICK = 8


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(warm_mod, "warm_dir", lambda: tmp_path / "warm")
    db = Database(tmp_path / "t.db").open()
    settings = WarmSettings()
    yield warm_mod.WarmStore(db, settings), db, settings
    db.close()


def _seed(db: Database) -> None:
    """한 틱에 프로세스 여러 개. **틱당 하나면 순서 문제가 드러나지 않는다.**"""
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "mem_percent", "cpu_per_core"),
        [(BASE + i, float(i), 30.0 + i, None) for i in range(TICKS)],
    )
    db.insert_many(
        "gpu_metrics",
        ("ts", "gpu_index", "util_percent", "temp_c"),
        [(BASE + i, 0, float(i), 60.0) for i in range(TICKS)],
    )
    rows = []
    for i in range(TICKS):
        # pid 를 일부러 내림차순으로 넣는다 — 저장 순서와 pid 순서를 어긋나게 해야
        # "삽입 순서로 읽혀서 우연히 맞았다"를 배제할 수 있다.
        for p in range(PROCS_PER_TICK, 0, -1):
            rows.append(
                (BASE + i, 1000 + p, "proc" + str(p), float(p), 10.0 * p, 0, 0, 5, 2, 0)
            )
    db.insert_many(
        "process_metrics",
        (
            "ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps",
            "io_write_bps", "handles", "threads", "foreground",
        ),
        rows,
    )


def _export_raw(store_tuple) -> None:
    warm_store, _db, _settings = store_tuple
    for kind in warm_mod.RAW_KINDS:
        warm_store.export_date(DAY, kind)


# ------------------------------------------------------------------ 골든 대조
#
# **이 파일에서 가장 중요한 테스트다.** 웜에서 읽은 재생이 핫에서 읽은 재생과 다르면
# 채점이 성립하지 않는다 — 리플레이로 낸 수치가 실시간의 수치가 아니게 된다.


def test_warm_replay_matches_hot_exactly(store) -> None:
    _warm_store, db, _settings = store
    _seed(db)
    _export_raw(store)

    window = Window(BASE - 1, BASE + TICKS + 1)
    hot_obs = list(Replayer(db).stream(window))
    with WarmReplayDatabase(db, [DAY]) as warm_db:
        warm_obs = list(Replayer(warm_db).stream(window))

    assert len(hot_obs) == TICKS, "핫에서 " + str(len(hot_obs)) + "개 — 씨앗이 안 들어갔다"
    assert len(warm_obs) == len(hot_obs), (
        "관측 수가 다르다: 핫 " + str(len(hot_obs)) + " vs 웜 " + str(len(warm_obs))
    )
    for a, b in zip(hot_obs, warm_obs):
        assert a.ts == b.ts
        assert a.metrics == b.metrics, "지표가 다르다"
        assert a.processes == b.processes, (
            "프로세스가 다르다 — 순서까지 같아야 결정론 회귀가 성립한다"
        )
        assert a.gpus == b.gpus, "GPU 가 다르다"


def test_process_order_is_total(store) -> None:
    """같은 `ts` 안의 순서가 정해져 있어야 한다.

    **막지 않았으면 무엇이 일어났을 것인가**: `ORDER BY ts` 만이면 같은 시각의
    수백 개 프로세스 순서를 저장 엔진이 정한다. 2026-09-09 에 실측 2,997개 중
    1개가 그래서 달랐고, 정렬하면 같았다.
    """
    _warm_store, db, _settings = store
    _seed(db)
    observations = list(Replayer(db).stream(Window(BASE - 1, BASE + TICKS + 1)))

    assert observations, "관측이 0개다 — 이 검증은 아무것도 재지 못했다"
    for obs in observations:
        pids = [p.pid for p in obs.processes]
        assert pids == sorted(pids), "프로세스 순서가 정해져 있지 않다: " + str(pids)


# ------------------------------------------------- warm 이 원본을 지우면 안 된다


def test_raw_kinds_are_not_purged_by_warm(store) -> None:
    """**원본 삭제는 `retention` 의 일이다.**

    warm 이 날짜 범위로 지우면 결함 주입 보호·롤업 워터마크를 둘 다 우회한다 —
    보호 중인 주입 구간이 통째로 사라지는 경로다.
    """
    warm_store, db, settings = store
    _seed(db)
    assert settings.purge_after_export is True, "이 테스트는 purge 가 켜진 상태를 전제한다"

    before = db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"]
    warm_store.export_date(DAY, "raw_process")
    after = db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"]

    assert before > 0
    assert after == before, "warm 이 초 단위 원본을 지웠다 — 주입 보호를 우회한다"


def test_rollup_kinds_are_still_purged(store) -> None:
    """반대쪽도 확인한다. 이게 없으면 위 테스트는 purge 가 통째로 죽어도 통과한다."""
    warm_store, db, _settings = store
    db.insert_many(
        "metrics_1m",
        ("ts_min", "sample_count", "cpu_mean"),
        [(BASE + i * 60, 60, float(i)) for i in range(5)],
    )
    warm_store.export_date(DAY, "metrics")
    left = db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"]
    assert left == 0, "집계는 내보낸 뒤 지워져야 한다 — purge 자체가 안 돌고 있다"


# ------------------------------------------------------------------ 워터마크


def test_watermark_is_none_until_every_kind_is_out(store) -> None:
    """하나라도 안 나갔으면 지우면 안 된다.

    앞선 종류 기준으로 지우면 뒤처진 쪽은 영영 못 나간다.
    """
    warm_store, db, _settings = store
    _seed(db)
    assert warm_store.raw_watermark() is None, "아무것도 안 나갔는데 워터마크가 섰다"

    warm_store.export_date(DAY, "raw_metrics")
    assert warm_store.raw_watermark() is None, "한 종류만 나갔는데 워터마크가 섰다"

    warm_store.export_date(DAY, "raw_gpu")
    warm_store.export_date(DAY, "raw_process")
    mark = warm_store.raw_watermark()
    assert mark is not None and mark > BASE, "셋 다 나갔는데 워터마크가 없다"


def test_zero_row_day_still_advances_the_watermark(store) -> None:
    """**행이 0인 날도 '끝났다'로 기록해야 한다.**

    안 하면 그 날짜가 영원히 미완으로 남아 워터마크가 멈추고, 워터마크가 멈추면
    `retention` 이 원본을 영영 안 지워 DB 가 무한히 자란다. GPU 없는 PC 의
    `raw_gpu` 가 정확히 이 경우다 — 실패가 아니라 내보낼 것이 없는 것이다.
    """
    warm_store, db, _settings = store
    db.insert_many(
        "metrics_raw", ("ts", "cpu_total"), [(BASE + i, float(i)) for i in range(3)]
    )
    db.insert_many("process_metrics", ("ts", "pid", "name"), [(BASE, 1, "x")])
    # gpu_metrics 는 비워 둔다 (GPU 없는 PC)
    for kind in warm_mod.RAW_KINDS:
        warm_store.export_date(DAY, kind)

    assert warm_store.raw_watermark() is not None, (
        "GPU 가 없다는 이유로 워터마크가 멈췄다 — 원본이 영영 안 지워진다"
    )


# ------------------------------------------------------------------ retention 관문


def test_retention_waits_for_the_warm_export() -> None:
    gated = Retention.__new__(Retention)
    gated.settings = RetentionSettings()
    gated.gate_on_warm_raw = True
    for table, _keep, rollups in gated._rules():
        if table in ("metrics_raw", "gpu_metrics", "process_metrics"):
            assert warm_mod.RAW_WATERMARK_NAME in rollups, table + " 이 내보내기를 안 기다린다"


def test_retention_does_not_wait_when_raw_export_is_off() -> None:
    """**끄면 관문도 없어야 한다.**

    안 그러면 오지 않을 워터마크를 기다리며 DB 가 무한히 자란다.
    """
    ungated = Retention.__new__(Retention)
    ungated.settings = RetentionSettings()
    ungated.gate_on_warm_raw = False
    for table, _keep, rollups in ungated._rules():
        assert warm_mod.RAW_WATERMARK_NAME not in rollups, (
            table + " 이 내보내기를 기다린다 — export_raw 가 꺼졌는데 삭제가 멈춘다"
        )


# ------------------------------------------------------------ 파티션 보존


def test_prune_keeps_fault_injection_days(store) -> None:
    """결함 주입 날짜는 기한이 지나도 남는다.

    2026-09-09 에 이 보호가 없어서 방금 내보낸 07-29·07-30·08-02 파티션이 30일
    기한에 걸려 **쓰자마자 지워졌다.** 채점의 유일한 근거라 아카이브가 그것부터
    갖고 있어야 한다.
    """
    warm_store, db, settings = store
    _seed(db)
    _export_raw(store)
    settings.raw_retention_days = 1

    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO fault_injections (scenario, ts_start, ts_end, completed) "
            "VALUES ('__t__', ?, ?, 1)",
            (BASE, BASE + 10),
        )
        db.conn.commit()

    assert DAY in warm_store._fault_days(), "주입이 있는 날짜를 못 찾았다"
    # 기한을 확실히 넘긴 '지금'. 보호가 없으면 전부 지워진다.
    warm_store.prune_raw_partitions(now=BASE + 400 * 86400)

    left = sorted(
        p.name for p in (warm_mod.warm_dir() / ("date=" + DAY)).glob("raw_*.parquet")
    )
    assert len(left) == 3, "주입 날짜의 원본이 지워졌다: 남은 것 " + str(left)


def test_prune_removes_ordinary_expired_days(store) -> None:
    """반대쪽. 이게 없으면 위 테스트는 정리가 통째로 죽어도 통과한다."""
    warm_store, db, settings = store
    _seed(db)
    _export_raw(store)
    settings.raw_retention_days = 1

    removed = warm_store.prune_raw_partitions(now=BASE + 400 * 86400)
    assert removed == 3, "기한이 지난 평상시 파티션이 안 지워졌다: " + str(removed)


# --------------------------------------------------------------- 조회 라우팅


def test_tables_without_warm_copies_fall_back_to_hot(store) -> None:
    """`system_events` 는 웜에 없다. 핫으로 넘어가지 않으면 공백 판정이 조용히 빈다."""
    _warm_store, db, _settings = store
    _seed(db)
    _export_raw(store)
    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO system_events (ts, event, gap_seconds) VALUES (?, 'startup', NULL)",
            (BASE + 1,),
        )
        db.conn.commit()

    with WarmReplayDatabase(db, [DAY]) as warm_db:
        rows = warm_db.query(
            "SELECT ts, gap_seconds FROM system_events WHERE ts BETWEEN ? AND ?",
            (BASE - 10, BASE + 100),
        )
    assert len(rows) == 1, "핫 위임이 안 된다: " + str(rows)

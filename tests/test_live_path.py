"""실시간 탐지 경로 — `DetectionComponent` + `ObservationTail` 배선.

**탐지기는 리플레이와 같지만, 실시간 쪽 배선은 따로다.** 2026-09-25 감사(F-028)에서 이 경로의
조건 9줄이 뒤집혀도 전체 테스트(644개)가 초록이었다 — `live.py` 의 `if rows:` 를 뒤집으면 **신호가
한 건도 DB 에 안 쓰이는데** 아무도 몰랐다. 여기서는 격리 DB 와 가짜 시계로 상주가 하는 일을 그대로
돌린다: 기동(예열) → 새 관측을 꼬리로 읽기 → 판정 → `anomaly_signals` 에 쓰기.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from argus.detection import replay_source
from argus.detection.live import DetectionComponent
from argus.storage.hot import Database

T = 1_790_000_000.0
COLUMNS = ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms")


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


@pytest.fixture()
def clock(monkeypatch):
    now = [T]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def _rows(start: float, seconds: int, cpu) -> list[tuple]:
    return [(start + i, cpu(i), min(100.0, cpu(i) + 5), 40.0, 0.5) for i in range(seconds)]


def _normal(i: int) -> float:
    return 10.0 + (i % 7)          # 평소 — 작은 흔들림(퇴화 통계가 되지 않게)


def _signals(db: Database) -> int:
    return db.query("SELECT COUNT(*) AS c FROM anomaly_signals WHERE run_id IS NULL")[0]["c"]


def _component(db: Database) -> DetectionComponent:
    comp = DetectionComponent(db, detector_name="rules", interval_s=10.0, lag_s=5.0)
    comp.setup()
    return comp


def test_live_tick_writes_signal_for_sustained_anomaly(db, clock):
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 1900, 1900, _normal))
    comp = _component(db)                                    # 예열: 평소 30분을 베이스라인으로
    assert comp.detectors, "탐지기가 하나도 안 떴다"
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 4, 75, lambda i: 95.0))
    clock[0] = T + 80
    comp.tick()
    assert _signals(db) >= 1, "70초 넘게 CPU 95% 인데 신호가 DB 에 안 쓰였다"
    assert comp._signals == _signals(db)


def test_live_without_warm_would_absorb_the_anomaly(db, clock, monkeypatch):
    """대조 — 예열이 **왜** 필요한지. 예열이 없으면 창이 이상값만으로 서서 이상이 평소가 된다.
    이게 참이어야 위 테스트가 예열 배선(`live.py` 의 `callable(warm)`)까지 재고 있는 것이다."""
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 1900, 1900, _normal))
    from argus.detection.rules import RuleEngine

    monkeypatch.setattr(RuleEngine, "warm", None, raising=False)
    comp = _component(db)
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 4, 75, lambda i: 95.0))
    clock[0] = T + 80
    comp.tick()
    assert _signals(db) == 0


def test_tail_reads_backlog_in_bounded_batches_without_repeats(db, clock):
    tail = replay_source.ObservationTail(db, lag_s=5.0, start_ts=T)
    db.insert_many("metrics_raw", COLUMNS, _rows(T + 1, 700, _normal))
    clock[0] = T + 800
    first = tail.next_batch()
    second = tail.next_batch()
    assert len(first) == replay_source.MAX_BATCH, f"한 번에 {len(first)}개 — 상한이 안 걸렸다"
    assert len(first) + len(second) == 700
    assert not {o.ts for o in first} & {o.ts for o in second}, "같은 관측을 두 번 줬다"
    assert tail.next_batch() == [], "다 읽었는데 또 준다"


def test_tail_waits_for_lag_and_does_not_reread(db, clock):
    tail = replay_source.ObservationTail(db, lag_s=5.0, start_ts=T)
    db.insert_many("metrics_raw", COLUMNS, _rows(T + 1, 10, _normal))
    clock[0] = T + 3                                          # horizon = T-2 < 커서 → 아직 읽지 않는다
    assert tail.next_batch() == []
    clock[0] = T + 20
    assert len(tail.next_batch()) == 10
    assert tail.next_batch() == []


def test_time_gap_skips_the_backlog_instead_of_judging_it(db, clock):
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 1900, 1900, _normal))
    comp = _component(db)
    # 잠든 동안(가짜) 쌓인 이상 구간 — 복귀 뒤 소급 판정하면 "자고 일어나자마자 알림"이 된다
    db.insert_many("metrics_raw", COLUMNS, _rows(T - 4, 300, lambda i: 95.0))
    clock[0] = T + 310
    comp.on_time_gap(300.0)
    # `approx` 의 기본은 상대 오차(1e-6)라 17억 초대 시각에선 ±1,790초가 같다 — 절대 오차를 준다
    # (처음엔 기본값으로 써서 skip_to_now 를 빼도 초록이었다)
    assert comp._tail.cursor == pytest.approx(T + 305, abs=1.0), "복귀했는데 꼬리가 밀린 구간 앞에 남아 있다"
    comp.tick()
    assert _signals(db) == 0

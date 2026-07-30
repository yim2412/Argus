"""닫히지 못한 주입 라벨이 보존 구간을 무한히 넓히지 않는다.

주입기는 종료할 때 `finally` 에서 `ts_end` 를 기록한다. **전원이 끊기면 그 코드가 돌지
않는다** — 2026-07-30 16:25 에 실제로 그렇게 되어 #47 이 `ts_end IS NULL` 로 남았고, 두
시간 뒤 보호 구간이 2시간 24분으로 자라 그 구간의 정리가 통째로 멈췄다.

행만 보고는 "지금 도는 중"과 "죽은 뒤 남은 것"을 구분할 수 없다. 그래서 계획 길이를
상한으로 쓴다. 여기서 고정하는 것은 세 가지 경우가 **서로 다르게** 처리된다는 것이다.

    진행 중(계획 안)  → `now` 까지 보호       (앞부분이 지워지면 주입이 망한다)
    고아(계획 초과)   → `start + 계획` 에서 멈춤 (무한히 자라지 않는다)
    계획 없음(manual) → `now` 까지 보호       (끝이 열린 것이 의도다)

조용히 깨지는 규칙이다 — 예외가 아니라 구간 길이만 틀어지므로, 상한을 지워도 정리는
정상 종료한다. 다만 지워야 할 데이터가 남는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.config.loader import RetentionSettings
from argus.storage.hot import Database
from argus.storage.retention import Retention

GUARD = 900.0
PLANNED = 720.0


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _label(database: Database, start: float, *, end: float | None, planned: float | None) -> None:
    params = json.dumps({"limits": {"duration_s": planned}}) if planned else None
    with database._lock:  # noqa: SLF001
        database.conn.execute(
            "INSERT INTO fault_injections (scenario, ts_start, ts_end, params, completed) "
            "VALUES ('handle_leak', ?, ?, ?, 0)",
            (start, end, params),
        )
        database.conn.commit()


def _window_end(database: Database, now: float) -> float:
    retention = Retention(database, RetentionSettings(fault_guard_s=GUARD))
    windows = retention._fault_windows(now)  # noqa: SLF001
    assert len(windows) == 1, windows
    # 여유(`fault_guard_s`)를 걷어내 실제 보호 끝 시각을 돌려준다.
    return windows[0][1] - GUARD


def test_running_injection_is_protected_up_to_now(db: Database) -> None:
    """계획 안에서 도는 주입은 `now` 까지 지킨다 — 앞부분이 지워지면 주입이 망한다."""
    now = 1_000_000.0
    start = now - 300.0  # 720초 계획 중 300초 경과
    _label(db, start, end=None, planned=PLANNED)
    assert _window_end(db, now) == pytest.approx(now)


def test_orphaned_label_stops_at_planned_end(db: Database) -> None:
    """전원이 끊겨 닫히지 못한 라벨은 계획 끝에서 멈춘다.

    이것이 #47 의 모양이다. 상한이 없으면 `now` 가 흐를수록 구간이 함께 자란다.
    """
    start = 1_000_000.0
    # 계획(720초)을 한참 넘긴 시점. 실제로는 두 시간 뒤에 발견했다.
    now = start + 8_000.0
    _label(db, start, end=None, planned=PLANNED)

    end = _window_end(db, now)
    assert end == pytest.approx(start + PLANNED)
    # 시간이 더 흘러도 구간이 자라지 않는다 — 이것이 규칙의 핵심이다.
    assert _window_end(db, now + 20_000.0) == pytest.approx(start + PLANNED)


def test_manual_label_without_plan_stays_open(db: Database) -> None:
    """계획이 없는 라벨(`manual`)은 끝이 열려 있는 것이 의도다.

    게임 세션처럼 "언제 끝날지 모르는 구간"에 라벨을 붙이는 용도라, 여기에 상한을 걸면
    그 데이터가 24시간 보존에 걸려 사라진다.
    """
    start = 1_000_000.0
    now = start + 8_000.0
    _label(db, start, end=None, planned=None)
    assert _window_end(db, now) == pytest.approx(now)


def test_closed_label_uses_its_recorded_end(db: Database) -> None:
    """닫힌 라벨은 기록된 `ts_end` 를 그대로 쓴다. 계획보다 짧게 끝나도(중단) 그쪽이 사실이다."""
    start = 1_000_000.0
    actual_end = start + 590.0  # #47 은 계획 720초 중 590초에서 전원이 끊겼다
    _label(db, start, end=actual_end, planned=PLANNED)
    assert _window_end(db, start + 8_000.0) == pytest.approx(actual_end)

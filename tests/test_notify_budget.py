"""알림 예산 — 설정 배선과 "하루"의 경계 (감사 F-022).

처음엔 하루 8건·`warning` 컷이 코드 상수라 사용자가 알림이 많다고 느껴도 YAML 로 줄일 수 없었고,
"하루"가 `now % 86400`(UTC 자정)이라 한국에서는 오전 9시에 예산이 풀렸다.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from argus.config.loader import load_settings
from argus.decide.budget import NotificationBudget
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _notified(db: Database, ts_start: float) -> None:
    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO incidents (ts_start, severity, title, detectors, signal_count, peak_score, notified) "
            "VALUES (?, 'warning', 't', '[\"rules\"]', 1, 0.5, 1)",
            (ts_start,),
        )
        db.conn.commit()


def test_budget_values_come_from_settings(monkeypatch):
    """**기본값이 아닌 값으로** 잰다 — 코드 기본과 YAML 기본이 같으면 배선이 끊겨도 참이다."""
    monkeypatch.setenv("ARGUS_NOTIFY_BUDGET__PER_DAY", "3")
    monkeypatch.setenv("ARGUS_NOTIFY_BUDGET__MIN_SEVERITY", "critical")
    settings = load_settings(use_user_file=False)
    budget = NotificationBudget.from_settings(settings.notify_budget)
    assert (budget.per_day, budget.min_severity) == (3, "critical")
    assert NotificationBudget().per_day != 3, "대조: 코드 기본이 3 이면 이 테스트는 배선을 못 잰다"


def test_main_wires_notify_budget_from_settings():
    """`__main__` 이 예산을 설정에서 만들어 융합에 넘기는지. 여기가 비면 YAML 이 죽은 설정이 된다."""
    source = Path("argus/__main__.py").read_text(encoding="utf-8")
    assert "budget=NotificationBudget.from_settings(settings.notify_budget)" in source


def test_day_starts_at_local_midnight(db):
    if time.localtime().tm_gmtoff == 0:
        pytest.skip("로컬 시각이 UTC 라 로컬 자정과 UTC 자정이 같다 — 이 PC 에서는 경계를 가를 수 없다")
    now = datetime(2026, 9, 25, 3, 0).timestamp()                 # 로컬 새벽 3시
    _notified(db, datetime(2026, 9, 25, 0, 30).timestamp())       # 오늘 — 센다
    _notified(db, datetime(2026, 9, 24, 23, 30).timestamp())      # 어제 — 안 센다
    assert NotificationBudget().used_today(db, now) == 1, "\"오늘\"이 로컬 자정에서 시작하지 않는다"

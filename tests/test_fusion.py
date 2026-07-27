"""신호 융합·억제·예산.

여기서 지키는 것: **같은 일을 두 번 말하지 않는다.** 룰이 30초 지속을 확인하고
발화하면 조건이 유지되는 동안 신호가 계속 나온다. 5분짜리 문제 하나가 신호 수십
개인데, 그걸 그대로 보여 주면 사용자는 알림을 끈다.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from argus.decide.budget import NotificationBudget
from argus.decide.fusion import Fusion, FusionSettings
from argus.decide.suppression import apply_suppression
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _signals(database: Database, rows) -> None:
    database.insert_many(
        "anomaly_signals", ("ts", "detector", "score", "severity", "run_id"), rows
    )


def _metrics(database: Database, start: float, seconds: int, cpu=20.0) -> None:
    database.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start + i, cpu, cpu + 20, 30.0, 0.1) for i in range(seconds)],
    )


# ---------------------------------------------------------------- 융합


def test_many_signals_become_one_incident(db: Database) -> None:
    now = time.time()
    start = now - 600
    _metrics(db, start - 1800, 2400)
    _signals(db, [(start + i * 5, "rules", 0.8, "warning", None) for i in range(30)])

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    assert fusion.run_once(now=now) == 1

    incidents = db.query("SELECT * FROM incidents")
    assert len(incidents) == 1, "신호 30건이 사건 하나로 접혀야 한다"
    assert incidents[0]["signal_count"] == 30
    linked = db.query("SELECT COUNT(*) AS c FROM incident_signals")[0]["c"]
    assert linked == 30, "어떤 신호로 만들어졌는지 추적할 수 있어야 한다"


def test_gap_splits_incidents(db: Database) -> None:
    """신호가 끊기면 다른 사건이다."""
    now = time.time()
    start = now - 1800
    _metrics(db, start - 1800, 3600)
    rows = [(start + i * 5, "rules", 0.8, "warning", None) for i in range(10)]
    # 5분 뒤 다시 발화 (gap_s=120 초과)
    rows += [(start + 300 + i * 5, "rules", 0.8, "warning", None) for i in range(10)]
    _signals(db, rows)

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    assert fusion.run_once(now=now) == 2
    assert db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"] == 2


def test_detector_consensus_escalates(db: Database) -> None:
    """서로 다른 탐지기가 같은 시각에 발화하면 우연일 가능성이 줄어든다.

    같은 탐지기가 여러 번 발화한 것은 합의가 아니라 지속이므로 올리지 않는다.
    """
    now = time.time()
    start = now - 600
    _metrics(db, start - 1800, 2400)

    # 같은 탐지기만 여러 번 → 지속일 뿐이므로 올리지 않는다
    _signals(db, [(start + i, "rules", 0.8, "warning", None) for i in range(5)])
    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    fusion.run_once(now=now)
    assert db.query("SELECT severity FROM incidents")[0]["severity"] == "warning"

    # 다른 탐지기가 같은 구간에 합류하면 올린다
    db.conn.execute("DELETE FROM incidents")
    db.conn.execute("DELETE FROM incident_signals")
    db.conn.commit()
    _signals(db, [(start + 10, "isolation", 0.9, "warning", None)])
    fusion._set_watermark(start - 1)
    fusion.run_once(now=now)

    row = db.query("SELECT * FROM incidents ORDER BY id")[0]
    assert row["severity"] == "critical"
    assert set(json.loads(row["detectors"])) == {"rules", "isolation"}


def test_late_signal_after_close_starts_new_incident(db: Database) -> None:
    """이미 닫힌 사건에는 신호를 되붙이지 않는다.

    닫을 때 귀인을 계산했으므로, 뒤늦게 신호를 더하면 그 계산이 입력과 어긋난다.
    새 사건으로 시작하는 편이 정직하다 — 그래야 리포트가 자기 구간만 설명한다.
    """
    now = time.time()
    start = now - 600
    _metrics(db, start - 1800, 2400)

    _signals(db, [(start + i, "rules", 0.8, "warning", None) for i in range(3)])
    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    fusion.run_once(now=now)
    assert db.query("SELECT ts_end FROM incidents")[0]["ts_end"] is not None

    _signals(db, [(start + 400, "rules", 0.8, "warning", None)])
    fusion._set_watermark(start + 300)
    fusion.run_once(now=now)
    assert db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"] == 2


def test_incident_gets_attribution_on_close(db: Database) -> None:
    """사건을 닫을 때 '왜'가 채워져야 한다.

    Phase 8 과 Phase 9 가 만나는 지점이다. 탐지가 언제를 주고 귀인이 왜를 채운다.
    """
    now = time.time()
    start = now - 900
    end = start + 120

    # 평소 조용하다가 이상 구간에 CPU 가 치솟는다
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1) for i in range(1800)]
        + [(start + i, 95.0, 99.0, 30.0, 0.1) for i in range(120)],
    )
    # 범인: hog. 이상 구간에만 존재한다
    procs = []
    for i in range(0, 200, 2):
        procs.append((start - 200 + i, 10, "idle_app", 1.0, 50.0, 0, 0, 10, 2, 0))
    for i in range(0, 120, 2):
        procs.append((start + i, 10, "idle_app", 1.0, 50.0, 0, 0, 10, 2, 0))
        procs.append((start + i, 20, "hog", 80.0, 200.0, 0, 0, 20, 4, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        procs,
    )
    _signals(db, [(start + i * 5, "rules", 0.9, "warning", None) for i in range(20)])

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    fusion.run_once(now=now)

    row = db.query("SELECT * FROM incidents")[0]
    assert row["ts_end"] is not None, "신호가 끊겼으면 사건이 닫혀야 한다"
    assert row["bottleneck"] == "CPU"
    contributors = json.loads(row["contributors"])
    assert contributors, "원인 후보가 비어 있다"
    assert contributors[0]["name"] == "hog", f"1위가 hog 가 아니다: {contributors[0]}"
    assert "hog" in row["title"]
    assert row["explanation_md"] and "CPU" in row["explanation_md"]


def test_close_without_metrics_does_not_invent(db: Database) -> None:
    """원본이 없으면 설명을 지어내지 않는다."""
    now = time.time()
    start = now - 600
    _signals(db, [(start + i, "rules", 0.8, "warning", None) for i in range(3)])

    fusion = Fusion(db)
    fusion._set_watermark(start - 1)
    fusion.run_once(now=now)

    row = db.query("SELECT * FROM incidents")[0]
    assert row["ts_end"] is not None
    assert row["bottleneck"] is None
    assert row["title"] == "관측 없음"


# ---------------------------------------------------------------- 억제


def test_suppression_marks_but_keeps(db: Database) -> None:
    """억제한 사건을 지우지 않는다 — 왜 안 알렸는지 설명할 수 있어야 한다."""
    now = time.time()
    db.insert_many(
        "incidents",
        ("ts_start", "ts_end", "severity", "title"),
        [(now - 600, now - 300, "critical", "디스크 병목"),
         (now - 500, now - 400, "warning", "크롬 CPU 높음")],
    )
    minor = db.query("SELECT id FROM incidents WHERE severity='warning'")[0]["id"]
    major = db.query("SELECT id FROM incidents WHERE severity='critical'")[0]["id"]

    assert apply_suppression(db, minor) == major
    row = db.query("SELECT * FROM incidents WHERE id = ?", (minor,))[0]
    assert row["suppressed_by"] == major
    assert db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"] == 2, "지우면 안 된다"


def test_suppression_ignores_non_overlapping(db: Database) -> None:
    now = time.time()
    db.insert_many(
        "incidents",
        ("ts_start", "ts_end", "severity", "title"),
        [(now - 6000, now - 5000, "critical", "옛날 일"),
         (now - 500, now - 400, "warning", "지금 일")],
    )
    minor = db.query("SELECT id FROM incidents WHERE severity='warning'")[0]["id"]
    assert apply_suppression(db, minor) is None


# ---------------------------------------------------------------- 예산


def test_budget_raises_bar_when_spent(db: Database) -> None:
    """예산을 다 쓰면 조용해지는 게 아니라 기준을 올린다.

    진짜 중요한 일이 예산 때문에 묻히면 예산 자체가 위험해진다.
    """
    now = time.time()
    day_start = now - (now % 86400)
    budget = NotificationBudget(per_day=3)

    db.insert_many(
        "incidents",
        ("ts_start", "ts_end", "severity", "title", "notified"),
        [(day_start + i * 60, day_start + i * 60 + 10, "warning", "x", 1) for i in range(3)],
    )
    assert budget.used_today(db, now) == 3
    assert budget.effective_severity(db, now) == "critical"

    # warning 은 더 이상 통과하지 못한다
    assert not budget.decide(db, {"severity": "warning"}, now).notify
    # critical 은 여전히 통과한다
    assert budget.decide(db, {"severity": "critical"}, now).notify


def test_budget_records_the_reason(db: Database) -> None:
    """안 보낸 이유를 남긴다. 조용히 사라지면 아무도 설명할 수 없다."""
    now = time.time()
    db.insert_many(
        "incidents", ("ts_start", "ts_end", "severity", "title"), [(now - 60, now, "info", "x")]
    )
    incident_id = db.query("SELECT id FROM incidents")[0]["id"]
    budget = NotificationBudget()

    decision = budget.decide(db, {"severity": "info"}, now)
    budget.record(db, incident_id, decision)

    row = db.query("SELECT * FROM incidents WHERE id = ?", (incident_id,))[0]
    assert row["notified"] == 0
    assert row["notify_skipped"] and "info" in row["notify_skipped"]


def test_suppressed_incident_is_never_notified(db: Database) -> None:
    now = time.time()
    budget = NotificationBudget()
    decision = budget.decide(db, {"severity": "critical", "suppressed_by": 1}, now)
    assert not decision.notify
    assert "묻힘" in decision.reason

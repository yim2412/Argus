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


def test_first_run_persists_watermark(db: Database) -> None:
    """첫 실행에서 워터마크를 저장하지 않으면 융합이 영원히 제자리가 된다.

    `run_once` 는 `end = now - lag` 가 `start` 보다 커야 진행하는데, 첫 실행이
    `time.time()` 을 반환만 하고 저장하지 않으면 다음 틱에서 또 새 `now` 를 받는다.
    실측에서 6분 동안 신호 3건이 쌓이는 사이 한 번도 진행하지 못했다.

    리플레이 테스트가 이 경로를 못 잡은 이유: 전부 `_set_watermark()` 로 시작점을
    명시하고 시작했다. **실시간에만 있는 경로였다.**
    """
    from argus.decide.fusion import WATERMARK_KEY

    fusion = Fusion(db)
    assert db.get_meta(WATERMARK_KEY) is None

    first = fusion.watermark()
    stored = db.get_meta(WATERMARK_KEY)
    assert stored is not None, "첫 호출이 워터마크를 저장하지 않았다"
    assert float(stored) == first

    # 두 번째 호출은 저장된 값을 그대로 돌려줘야 한다(새 now 가 아니라)
    assert fusion.watermark() == first


def test_fusion_advances_across_ticks(db: Database) -> None:
    """틱을 거듭하면 워터마크가 실제로 전진해야 한다."""
    fusion = Fusion(db)
    base = time.time() - 600
    fusion._set_watermark(base)

    fusion.run_once(now=base + 100)
    after_first = fusion.watermark()
    assert after_first > base

    fusion.run_once(now=base + 200)
    assert fusion.watermark() > after_first


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


# --------------------------------------------------------------- 귀인 정직성


def test_gpu_bottleneck_is_marked_unattributable() -> None:
    """GPU·발열은 프로세스별 사용량을 알 수 없다. 그 사실이 값에 실려야 한다."""
    from argus.detection.baseline import BaselineSet
    from argus.explain.bottleneck import classify

    baselines = BaselineSet()
    thermal = classify(
        {"gpu_temp_c": 86.0, "gpu_throttle_reason": "SW_THERMAL", "gpu_temp": 86.0},
        baselines,
    )
    assert thermal.kind == "THERMAL"
    assert not thermal.attributable
    # 근거에 온도가 들어가야 제목이 "스로틀 사유에 THERMAL" 같은 동어반복이 되지 않는다.
    assert "86" in " ".join(thermal.evidence)

    cpu = classify({"cpu_total": 95.0}, baselines)
    assert cpu.kind == "CPU" and cpu.attributable


def test_unattributable_report_does_not_name_a_culprit() -> None:
    """실측에서 "발열 스로틀링 — svchost 19%" 가 나왔다. svchost 는 CPU 를 2% 썼을 뿐이고
    GPU 를 태운 것은 게임이었다. 모르는 것을 아는 척하면 사용자가 엉뚱한 곳을 고친다."""
    from argus.explain.attribution import Contributor
    from argus.explain.bottleneck import Bottleneck
    from argus.explain.report import build_incident, render

    bottleneck = Bottleneck("THERMAL", 0.7, ["GPU 86°C 열 스로틀링"], "cpu", attributable=False)
    contributors = [Contributor(name="svchost", share=0.19, before=0.0, after=2.19, pids={1904})]
    report = build_incident(0.0, 60.0, bottleneck, contributors, triggers=["GPU 열 스로틀링"])

    text = render(report, "cpu")
    assert "원인 후보:" not in text
    assert "특정할 수 없습니다" in text
    assert "발화한 룰: GPU 열 스로틀링" in text


# ------------------------------------------------------- 방아쇠와 병목의 일치

# 실측 사건 7 (2026-07-28 06:35). GPU 온도 룰이 열었는데 제목이 "CPU 병목 —
# deltaforceclient 53%" 로 나가 **실제로 발송됐다.** 게임 중에는 CPU 가 늘 70% 를 넘어
# CPU 점수가 THERMAL 을 근소하게 앞선다.
_INCIDENT_7_PEAK = {
    "cpu_total": 73.0,
    "gpu_temp": 93.0,
    "gpu_temp_c": 93.0,
    "gpu_throttle_reason": "SW_THERMAL",
}


def _hot_baselines():
    """게임 중처럼 CPU 가 평소보다 크게 높은 상태의 베이스라인."""
    from argus.detection.baseline import BaselineSet

    baselines = BaselineSet(window_s=1800.0, min_samples=10)
    for i in range(120):
        baselines.observe(float(i), {"cpu_total": 15.0 + (i % 5)})
    return baselines


def test_trigger_metric_decides_the_bottleneck() -> None:
    """방아쇠가 GPU 온도였으면 CPU 가 조금 더 높아도 발열로 보고한다."""
    from argus.explain.bottleneck import classify

    baselines = _hot_baselines()

    blind = classify(_INCIDENT_7_PEAK, baselines)
    assert blind.kind == "CPU"  # 수정 전 동작 — 이것이 잘못 발송된 알림이었다

    aware = classify(_INCIDENT_7_PEAK, baselines, trigger_metrics=["gpu_temp_c"])
    assert aware.kind == "THERMAL"
    assert aware.overridden_from is None
    assert aware.trigger_kinds == ("THERMAL",)


def test_overwhelming_evidence_still_overrides_the_trigger() -> None:
    """방아쇠 우선이 '방아쇠 절대 복종'은 아니다. 근거가 압도적이면 뒤집되 밝힌다."""
    from argus.explain.bottleneck import classify

    baselines = _hot_baselines()
    # 메모리 룰이 열었지만 구간에서 메모리 근거는 약하고 디스크가 확실히 막혔다.
    peak = {
        "cpu_total": 20.0,
        "mem_percent": 62.0,
        "disk_resp_ms": 40.0,
        "disk_queue": 6.0,
    }
    for i in range(120):
        baselines.observe(float(200 + i), {"mem_percent": 55.0, "disk_resp_ms": 1.0})

    result = classify(peak, baselines, trigger_metrics=["mem_percent"])
    assert result.kind == "IO"
    assert result.overridden_from == "MEMORY"


def test_override_is_disclosed_in_the_report() -> None:
    from argus.explain.bottleneck import Bottleneck
    from argus.explain.report import build_incident, render

    bottleneck = Bottleneck(
        "IO", 0.8, ["디스크 응답 40.0ms"], "io_write", overridden_from="MEMORY"
    )
    text = render(build_incident(0.0, 60.0, bottleneck, [], triggers=["메모리 이상 증가"]))
    assert "메모리 압박" in text and "디스크 IO 병목" in text


def test_unknown_trigger_metric_falls_back_to_metrics() -> None:
    """사용자 룰이 우리가 모르는 지표를 보더라도 판정이 죽지 않는다."""
    from argus.explain.bottleneck import classify

    result = classify(
        {"cpu_total": 95.0}, _hot_baselines(), trigger_metrics=["뭔가_새로운_지표"]
    )
    assert result.kind == "CPU"
    assert result.trigger_kinds == ()

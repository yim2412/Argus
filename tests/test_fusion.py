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
from argus.config.loader import BottleneckSettings, IncidentSettings
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


# ---------------------------------------------------------------- 알림 발송
#
# **GUI 를 띄우지 않는다.** 풍선이 실제로 화면에 뜨는지는 사람이 봐야 하는 것이고,
# 마우스를 움직이는 자동화는 쓰지 않는다(CLAUDE.md). 여기서 고정하는 것은 "누가
# 언제 불리는가"이고, 그건 가짜 전달자로 전부 확인된다.


class _FakeNotifier:
    """`notify(title, message, severity, incident_id=None) -> bool` 만 있으면 된다."""

    def __init__(self, *, fail: bool = False, returns: bool = True) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.incident_ids: list[int | None] = []
        self.fail = fail
        self.returns = returns

    def notify(
        self,
        title: str,
        message: str,
        severity: str = "warning",
        incident_id: int | None = None,
    ) -> bool:
        self.calls.append((title, message, severity))
        self.incident_ids.append(incident_id)
        if self.fail:
            raise RuntimeError("알림 채널이 죽었다")
        return self.returns


def _one_incident(database: Database, now: float) -> None:
    start = now - 600
    _metrics(database, start - 1800, 2400)
    _signals(database, [(start + i * 5, "rules", 0.8, "warning", None) for i in range(30)])


def test_notification_is_not_sent_while_disabled(db: Database) -> None:
    """**판정과 발송은 별개다.** `notified` 는 항상 판정하고 발송만 설정으로 막는다.

    이 분리가 없으면 "알림을 켜면 몇 건이 올 것인가"를 켜기 전에 잴 수 없다.
    알림은 되돌릴 수 없으므로(오탐 3번이면 사용자는 끈다) 먼저 재고 켜야 한다.
    """
    now = time.time()
    _one_incident(db, now)
    notifier = _FakeNotifier()

    fusion = Fusion(db, FusionSettings(notify_enabled=False), notifier=notifier)
    fusion._set_watermark(now - 601)
    fusion.run_once(now=now)

    assert notifier.calls == [], "발송이 꺼져 있는데 알림을 보냈다"
    notified = db.query("SELECT notified FROM incidents")[0]["notified"]
    assert notified == 1, "발송이 꺼졌다고 판정까지 멈추면 알림량을 미리 잴 수 없다"


def test_notification_is_sent_when_enabled(db: Database) -> None:
    now = time.time()
    _one_incident(db, now)
    notifier = _FakeNotifier()

    fusion = Fusion(db, FusionSettings(notify_enabled=True), notifier=notifier)
    fusion._set_watermark(now - 601)
    fusion.run_once(now=now)

    assert len(notifier.calls) == 1, f"알림이 {len(notifier.calls)}건 — 사건당 한 번이어야 한다"
    title, message, severity = notifier.calls[0]
    assert title and title != "분석 중", f"제목이 비었거나 미완성이다: {title!r}"
    assert message, "본문이 비었다"
    assert severity in {"info", "warning", "critical"}


def test_notification_carries_the_incident_it_is_about(db: Database) -> None:
    """**알림에 사건 id 가 실려야 풍선을 눌렀을 때 갈 곳이 있다.**

    없으면 사용자는 창을 열고 목록에서 그 시각을 손으로 찾아야 한다. 실제로 14일
    동안 피드백이 0건이었고, 라벨이 없으니 문턱을 고칠 근거도 없었다.
    **이 배선이 끊겨도 알림은 그대로 뜬다** — 조용히 되돌아가는 자리다.
    """
    now = time.time()
    _one_incident(db, now)
    notifier = _FakeNotifier()

    fusion = Fusion(db, FusionSettings(notify_enabled=True), notifier=notifier)
    fusion._set_watermark(now - 601)
    fusion.run_once(now=now)

    stored = db.query("SELECT id FROM incidents ORDER BY id")[0]["id"]
    assert notifier.incident_ids == [stored], (
        f"알림이 가리키는 사건이 저장된 사건과 다르다: {notifier.incident_ids} vs {stored}"
    )


def test_notifier_failure_does_not_break_fusion(db: Database) -> None:
    """**알림 실패가 융합을 죽이면 사건 기록이 통째로 멈춘다.**

    탐지가 알림보다 중요하다 — 알림은 못 받아도 나중에 대시보드에서 볼 수 있지만,
    사건이 기록되지 않으면 그 시간대는 영영 설명할 수 없다.
    """
    now = time.time()
    _one_incident(db, now)
    notifier = _FakeNotifier(fail=True)

    fusion = Fusion(db, FusionSettings(notify_enabled=True), notifier=notifier)
    fusion._set_watermark(now - 601)
    fusion.run_once(now=now)  # 예외가 새어 나오면 여기서 실패한다

    row = db.query("SELECT notified, ts_end FROM incidents")[0]
    assert row["notified"] == 1 and row["ts_end"] is not None, "알림 실패가 사건 기록을 막았다"


def test_notifier_absent_is_safe(db: Database) -> None:
    """트레이가 꺼졌거나 등록에 실패한 경우. 발송만 없고 나머지는 그대로 돈다."""
    now = time.time()
    _one_incident(db, now)

    fusion = Fusion(db, FusionSettings(notify_enabled=True), notifier=None)
    fusion._set_watermark(now - 601)
    fusion.run_once(now=now)

    assert db.query("SELECT notified FROM incidents")[0]["notified"] == 1


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


def test_override_requires_both_ratio_and_margin() -> None:
    """방아쇠를 뒤집는 문턱을 값으로 고정한다.

    뒤집기 조건은 배수(1.5)와 절대차(0.3) 를 **둘 다** 넘는 것이다. 문턱이 낮아지면
    0.1 대 0.2 같은 근거 없는 차이로 병목이 뒤집혀 사용자가 엉뚱한 곳을 고치고,
    높아지면 지표가 압도적일 때도 방아쇠가 지목한 자원을 계속 들고 있게 된다.
    어느 쪽도 예외를 내지 않으므로 값으로 못 박아 둔다.
    """
    from argus.explain.bottleneck import _choose

    # 배수만 모자란 경우(1.4 < 1.5). 절대차 0.4 는 충족한다.
    assert _choose({"DISK": 1.4, "CPU": 1.0}, "DISK", ("CPU",), BottleneckSettings()) == ("CPU", None)
    # 배수를 넘고 절대차도 넘으면 뒤집고, 원래 자원을 남긴다.
    assert _choose({"DISK": 1.6, "CPU": 1.0}, "DISK", ("CPU",), BottleneckSettings()) == ("DISK", "CPU")
    # 배수는 넉넉하지만 절대차가 모자란 경우(0.2 < 0.3).
    assert _choose({"DISK": 0.5, "CPU": 0.3}, "DISK", ("CPU",), BottleneckSettings()) == ("CPU", None)


def test_override_is_not_needed_when_trigger_already_agrees() -> None:
    from argus.explain.bottleneck import _choose

    assert _choose({"CPU": 1.0, "DISK": 0.2}, "CPU", ("CPU",), BottleneckSettings()) == ("CPU", None)


def test_no_trigger_evidence_keeps_the_trigger_on_record() -> None:
    """방아쇠 자원의 지표 근거가 없어도 다른 답을 냈다는 사실은 남는다."""
    from argus.explain.bottleneck import _choose

    assert _choose({"DISK": 0.9}, "DISK", ("CPU",), BottleneckSettings()) == ("DISK", "CPU")


def _ramp_before(database: Database, ts_start: float, *, elevated_s: int) -> None:
    """베이스라인 1800초 + 그중 마지막 `elevated_s` 초가 높은 구간.

    조용한 구간을 18/22 로 흔드는 이유: 값이 내내 같으면 σ 가 0 이라 z 판정이
    성립하지 않아(`degenerate`) 경계 보정이 아예 돌지 않는다.
    """
    rows = []
    for i in range(1800):
        ts = ts_start - 1800 + i
        elevated = i >= 1800 - elevated_s
        cpu = 90.0 if elevated else (18.0 if i % 2 else 22.0)
        rows.append((ts, cpu, cpu + 5, 30.0, 0.1))
    database.insert_many(
        "metrics_raw", ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"), rows
    )


def test_bound_refinement_stops_at_the_before_cap(db: Database) -> None:
    """앞쪽 경계 보정에 상한이 걸리는지 값으로 고정한다.

    상한이 없으면 오래 높은 지표 하나가 사건을 30분까지 늘려 다른 사건을 삼킨다
    (실측에서 4분짜리가 23분 46초가 됐다). 반대로 상한이 너무 좁으면 탐지 지연을
    되돌리지 못해 원인이 결과보다 늦게 오른 것처럼 보인다.

    여기서는 이상이 신호 400초 전부터 있었는데 상한이 300초다. 보정된 시작은
    300초 이전으로 갈 수 없고, 동시에 상한 근처까지는 실제로 당겨져야 한다.
    """
    from argus.decide.fusion import _refine_bounds

    ts_start = 1_700_000_000.0
    ts_end = ts_start + 240.0
    _ramp_before(db, ts_start, elevated_s=400)

    refined_start, _ = _refine_bounds(db, ts_start, ts_end, IncidentSettings())

    assert refined_start >= ts_start - 300.0 - 1e-6, (
        f"상한을 넘어 {ts_start - refined_start:.0f}초까지 당겨졌다"
    )
    assert refined_start <= ts_start - 290.0, (
        f"상한 근처까지 당기지 못했다: {ts_start - refined_start:.0f}초"
    )


# ------------------------------------------------- 탐지기가 본 자원의 전달 (8번)


def _leak_scene(db: Database, start: float, *, leak_name: str = "python") -> float:
    """전역 지표는 조용하고 한 프로세스의 핸들만 늘어나는 구간.

    **핸들 누수는 전역 지표에 드러나지 않는다.** 그래서 병목 분류가 `NONE` 을 내고,
    고치기 전에는 `NONE` → `cpu` 매핑 때문에 "구간에 CPU 를 많이 쓴 무관한 프로세스"가
    원인으로 발표됐다. 여기서 `cpu_eater` 가 그 무관한 프로세스 역할이다 —
    **고쳐지지 않았다면 이 프로세스가 1위로 뽑힌다.**
    """
    end = start + 120
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1) for i in range(1800)]
        + [(start + i, 16.0 + (i % 3), 31.0, 30.0, 0.1) for i in range(120)],
    )
    procs = []
    for i in range(0, 200, 2):  # 비교 창 — 둘 다 평소 상태로 존재한다
        procs.append((start - 200 + i, 10, leak_name, 2.0, 50.0, 0, 0, 460, 4, 0))
        procs.append((start - 200 + i, 20, "cpu_eater", 40.0, 100.0, 0, 0, 300, 8, 0))
    for i in range(0, 120, 2):
        # 누수: 핸들이 460 → 3,800 으로. CPU 는 거의 안 쓴다.
        procs.append((start + i, 10, leak_name, 2.0, 52.0, 0, 0, 460 + i * 28, 4, 0))
        # 무관한 프로세스: CPU 를 많이 쓰지만 핸들은 그대로다.
        procs.append((start + i, 20, "cpu_eater", 75.0, 100.0, 0, 0, 300, 8, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        procs,
    )
    return end


def _leak_signal(db: Database, ts: float, *, score: float = 0.6, metric: str = "handles",
                 process: str = "python", explain: str = "", duration_s: float = 120.0) -> None:
    features = {
        "rule": "핸들 누수", "rules": ["핸들 누수"], "process": process, "pid": 10,
        "metric": metric, "duration_s": duration_s,
        "explain": explain or f"{process} (PID 10) 핸들 460 → 3,800개 (8.3배, 2분간 줄지 않음)",
    }
    db.insert_many(
        "anomaly_signals", ("ts", "detector", "score", "severity", "features", "run_id"),
        [(ts, "procleak", score, "warning", json.dumps(features, ensure_ascii=False), None)],
    )


def _run(db: Database, start: float, now: float) -> dict:
    fusion = Fusion(db)
    fusion._set_watermark(start - 1)  # noqa: SLF001
    fusion.run_once(now=now)
    return dict(db.query("SELECT * FROM incidents ORDER BY id DESC LIMIT 1")[0])


def test_leak_incident_names_the_leaking_process(db: Database) -> None:
    """탐지기가 `handles` 를 봤다고 말했으면 핸들로 귀인해야 한다.

    2026-07-30 실측: 주입 4건이 모두 엉뚱한 프로세스를 지목했다. 병목이 `NONE` 일 때
    `cpu` 로 분해했기 때문이다. 이 테스트는 그 조건을 그대로 만든다 —
    `cpu_eater` 가 CPU 1위이고 `python` 이 핸들 1위다.
    """
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    for i in range(20):
        _leak_signal(db, start + i * 5)

    row = _run(db, start, now)
    contributors = json.loads(row["contributors"])
    assert contributors, "원인 후보가 비어 있다"
    assert contributors[0]["name"] == "python", (
        f"핸들 누수인데 1위가 python 이 아니다: {[c['name'] for c in contributors[:3]]}"
    )


def test_leak_incident_title_uses_the_detector_sentence(db: Database) -> None:
    """제목은 "병목 없음" 이 아니라 탐지기가 만든 문장이어야 한다.

    탐지가 아니라 설명이 산출물이다(CLAUDE.md). 문장은 이미 만들어져 있었고
    버려지고 있었을 뿐이다.
    """
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    for i in range(20):
        _leak_signal(db, start + i * 5)

    row = _run(db, start, now)
    assert "병목 없음" not in row["title"], f"제목이 그대로다: {row['title']}"
    assert "핸들" in row["title"] and "python" in row["title"], f"제목: {row['title']}"

    # **제목과 기여자가 같은 프로세스를 가리켜야 한다.** 제목은 탐지기 문장에서,
    # 기여자는 귀인에서 따로 오므로 두 경로가 갈라질 수 있다 — 그러면 사용자는
    # "python 핸들 누수"라는 제목 아래 cpu_eater 가 1위인 리포트를 읽는다.
    contributors = json.loads(row["contributors"])
    assert contributors[0]["name"] == "python", (
        f"제목은 python 인데 1위는 {contributors[0]['name']} 다 — 두 경로가 갈라졌다"
    )


def test_concrete_bottleneck_is_not_overridden_by_detector(db: Database) -> None:
    """전역 지표가 증상을 보이면 그쪽을 유지한다.

    탐지기 주장이 항상 이기면 CPU 가 실제로 막힌 구간에서도 프로세스 하나의 내부
    사정으로 제목이 바뀐다. 사용자가 느낀 것은 전역 증상이다.
    """
    now = time.time()
    start = now - 900
    # CPU 가 실제로 치솟는 구간
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        [(start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1) for i in range(1800)]
        + [(start + i, 95.0, 99.0, 30.0, 0.1) for i in range(120)],
    )
    procs = []
    for i in range(0, 200, 2):
        procs.append((start - 200 + i, 20, "cpu_eater", 5.0, 100.0, 0, 0, 300, 8, 0))
    for i in range(0, 120, 2):
        procs.append((start + i, 20, "cpu_eater", 80.0, 100.0, 0, 0, 300, 8, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        procs,
    )
    for i in range(20):
        _leak_signal(db, start + i * 5)

    row = _run(db, start, now)
    assert row["bottleneck"] == "CPU", f"병목이 CPU 가 아니다: {row['bottleneck']}"
    assert "CPU" in row["title"], f"제목이 병목을 버렸다: {row['title']}"


def test_highest_scoring_detector_claim_wins(db: Database) -> None:
    """탐지기가 여럿이면 점수가 큰 쪽의 자원을 쓴다."""
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    for i in range(20):
        _leak_signal(db, start + i * 5, score=0.3, metric="rss_mb", process="mem_hog",
                     explain="mem_hog 메모리 증가")
    _leak_signal(db, start + 7, score=0.95)  # handles, python — 점수가 더 높다

    row = _run(db, start, now)
    assert "python" in row["title"], f"점수가 낮은 주장이 이겼다: {row['title']}"


def test_none_bottleneck_without_claim_does_not_name_a_culprit(db: Database) -> None:
    """탐지기 주장이 없으면 "병목 없음" 에 프로세스를 붙이지 않는다.

    전역 지표에서 아무것도 못 찾은 상태로 CPU 1위를 원인이라 부르면, 구간에 CPU 를
    많이 쓴 무관한 프로세스가 범인이 된다.
    """
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    # 자원을 말하지 않는 신호(룰 엔진)만 있다
    _signals(db, [(start + i * 5, "rules", 0.5, "info", None) for i in range(20)])

    row = _run(db, start, now)
    assert row["bottleneck"] == "NONE", f"병목: {row['bottleneck']}"
    assert "cpu_eater" not in row["title"], f"무관한 프로세스를 지목했다: {row['title']}"


def test_leak_window_is_extended_by_detector_duration(db: Database) -> None:
    """전역 지표가 조용하면 경계 보정이 실패한다 — 탐지기가 센 지속 시간으로 늘린다.

    2026-07-30 실측: 12분짜리 주입이 **18초**로 기록됐다. `_refine_bounds` 는 전역
    지표만 보고(`_BOUND_METRICS`), 핸들 누수는 전역 지표를 움직이지 않기 때문이다.
    그 18초 창에서 귀인하면 누수 프로세스의 증가분도 18초분뿐이라, 마침 그때 뜬 다른
    프로세스가 1위를 가져간다(주입 4건 중 2건이 그랬다).
    """
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    # 신호를 짧게만 둔다 — 쿨다운 때문에 실제로 이렇게 온다.
    _leak_signal(db, start + 60, duration_s=600.0)
    _leak_signal(db, start + 64, duration_s=600.0)

    row = _run(db, start, now)
    claimed_start = start + 60 - 600.0
    assert row["ts_start"] <= claimed_start + 1.0, (
        f"구간이 늘어나지 않았다: 시작 {row['ts_start'] - start:.0f}초 "
        f"(탐지기 주장 {claimed_start - start:.0f}초)"
    )


def test_leak_window_is_not_shrunk_by_detector_duration(db: Database) -> None:
    """이미 더 넓게 잡힌 구간을 탐지기 주장이 좁히지는 않는다."""
    now = time.time()
    start = now - 900
    _leak_scene(db, start)
    for i in range(20):
        _leak_signal(db, start + i * 5, duration_s=1.0)  # 아주 짧게 주장한다

    row = _run(db, start, now)
    assert row["ts_start"] <= start + 5, f"구간이 좁아졌다: {row['ts_start'] - start:.0f}초"


# ---------------------------------------- 경합 분류가 탐지기 주장을 덮던 것 (15번)


def _leak_scene_with_contention(db: Database, start: float) -> float:
    """`_leak_scene` 과 같되, 구간 안에 컨텍스트 스위치 스파이크가 하나 있다.

    2026-07-30 배치의 모양이다. 12분짜리 구간에서 분류기는 **최악 시점 한 점**을 보는데,
    그 한 점이 유튜브·크롬의 잡음이었다. 실측에서 주입 구간의 컨텍스트 스위치 중앙값은
    오히려 더 낮았다(28,431/s vs 구간 밖 103,580/s) — 경합은 주입과 인과가 없었는데도
    `CONTENTION` 이 나오면서 탐지기 주장이 통째로 버려졌다.
    """
    end = start + 120
    base = [
        (start - 1800 + i, 15.0 + (i % 3), 30.0, 30.0, 0.1, 20_000.0 + (i % 5) * 100)
        for i in range(1800)
    ]
    during = [
        (start + i, 16.0 + (i % 3), 31.0, 30.0, 0.1, 20_000.0 + (i % 5) * 100)
        for i in range(120)
    ]
    # 무관한 스파이크 한 점. **CPU 도 함께 올려야 이 행이 `peak` 으로 뽑힌다** —
    # `_peak_and_baselines` 는 `max(rows, key=cpu_total)` 로 최악 시점 하나를 고르고
    # 분류기는 그 행만 본다. 30% 는 `CONTENTION` 조건(`cpu < 60`)을 지키면서 CPU 병목
    # 조건(`cpu >= 40`)에는 닿지 않는 값이다. 긴 구간에서 무관한 스파이크가 분류를
    # 통째로 결정하는 구조가 여기 그대로 들어 있다.
    during[60] = (start + 60, 30.0, 31.0, 30.0, 0.1, 400_000.0)
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms", "ctx_switches_ps"),
        base + during,
    )
    procs = []
    for i in range(0, 200, 2):
        procs.append((start - 200 + i, 10, "python", 2.0, 50.0, 0, 0, 460, 4, 0))
        procs.append((start - 200 + i, 20, "cpu_eater", 40.0, 100.0, 0, 0, 300, 8, 0))
    for i in range(0, 120, 2):
        procs.append((start + i, 10, "python", 2.0, 52.0, 0, 0, 460 + i * 28, 4, 0))
        procs.append((start + i, 20, "cpu_eater", 50.0, 100.0, 0, 0, 300, 8, 0))
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
         "handles", "threads", "foreground"),
        procs,
    )
    return end


def test_contention_does_not_discard_the_detector_claim(db: Database) -> None:
    """`CONTENTION` 은 `NONE` 과 같은 편이다 — 탐지기가 자원과 문장을 갖고 있으면 그쪽을 쓴다.

    `CONTENTION` 은 정의상 "어떤 자원도 포화되지 않았다"는 선언이고, `_RESOURCE_BY_KIND`
    도 `attributable=False` 로 둔다("CPU 상위는 참고일 뿐이다"). 그 상태에서 CPU 로
    분해하면 구간에 CPU 를 많이 쓴 무관한 프로세스(`cpu_eater`)가 원인으로 발표된다.
    """
    now = time.time()
    start = now - 900
    _leak_scene_with_contention(db, start)
    for i in range(20):
        _leak_signal(db, start + i * 5)

    row = _run(db, start, now)

    # **분류가 실제로 `CONTENTION` 이 나왔는지 먼저 고정한다.** 이게 없으면 시나리오가
    # `NONE` 으로 새도 테스트는 통과한다 — `NONE` 은 원래 주장을 채택하기 때문이다.
    # 처음 이 테스트를 쓸 때 실제로 그렇게 됐고, 규칙을 지워도 통과했다.
    assert row["bottleneck"] == "CONTENTION", (
        f"시나리오가 경합을 만들지 못했다({row['bottleneck']}) — 이 테스트는 아무것도 보지 않는다"
    )
    assert "python" in row["title"], f"제목이 탐지기 주장을 버렸다: {row['title']}"
    assert "경합" not in row["title"], f"경합이 제목을 차지했다: {row['title']}"
    assert "cpu_eater" not in row["title"], f"무관한 프로세스를 지목했다: {row['title']}"


def test_claim_never_wins_over_observed_hardware_states() -> None:
    """`GPU`·`THERMAL` 은 탐지기 주장에 양보하지 않는다.

    넷 다 `attributable=False` 지만 성질이 다르다. `NONE`·`CONTENTION` 은 "원인을 짚지
    못했다"는 뜻이고, `GPU`·`THERMAL` 은 특정 하드웨어 상태를 **실제로 관측한** 것이다.
    여기까지 넓히면 `발열 스로틀링 — GPU 92°C` 가 프로세스 하나의 핸들 이야기로 바뀐다 —
    읽는 사람은 그 프로세스를 의심하지만 GPU 를 태운 것은 게임이다.

    실제 시나리오로 재현하려면 GPU 수집 경로까지 세워야 해서, 여기서는 경계를 이름으로
    고정한다. 구체 병목(CPU) 쪽 회귀는
    `test_concrete_bottleneck_is_not_overridden_by_detector` 가 실제 데이터로 지킨다.
    """
    from argus.decide.fusion import _CLAIM_WINS_OVER

    assert _CLAIM_WINS_OVER == {"NONE", "CONTENTION"}, (
        f"주장이 이기는 병목 종류가 바뀌었다: {sorted(_CLAIM_WINS_OVER)}"
    )

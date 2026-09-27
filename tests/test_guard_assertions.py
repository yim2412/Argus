"""조건을 뒤집어도 전체 테스트가 초록이던 제품 판정 줄들 (감사 F-025).

if-py 변이를 전체 테스트(644개)로 재확인했더니 제품 판정인데 단언이 없는 줄이 남았다. 오늘 다른
수정이 한 줄(`fusion.py` 방아쇠 지표)을 덮었고, `procleak.py` 퇴출 순위의 두 가드는 동치(빈 트랙은
생길 수 없고, 표본 1개면 증가율 0 이라 가드와 같은 값)라 뺐다. 여기 남은 12줄을 잰다.
"""

from __future__ import annotations

import pytest

from argus.detection import expr
from argus.detection.base import BaseDetector, Detection, Observation
from argus.detection.baseline import BaselineSet
from argus.detection.rules import Condition, Rule


# ---- 룰 비교 연산자 (rules.py) — `<` 는 동봉 「단일 코어 병목」이 쓴다. 뒤집으면 `!=` 로 떨어진다
@pytest.mark.parametrize("op, actual, expected", [
    (">=", 5.0, True), (">=", 4.0, False),
    ("<", 4.0, True), ("<", 5.0, False),
    ("<=", 5.0, True), ("<=", 6.0, False),
    ("==", 5.0, True), ("==", 4.0, False),
])
def test_condition_operators(op, actual, expected):
    cond = Condition(metric="m", op=op, value=5)
    assert cond.evaluate(Observation(ts=0.0, metrics={"m": actual}), BaselineSet()) is expected


def test_any_rule_is_true_when_one_condition_is_true():
    true_c = Condition(metric="a", op=">", value=1)
    false_c = Condition(metric="b", op=">", value=1)
    rule = Rule(name="x", conditions=[true_c, false_c], mode="any")
    obs = Observation(ts=0.0, metrics={"a": 5.0, "b": 0.0})
    assert rule.evaluate(obs, BaselineSet()) is True
    assert Rule(name="y", conditions=[false_c], mode="any").evaluate(obs, BaselineSet()) is False, "대조"


def test_swap_variable_only_when_the_machine_has_swap(monkeypatch):
    from argus.detection import rules as rules_mod
    from argus.machine import calibration

    class _P:
        cpu = {"logical": 8, "physical": 4}
        memory = {"total_gb": 16, "swap_total_gb": 2}

    monkeypatch.setattr(calibration, "ensure_profile", lambda: _P())
    assert rules_mod._read_machine_variables()["swap_total_mb"] == 2048.0
    _P.memory = {"total_gb": 16, "swap_total_gb": 0}
    assert "swap_total_mb" not in rules_mod._read_machine_variables(), "스왑이 없는데 0MB 로 채웠다"


def test_conditional_expression_with_unknown_test_is_unknown():
    assert expr.evaluate("a if x > 1 else b", {"x": None, "a": 1.0, "b": 2.0}) is None
    assert expr.evaluate("a if x > 1 else b", {"x": 5.0, "a": 1.0, "b": 2.0}) == 1.0, "대조"


def test_warmup_counts_from_the_first_observation():
    class _D(BaseDetector):
        name = "d"

        def evaluate(self, obs):
            return Detection(ts=obs.ts, detector=self.name, score=1.0, severity="info", features={})

    d = _D(warmup_s=10.0)
    assert d.observe(Observation(ts=100.0)) is None
    assert d.observe(Observation(ts=105.0)) is None
    assert d.observe(Observation(ts=111.0)) is not None, "첫 관측부터 10초가 지났는데 판정하지 않는다"


def test_fingerprint_series_accepts_known_stats_and_rejects_others(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    from argus.detection import fingerprint

    assert fingerprint._series("handles_max") == {}, "정상 통계량을 거절했다 — 지문이 하나도 안 만들어진다"
    with pytest.raises(ValueError):
        fingerprint._series("handles_max; DROP TABLE x")


def test_leak_prefilter_handles_a_track_starting_at_zero():
    """시작값이 0 인 트랙(핸들 0 에서 자라는 새 프로세스). 가드가 없으면 0 으로 나눈다."""
    from argus.detection.procleak import _may_grow, _Track

    t = _Track()
    for i, v in enumerate([0.0, 10.0, 20.0]):
        t.add(float(i), v, window_s=1000.0, drop_ratio=0.5)
    assert _may_grow(t, 1.5) is True


# ---- 발열 탐지기 (thermal.py) — 판정이 없으면 신호 없음 · **하루 한 번만** (탐지 규칙 1)
def test_thermal_drift_signals_once_per_day(tmp_path, monkeypatch):
    from argus.detection import thermal
    from argus.storage.hot import Database

    verdicts = [None]
    monkeypatch.setattr(thermal, "evaluate", lambda settings: verdicts[0])
    with Database(tmp_path / "t.db") as db:
        mon = thermal.ThermalDriftMonitor(db)
        count = lambda: db.query("SELECT COUNT(*) AS c FROM anomaly_signals")[0]["c"]  # noqa: E731
        mon.tick()
        assert count() == 0, "판정이 없는데 신호를 냈다"
        verdicts[0] = thermal.Drift(baseline_c=70.0, recent_c=80.0, rise_c=10.0, baseline_days=7, recent_days=3)
        mon.tick()
        assert count() == 1, "대조: 열화 판정이면 신호를 낸다"
        mon.tick()
        assert count() == 1, "같은 열화를 하루 안에 또 알렸다 — 6시간마다 같은 말을 반복한다"

"""탐지 레이어 검증 — 표현식 파서 보안, 베이스라인 로버스트성, 룰 지속·쿨다운.

여기서 가장 중요한 것은 **표현식 파서 보안**이다. 룰 파일은 사용자가 편집하고
나중에는 남이 만든 룰을 받아 쓰게 된다. `eval()` 이었다면 한 줄로 임의 코드 실행이
되고, 성능 모니터가 실행 통로가 된다. 화이트리스트가 뚫리면 조용히 뚫리므로
회귀를 여기서 잡는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection.base import Observation, run_detector  # noqa: E402
from argus.detection.baseline import BaselineSet  # noqa: E402
from argus.detection.expr import ExprError, evaluate  # noqa: E402
from argus.detection.rules import Condition, Rule, RuleEngine, load_rules, parse_duration  # noqa: E402
from argus.detection.rules import RuleError  # noqa: E402


# --------------------------------------------------------------------- 표현식


@pytest.mark.parametrize("source,expected", [
    ("median + 4 * sigma", 18.0),
    ("baseline * 3", 30.0),
    ("max(baseline, 5)", 10.0),
    ("median > 5", True),
    ("median > 5 and sigma < 1", False),
])
def test_expression_evaluates(source, expected):
    assert evaluate(source, {"median": 10.0, "sigma": 2.0, "baseline": 10.0}) == expected


@pytest.mark.parametrize("attack", [
    "__import__('os').system('calc')",
    "().__class__.__bases__[0].__subclasses__()",
    "open('x','w')",
    "exec('x=1')",
    "eval('1')",
    "median.__class__",
    "[1, 2, 3]",
    "{'a': 1}",
    "lambda: 1",
    "globals()",
    "median.__init__.__globals__",
])
def test_expression_rejects_dangerous_input(attack):
    """룰 파일이 실행 통로가 되면 안 된다. 하나라도 통과하면 심각한 결함이다."""
    with pytest.raises(ExprError):
        evaluate(attack, {"median": 10.0})


def test_unknown_metric_yields_none_not_zero():
    """모르는 값을 0 으로 치면 'GPU 온도 정상' 같은 틀린 판정이 나온다."""
    assert evaluate("gpu_temp > 80", {"gpu_temp": None}) is None
    assert evaluate("gpu_temp * 2", {"gpu_temp": None}) is None


def test_division_by_zero_is_none_not_crash():
    assert evaluate("median / sigma", {"median": 1.0, "sigma": 0.0}) is None


# --------------------------------------------------------------------- 베이스라인


def test_baseline_median_resists_contamination():
    """이상 구간이 섞여도 '평소'가 끌려가면 안 된다 — 평균을 안 쓰는 이유다."""
    baseline = BaselineSet(window_s=1000.0, min_samples=30)
    for i in range(300):
        baseline.observe(1000.0 + i, {"cpu": 95.0 if i % 10 < 3 else 20.0})
    stats = baseline.stats("cpu")
    assert 19.0 <= stats.median <= 21.0


def test_baseline_without_dispersion_refuses_to_judge():
    """산포가 없으면 z 를 내지 않는다. 억지로 내면 작은 변동이 전부 이상이 된다."""
    baseline = BaselineSet(window_s=1000.0, min_samples=10)
    for i in range(100):
        baseline.observe(1000.0 + i, {"disk_queue": 0.0})
    stats = baseline.stats("disk_queue")
    assert stats.degenerate
    assert stats.z(1.0) is None
    assert stats.threshold(4) is None


def test_baseline_drops_samples_outside_window():
    baseline = BaselineSet(window_s=100.0, min_samples=10)
    for i in range(500):
        baseline.observe(1000.0 + i, {"cpu": float(i)})
    stats = baseline.stats("cpu")
    assert stats.samples <= 105
    assert stats.minimum >= 399


def test_baseline_not_ready_before_min_samples():
    baseline = BaselineSet(window_s=100.0, min_samples=60)
    for i in range(10):
        baseline.observe(1000.0 + i, {"cpu": 20.0})
    assert baseline.stats("cpu") is None


# --------------------------------------------------------------------- 룰


def _engine(**kwargs):
    rule = Rule(
        name="테스트",
        conditions=[Condition(metric="cpu_total", op=">", value="median + 4 * sigma")],
        for_s=kwargs.pop("for_s", 30.0),
        cooldown_s=kwargs.pop("cooldown_s", 120.0),
    )
    return RuleEngine([rule], window_s=600.0, min_samples=30)


def _stream(quiet_ticks, loud_ticks, *, start=1000.0, quiet=20.0, loud=90.0):
    obs = [Observation(ts=start + i, metrics={"cpu_total": quiet + (i % 3)}) for i in range(quiet_ticks)]
    obs += [Observation(ts=start + quiet_ticks + i, metrics={"cpu_total": loud}) for i in range(loud_ticks)]
    return obs


def test_rule_requires_sustained_condition():
    """순간 스파이크는 이상이 아니다. 알리면 사용자가 알림을 끈다."""
    assert run_detector(_engine(for_s=30.0), _stream(200, 5)) == []


def test_rule_fires_after_sustain_window():
    results = run_detector(_engine(for_s=30.0), _stream(200, 60))
    assert results, "지속된 이상을 탐지하지 못했다"
    assert results[0].ts == 1230.0, f"지속 조건이 틀렸다: {results[0].ts}"


def test_rule_cooldown_suppresses_refiring():
    """쿨다운이 없으면 한 사건에 수백 번 울린다."""
    long_run = run_detector(_engine(for_s=30.0, cooldown_s=120.0), _stream(200, 400))
    assert 1 <= len(long_run) <= 4, f"쿨다운이 안 걸렸다: {len(long_run)}건"


def test_rule_does_not_fire_on_unknown_metric():
    stream = [Observation(ts=1000.0 + i, metrics={"cpu_total": None}) for i in range(300)]
    assert run_detector(_engine(), stream) == []


def test_rule_is_deterministic():
    """같은 입력에 같은 출력. 아니면 탐지기 비교가 성립하지 않는다."""
    stream = _stream(200, 120)
    first = [(d.ts, round(d.score, 6)) for d in run_detector(_engine(), stream)]
    second = [(d.ts, round(d.score, 6)) for d in run_detector(_engine(), stream)]
    assert first == second


def test_suspect_observations_are_skipped():
    """절전 복귀 직후의 값으로 학습도 탐지도 하지 않는다."""
    stream = [Observation(ts=1000.0 + i, metrics={"cpu_total": 99.0}, suspect=True) for i in range(300)]
    assert run_detector(_engine(for_s=1.0), stream) == []


# --------------------------------------------------------------------- 룰 로드


@pytest.mark.parametrize("value,seconds", [
    ("30s", 30.0), ("10m", 600.0), ("1.5h", 5400.0), ("500ms", 0.5), (45, 45.0),
])
def test_duration_parsing(value, seconds):
    assert parse_duration(value, field_name="for") == seconds


def test_bad_operator_rejected_at_load_time():
    """런타임에 처음 터지면 몇 시간 뒤에야 탐지가 멈춘 걸 알게 된다."""
    with pytest.raises(RuleError):
        Condition(metric="cpu", op="~=", value=1)


def test_dangerous_expression_rejected_at_load_time():
    with pytest.raises(RuleError):
        Condition(metric="cpu", op=">", value="__import__('os').system('calc')")


def test_shipped_rules_are_valid():
    """동봉 룰이 깨진 채 배포되면 탐지가 통째로 없는 제품이 나간다."""
    rules = load_rules()
    assert rules, "기본 룰을 하나도 읽지 못했다"
    for rule in rules:
        assert rule.for_s > 0, f"{rule.name}: 지속 조건이 없다"
        assert rule.cooldown_s > 0, f"{rule.name}: 쿨다운이 없다"
        assert rule.conditions, f"{rule.name}: 조건이 없다"

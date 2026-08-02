"""룰의 절대 문턱을 이 PC 의 능력으로 표현한다 (규칙 2).

`ctx_switches_ps > 50000` 은 12코어 기준으로 정한 값이다. 4코어 PC 에서는 과하고,
정작 그 12코어 PC 에서도 **정상값 아래**였다 — 실측 중앙값이 64,566 이라 시간의 66.2%
가 문턱을 넘었다(2026-08-02). 상대 조건이 AND 로 걸려 있어 발화까지 가진 않았지만,
하한으로서 아무 일도 하지 않고 있었다.

`swap_used_mb > 512` 도 같다. RAM 64GB 에서 0.8% 지만 4GB 에서는 12.5% 다 — 같은 숫자가
기계마다 다른 뜻이 되고, 작은 PC 에서는 상시 참이 된다.

여기서 고정하는 것은 두 가지다.

    비례한다        — 코어·RAM 이 다르면 문턱도 다르다
    조용히 안 넘어간다 — 머신 값을 못 읽으면 판정 불가로 두고 경고한다
"""

from __future__ import annotations

import pytest

from argus.detection.baseline import BaselineSet
from argus.detection.base import Observation
from argus.detection.rules import Condition, machine_variables


@pytest.fixture(autouse=True)
def _clear_machine_cache():
    """프로파일 캐시는 프로세스 전역이다. 테스트끼리 새게 두지 않는다."""
    machine_variables.cache_clear()
    yield
    machine_variables.cache_clear()


def _baselines(metric: str, value: float) -> BaselineSet:
    baselines = BaselineSet(window_s=10_000.0, min_samples=10)
    for index in range(120):
        # 산포를 조금 준다 — 완전히 평평하면 sigma 가 0 이라 상대 조건이 성립하지 않는다.
        baselines.observe(float(index), {metric: value + (index % 5)})
    return baselines


def _obs(metric: str, value: float) -> Observation:
    return Observation(ts=1000.0, metrics={metric: value})


def test_context_switch_threshold_scales_with_cores(monkeypatch) -> None:
    """코어가 적은 PC 에서는 문턱도 낮아진다.

    같은 값(50,000)이 4코어에서는 넘고 12코어에서는 못 넘어야 한다 — `cores * 8000`
    이 각각 32,000 과 96,000 이기 때문이다. 절대값이었다면 둘 다 같은 답을 낸다.
    """
    condition = Condition(metric="ctx_switches_ps", op=">", value="cores * 8000")
    baselines = _baselines("ctx_switches_ps", 20_000.0)
    obs = _obs("ctx_switches_ps", 50_000.0)

    monkeypatch.setattr(
        "argus.detection.rules.machine_variables", lambda: {"cores": 4.0}
    )
    assert condition.evaluate(obs, baselines) is True, "4코어에서 32,000 을 넘어야 한다"

    monkeypatch.setattr(
        "argus.detection.rules.machine_variables", lambda: {"cores": 12.0}
    )
    assert condition.evaluate(obs, baselines) is False, "12코어에서 96,000 을 넘으면 안 된다"


def test_swap_threshold_scales_with_ram(monkeypatch) -> None:
    """RAM 이 작은 PC 에서는 더 적은 스왑도 압박이다.

    600MB 스왑은 RAM 4GB(문턱 200MB)에서는 압박이고 64GB(문턱 3.2GB)에서는 아니다.
    """
    condition = Condition(metric="swap_used_mb", op=">", value="ram_mb * 0.05")
    baselines = _baselines("swap_used_mb", 100.0)
    obs = _obs("swap_used_mb", 600.0)

    monkeypatch.setattr(
        "argus.detection.rules.machine_variables", lambda: {"ram_mb": 4096.0}
    )
    assert condition.evaluate(obs, baselines) is True, "RAM 4GB 에서 200MB 를 넘어야 한다"

    monkeypatch.setattr(
        "argus.detection.rules.machine_variables", lambda: {"ram_mb": 65536.0}
    )
    assert condition.evaluate(obs, baselines) is False, "RAM 64GB 에서 3.2GB 를 넘으면 안 된다"


def test_missing_machine_profile_is_undecided_not_silently_true(monkeypatch) -> None:
    """머신 값을 못 읽으면 **판정 불가**다. 참도 거짓도 아니다.

    조용히 `False` 로 두면 그 룰이 영영 발화하지 않고 아무도 모른다. 조용히 `True` 로
    두면 상시 오탐이다. 규칙 4 가 말하는 "없으면 드러내 놓고 비활성화"에 해당한다.
    """
    condition = Condition(metric="ctx_switches_ps", op=">", value="cores * 8000")
    baselines = _baselines("ctx_switches_ps", 20_000.0)
    obs = _obs("ctx_switches_ps", 500_000.0)  # 어떤 문턱이든 넘을 값

    monkeypatch.setattr("argus.detection.rules.machine_variables", dict)
    assert condition.evaluate(obs, baselines) is None, (
        "머신 값이 없는데 판정을 내렸다 — 조용히 통과하거나 상시 발화하게 된다"
    )


def test_machine_variables_expose_cores_and_ram() -> None:
    """실제 프로파일에서 값이 나온다. 이름이 바뀌면 룰이 조용히 깨진다.

    `rules.yaml` 이 `cores`·`ram_mb` 를 문자열로 참조하므로, 키 이름은 계약이다.
    """
    variables = machine_variables()
    if not variables:
        pytest.skip("머신 프로파일이 없는 환경")

    assert variables["cores"] > 0
    assert variables["ram_mb"] > 0
    assert variables["ram_mb"] == pytest.approx(variables["ram_gb"] * 1024.0)


def test_shipped_rules_use_machine_relative_thresholds() -> None:
    """동봉 `rules.yaml` 이 실제로 상대 문턱을 쓴다.

    위 테스트들은 `Condition` 하나만 본다. 룰 파일이 옛 절대값으로 되돌아가도
    그것만으로는 아무도 모른다.
    """
    from argus.detection.rules import load_rules

    by_name = {r.name: r for r in load_rules()}
    checks = {
        "컨텍스트 스위치 급증": ("ctx_switches_ps", "cores"),
        "스왑 사용": ("swap_used_mb", "ram_mb"),
    }
    for rule_name, (metric, expected_var) in checks.items():
        rule = by_name.get(rule_name)
        if rule is None:
            pytest.skip(f"룰이 없다: {rule_name}")
        values = [str(c.value) for c in rule.conditions if c.metric == metric]
        assert any(expected_var in v for v in values), (
            f"{rule_name} 의 {metric} 문턱이 `{expected_var}` 를 쓰지 않는다: {values}"
        )

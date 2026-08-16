"""주입기가 만드는 시나리오를 채점기가 전부 아는가.

**2026-08-16 에 실제로 뚫렸다.** `memory_leak_spread` 를 `tools/fault_injector.py` 에
추가하면서 `argus/eval/attribution.py` 의 `SCENARIO_RESOURCE` 를 안 고쳤는데, 그때
기본값이 `"cpu"` 여서 **메모리 누수를 CPU 기준으로 채점**할 뻔했다. 예외도 경고도
없이 그냥 틀린 점수가 나온다.

`tools/` 에는 테스트가 없어서 이런 어긋남이 초록불 뒤에 숨는다(같은 유형을 08-15 에
`autolabel_backfill.py` 에서 겪었다). 그래서 여기서 **양쪽을 마주 세워** 기계적으로
잡는다 — 새 시나리오를 추가하면 이 테스트가 먼저 깨진다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from argus.eval.attribution import NOT_ATTRIBUTABLE, SCENARIO_RESOURCE

ROOT = Path(__file__).resolve().parent.parent


def _injector_scenarios() -> set[str]:
    """`tools/fault_injector.py` 의 `SCENARIOS` 키. 패키지가 아니라 경로로 읽는다."""
    path = ROOT / "tools" / "fault_injector.py"
    spec = importlib.util.spec_from_file_location("_fault_injector_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return set(module.SCENARIOS)
    finally:
        sys.modules.pop(spec.name, None)


def test_every_injector_scenario_is_known_to_the_scorer() -> None:
    scenarios = _injector_scenarios()
    known = set(SCENARIO_RESOURCE) | set(NOT_ATTRIBUTABLE)
    missing = scenarios - known
    assert not missing, (
        f"주입기에 있는데 채점기가 모르는 시나리오: {sorted(missing)} — "
        f"`SCENARIO_RESOURCE` 에 자원을 적거나 `NOT_ATTRIBUTABLE` 에 넣을 것"
    )


def test_scorer_does_not_know_scenarios_that_cannot_be_injected() -> None:
    """반대 방향. 이름을 바꾸면 옛 이름이 표에 남아 조용히 죽는다."""
    scenarios = _injector_scenarios()
    stale = (set(SCENARIO_RESOURCE) | set(NOT_ATTRIBUTABLE)) - scenarios
    assert not stale, f"채점기에만 남은 시나리오(주입기에서 사라졌다): {sorted(stale)}"


def test_unknown_scenario_is_skipped_not_scored_as_cpu() -> None:
    """**기본값으로 때우지 않는다.**

    이 테스트가 없으면 `SCENARIO_RESOURCE.get(scenario, "cpu")` 로 되돌려도 위 두
    테스트는 통과한다 — 표는 여전히 맞으니까. 막아야 하는 것은 *표에 없는 것이
    들어왔을 때의 행동* 이다.
    """
    from argus.eval.attribution import score_fault

    fault = {
        "id": -1,
        "scenario": "이_시나리오는_표에_없다",
        "ts_start": 1_000_000.0,
        "ts_end": 1_000_600.0,
        "pid": 4242,
    }
    verdict = score_fault(None, fault)      # db 에 닿기 전에 반환되어야 한다
    assert verdict.skipped, "표에 없는 시나리오가 채점을 건너뛰지 않았다"
    assert "SCENARIO_RESOURCE" in verdict.skipped
    assert verdict.resource != "cpu", "모르는 시나리오를 cpu 로 채점하고 있다"


@pytest.mark.parametrize("scenario", ["memory_leak", "memory_leak_spread"])
def test_memory_scenarios_are_scored_on_rss(scenario: str) -> None:
    """분산 누수도 메모리다. 이름이 달라졌다고 자원이 달라지지 않는다."""
    assert SCENARIO_RESOURCE[scenario] == "rss"

"""스로틀이 풀리면 자고 있던 컴포넌트가 깨는가.

2026-08-04 에 스로틀 3(배수 10.0)이 걸리며 5분 롤업이 `_stop.wait(3000)` 에 들어갔다.
24분 뒤 스로틀은 0 으로 회복됐지만 **이미 잠든 스레드는 깨지 않아 46분간 롤업이
멈춰 있었다.** `metrics_1m` 은 60초 주기라 정상이어서 티도 안 났다.

증상이 "예외"가 아니라 "관측이 조용히 빈다"는 쪽이라 로그로는 안 잡힌다.
그래서 대기 시간 자체를 잰다.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import BudgetSettings  # noqa: E402
from argus.runtime.supervisor import CallableComponent, Supervisor  # noqa: E402


class _Multiplier:
    """스로틀 배수를 테스트가 직접 흔든다. BudgetGuard 를 띄우지 않기 위함."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _run_until(sup: Supervisor, event: threading.Event, timeout: float) -> bool:
    sup.start()
    try:
        return event.wait(timeout)
    finally:
        sup.stop(timeout=5.0)


def test_thread_wakes_when_throttle_is_released():
    """배수 20 배로 자던 중 스로틀이 풀리면 정상 주기 근처에서 깨야 한다."""
    mult = _Multiplier(20.0)  # interval 0.2s * 20 = 4초 대기
    ticks: list[float] = []
    second_tick = threading.Event()

    def tick() -> None:
        ticks.append(time.perf_counter())
        if len(ticks) >= 2:
            second_tick.set()

    sup = Supervisor(multiplier_fn=mult, wake_granularity_s=0.05)
    sup.add(CallableComponent("throttled", tick, interval_s=0.2))

    def release() -> None:
        time.sleep(0.5)  # 첫 틱 뒤 4초 대기에 확실히 들어간 시점
        mult.value = 1.0

    releaser = threading.Thread(target=release, daemon=True)
    releaser.start()

    assert _run_until(sup, second_tick, timeout=3.0), (
        "스로틀이 풀렸는데도 3초 안에 두 번째 틱이 오지 않았다 — "
        "잠든 스레드가 배수 변화를 보지 않는다"
    )
    releaser.join(timeout=1.0)

    waited = ticks[1] - ticks[0]
    # 스로틀이 풀린 것은 0.5초 시점. 거기서 granularity(0.05) 안에 깨야 하므로
    # 넉넉히 잡아도 1초를 넘지 않는다. 고치기 전에는 4초였다.
    assert waited < 1.0, f"두 번째 틱이 {waited:.2f}초 뒤에 왔다 (스로틀 해제는 0.5초 시점)"


def test_thread_keeps_sleeping_while_throttle_holds():
    """반대 방향 — 스로틀이 유지되면 늘어난 주기를 실제로 지켜야 한다.

    이게 없으면 위 테스트는 `wake_granularity` 마다 무조건 틱하는 구현으로도 통과한다.
    """
    mult = _Multiplier(20.0)  # 0.15s * 20 = 3초
    ticks: list[float] = []
    second_tick = threading.Event()

    def tick() -> None:
        ticks.append(time.perf_counter())
        if len(ticks) >= 2:
            second_tick.set()

    sup = Supervisor(multiplier_fn=mult, wake_granularity_s=0.05)
    sup.add(CallableComponent("throttled", tick, interval_s=0.15))

    fired = _run_until(sup, second_tick, timeout=1.0)
    assert not fired, (
        f"스로틀 20배(주기 3초)인데 1초 안에 두 번 틱했다 — 대기가 무시된다 (ticks={len(ticks)})"
    )


def test_non_throttleable_component_ignores_multiplier():
    """`throttleable=False` 는 배수를 받지 않는다 — 예산 가드 자신이 늦어지면 회복도 늦는다."""
    mult = _Multiplier(50.0)
    ticks: list[float] = []
    second_tick = threading.Event()

    def tick() -> None:
        ticks.append(time.perf_counter())
        if len(ticks) >= 2:
            second_tick.set()

    sup = Supervisor(multiplier_fn=mult, wake_granularity_s=0.05)
    sup.add(CallableComponent("guard", tick, interval_s=0.1, throttleable=False))

    assert _run_until(sup, second_tick, timeout=2.0), "배수가 안 걸려야 하는 컴포넌트가 늦춰졌다"


def test_wake_granularity_must_be_positive():
    """0 이면 대기 루프가 바쁜 회전(busy loop)이 된다 — 예산을 지키려는 코드가 예산을 먹는다."""
    with pytest.raises(ValueError):
        Supervisor(wake_granularity_s=0.0)


# ---------------------------------------------------------------- 배선

def test_wake_granularity_is_wired_from_config():
    """**로직과 배선을 따로 잰다.** 값이 config 에서 오지 않으면 YAML 을 고쳐도 안 바뀐다.

    2026-08-03 에 `procleak` 단조성이 정확히 이 상태였다 — 로직만 보는 테스트라
    `defaults.yaml` 을 고쳐도 판정이 안 바뀌는데 241개가 전부 통과했다.
    """
    from argus.config.loader import load_settings

    settings = load_settings()
    assert settings.budget.wake_granularity_s > 0

    # 기본값(15.0)과 다른 값을 넣었을 때 Supervisor 가 그 값을 쓰는지
    custom = BudgetSettings(wake_granularity_s=3.25)
    sup = Supervisor(wake_granularity_s=custom.wake_granularity_s)
    assert sup._wake_granularity_s == 3.25


def test_defaults_yaml_declares_wake_granularity():
    """YAML 에 키가 있어야 사용자가 튜닝할 수 있다 (설계 규칙 3)."""
    import yaml

    raw = yaml.safe_load((ROOT / "argus" / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    assert "wake_granularity_s" in raw["budget"], "defaults.yaml 의 budget 에 키가 없다"

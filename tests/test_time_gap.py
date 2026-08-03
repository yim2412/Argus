"""절전 복귀 처리 검증.

실제로 PC 를 재우고 테스트할 수는 없으므로, 감시자의 시계 기준점을 과거로 밀어
"방금 3시간이 지났다"고 믿게 만든다. 그 뒤 수집기들이 올바르게 재설정되는지 본다.

확인하는 것은 두 가지다.
  1. 공백이 `system_events` 에 기록되는가 — 이후 단계가 그 구간을 제외할 근거.
  2. 프로세스 생성·종료 이벤트가 폭주하지 않는가 — 못 본 것을 지어내면 안 된다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# `python tests/test_time_gap.py` 로 직접 실행할 때도 패키지를 찾게 한다.
# (CLAUDE.md: 각 모듈은 단독 실행으로 자기 점검이 되어야 한다)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.collector.process import ProcessCollector  # noqa: E402
from argus.collector.system import SystemCollector  # noqa: E402
from argus.runtime.gapmon import GapMonitor  # noqa: E402
from argus.runtime.supervisor import Supervisor  # noqa: E402
from argus.storage.queue import SampleQueue  # noqa: E402


def test_gap_monitor_ignores_normal_ticks() -> None:
    fired: list[float] = []
    monitor = GapMonitor(interval_s=0.05, threshold_s=1.0, on_gap=lambda g, d: fired.append(g))
    monitor.setup()
    for _ in range(5):
        time.sleep(0.05)
        monitor.tick()
    assert not fired, "정상 동작을 공백으로 오인했다"


def test_gap_monitor_detects_suspend() -> None:
    fired: list[tuple[float, dict]] = []
    monitor = GapMonitor(interval_s=1.0, threshold_s=30.0, on_gap=lambda g, d: fired.append((g, d)))
    monitor.setup()

    # 절전 흉내: 두 시계를 함께 되돌린다
    monitor._last_wall -= 10800.0
    monitor._last_mono -= 10800.0
    monitor.tick()

    assert fired, "공백을 감지하지 못했다"
    gap, detail = fired[-1]
    assert gap > 10000, f"공백 길이가 이상하다: {gap}"
    assert detail["likely_cause"] == "suspend_or_stall", detail


def test_process_collector_suppresses_event_storm() -> None:
    """복귀 후 프로세스 목록 재기준. 대량 이벤트가 나오면 안 된다."""
    queue = SampleQueue(maxsize=50000)
    collector = ProcessCollector(queue, collect_interval_s=1.0, full_store_interval_s=3600.0)
    collector.setup()
    try:
        # 절전 동안 프로세스가 전부 바뀐 상황을 만든다 —
        # 기준선이 재설정되지 않으면 여기서 수백 건의 이벤트가 쏟아진다.
        collector._known = {999000 + i for i in range(300)}

        queue.drain(100000)  # 앞선 내용 비우기
        collector.on_time_gap(10800.0)
        collector.tick()
        samples = queue.drain(100000)
    finally:
        collector.teardown()

    events = [s for s in samples if s.table == "process_events"]
    assert len(events) < 10, f"복귀 후 이벤트가 폭주했다: {len(events)}건"

    metrics = [s for s in samples if s.table == "process_metrics"]
    assert metrics, "복귀 후 수집이 재개되지 않았다"


def test_system_collector_resets_rates() -> None:
    """복귀 후 누적 카운터 차분을 버려야 한다."""
    queue = SampleQueue(maxsize=1000)
    collector = SystemCollector(queue, interval_s=1.0)
    collector.setup()
    try:
        time.sleep(0.3)
        collector.tick()
        assert len(collector._rates._prev) > 0, "속도 추적이 시작되지 않았다"

        collector.on_time_gap(10800.0)
        assert len(collector._rates._prev) == 0, "복귀 후 속도 추적이 초기화되지 않았다"

        # 재설정 후에도 계속 동작해야 한다
        time.sleep(0.3)
        collector.tick()
        collector.tick()
    finally:
        collector.teardown()


def test_supervisor_rejects_components_missing_the_contract() -> None:
    """**규약 위반은 등록 시점에 터져야 한다.**

    2026-08-03 까지 `FingerprintBuilder` 가 `Component` 를 상속하지 않아 `throttleable`
    이 없었다. 그 속성은 매 틱 `tick()` 을 감싼 try **바깥**에서 읽히므로 첫 틱 직후
    `AttributeError` 로 스레드가 죽었고, `pythonw` 에는 stderr 가 없어 **아무 신호도
    없었다** — 지문 갱신이 기동 시 1회로 줄어든 채 돌았다. exe 빌드가 콘솔을 띄운
    덕에 드러났다.

    기동 시점의 시끄러운 실패가 런타임의 조용한 죽음보다 낫다.
    """
    import pytest

    class MissingThrottleable:
        name = "broken"
        interval_s = 1.0

        def tick(self) -> None:
            pass

    with pytest.raises(TypeError, match="throttleable"):
        Supervisor().add(MissingThrottleable())


def test_supervisor_accepts_components_without_optional_hooks() -> None:
    """`setup`·`teardown` 은 선택이다 — 둘 다 try 안에서 불려 없어도 조용히 죽지 않는다.

    규약 검사가 이것까지 요구하면 덕 타이핑으로 만든 대역을 거부하게 된다.
    """

    class Minimal:
        name = "minimal"
        interval_s = 1.0
        throttleable = True

        def tick(self) -> None:
            pass

    Supervisor().add(Minimal())  # 예외가 나면 실패


def test_fingerprint_builder_satisfies_the_component_contract() -> None:
    """실제로 물렸던 그 컴포넌트를 직접 고정한다.

    위 두 테스트는 규약 **검사**를 보고, 이건 그 검사를 통과해야 할 **당사자**를 본다.
    """
    from argus.detection.fingerprint import FingerprintBuilder
    from argus.runtime.supervisor import Component

    assert issubclass(FingerprintBuilder, Component), (
        "Component 를 상속하지 않으면 throttleable 이 없어 첫 틱 직후 스레드가 죽는다"
    )
    builder = FingerprintBuilder(db=None)
    for attr in Supervisor._REQUIRED:
        assert hasattr(builder, attr), f"규약 속성 `{attr}` 이 없다"


def test_supervisor_broadcast_isolates_failures() -> None:
    """한 컴포넌트의 복구 실패가 나머지를 막으면 안 된다."""

    class Boom:
        name = "boom"
        interval_s = 1.0
        throttleable = True

        def tick(self) -> None:
            pass

        def on_time_gap(self, gap_s: float) -> None:
            raise RuntimeError("의도된 복구 실패")

    class Fine:
        name = "fine"
        interval_s = 1.0
        throttleable = True

        def __init__(self) -> None:
            self.called = False

        def tick(self) -> None:
            pass

        def on_time_gap(self, gap_s: float) -> None:
            self.called = True

    fine = Fine()
    sup = Supervisor()
    sup.add(Boom())  # type: ignore[arg-type]
    sup.add(fine)  # type: ignore[arg-type]

    handled = sup.broadcast_time_gap(3600.0)
    assert fine.called, "실패한 컴포넌트 뒤의 컴포넌트가 복구되지 않았다"
    assert handled == ["fine"], handled


if __name__ == "__main__":  # 스모크: python tests/test_time_gap.py
    checks = [
        ("정상 틱을 공백으로 오인하지 않음", test_gap_monitor_ignores_normal_ticks),
        ("절전 감지", test_gap_monitor_detects_suspend),
        ("이벤트 폭주 억제", test_process_collector_suppresses_event_storm),
        ("속도 추적 초기화", test_system_collector_resets_rates),
        ("복구 실패 격리", test_supervisor_broadcast_isolates_failures),
    ]
    for label, fn in checks:
        started = time.perf_counter()
        try:
            fn()
        except AssertionError as e:
            print(f"[FAIL] {label}: {e}")
            raise SystemExit(1)
        print(f"  [OK] {label}  ({(time.perf_counter()-started)*1000:.0f}ms)")
    print("[OK] 절전 복귀 처리")

"""GPU 수집기의 실패·복구 경로.

**2026-07-29 에 GPU 수집이 조용히 1시간 죽었다.** `access violation` 이 3,845번 연속이고
그 구간 `gpu_metrics` 0행인데, 실패가 `log.debug` 라 경고도 대시보드 노출도 없었다.
`nvmlInit()` 이 `setup()` 에서 한 번뿐이라 재기동 없이는 복구 경로도 없었다.

여기서 재현하는 것은 **드라이버 재시작**이다. 실제 GPU 를 쓰지 않고 가짜 NVML 을 끼워
넣는다 — 실제 드라이버를 죽이는 테스트는 돌릴 수 없고, 돌릴 수 있어도 남의 PC 에서
재현되지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.collector import gpu as gpumod  # noqa: E402
from argus.storage.queue import SampleQueue  # noqa: E402


class FakeNvmlError(Exception):
    pass


class FakeNotSupported(FakeNvmlError):
    pass


class FakeNoPermission(FakeNvmlError):
    pass


class _Util:
    def __init__(self, g: float, m: float) -> None:
        self.gpu = g
        self.memory = m


class _Mem:
    def __init__(self) -> None:
        self.used = 2 * 1024 * 1024 * 1024
        self.total = 10 * 1024 * 1024 * 1024


class FakeNvml:
    """NVML 의 최소 대역. `alive` 를 내리면 드라이버가 죽은 상태가 된다."""

    # 두 클래스는 서로 다르고, 둘 다 아닌 예외(FakeNvmlError)가 "일시적 실패"다.
    # 여기서 기반 클래스를 alias 하면 모든 실패가 영구 실패로 판정되어 테스트가
    # 아무것도 검증하지 않는다 — 처음에 그렇게 써서 실제로 통과했다.
    NVMLError_NotSupported = FakeNotSupported
    NVMLError_NoPermission = FakeNoPermission
    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_SM = 0
    NVML_CLOCK_MEM = 1

    def __init__(self) -> None:
        self.alive = True
        self.init_calls = 0
        self.shutdown_calls = 0
        self.temp_supported = True
        self.temp_transient_fail = False

    # --- 준비 -----------------------------------------------------------
    def nvmlInit(self):  # noqa: N802
        self.init_calls += 1
        if not self.alive:
            raise FakeNvmlError("드라이버 없음")

    def nvmlShutdown(self):  # noqa: N802
        self.shutdown_calls += 1

    def nvmlDeviceGetCount(self):  # noqa: N802
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):  # noqa: N802
        if not self.alive:
            raise FakeNvmlError("핸들 없음")
        return f"handle{i}"

    def nvmlDeviceGetName(self, handle):  # noqa: N802
        return b"FakeGPU 9090"

    # --- 조회 -----------------------------------------------------------
    def _check(self):
        if not self.alive:
            raise FakeNvmlError("access violation")

    def nvmlDeviceGetUtilizationRates(self, handle):  # noqa: N802
        self._check()
        return _Util(42.0, 13.0)

    def nvmlDeviceGetMemoryInfo(self, handle):  # noqa: N802
        self._check()
        return _Mem()

    def nvmlDeviceGetTemperature(self, handle, kind):  # noqa: N802
        if not self.temp_supported:
            raise FakeNotSupported("미지원")
        if self.temp_transient_fail:
            raise FakeNvmlError("일시적 실패")
        self._check()
        return 61.0

    def nvmlDeviceGetPowerUsage(self, handle):  # noqa: N802
        self._check()
        return 120_000

    def nvmlDeviceGetEnforcedPowerLimit(self, handle):  # noqa: N802
        self._check()
        return 320_000

    def nvmlDeviceGetPerformanceState(self, handle):  # noqa: N802
        self._check()
        return 2

    def nvmlDeviceGetClockInfo(self, handle, kind):  # noqa: N802
        self._check()
        return 1800

    def nvmlDeviceGetFanSpeed(self, handle):  # noqa: N802
        self._check()
        return 45

    def nvmlDeviceGetCurrentClocksThrottleReasons(self, handle):  # noqa: N802
        self._check()
        return 0


@pytest.fixture()
def fake(monkeypatch):
    nvml = FakeNvml()
    monkeypatch.setattr(gpumod, "pynvml", nvml)
    monkeypatch.setattr(gpumod, "_HAVE_NVML", True)
    return nvml


def _collector(fake, **kwargs):
    c = gpumod.GpuCollector(SampleQueue(maxsize=1000), **kwargs)
    c.setup()
    assert c._enabled, "가짜 NVML 로 활성화되지 않았다"
    return c


def test_collects_when_driver_is_alive(fake):
    c = _collector(fake)
    c.tick()
    assert c.healthy
    assert c.describe()["consecutive_failures"] == 0


def test_transient_failure_does_not_warn_or_reinit(fake):
    """한두 번의 실패는 정상이다. 이때 재초기화하면 드라이버를 흔든다."""
    c = _collector(fake, recover_after_failures=5)
    fake.alive = False
    for _ in range(3):
        c.tick()
    assert c.healthy, "3회 실패에 이미 비정상으로 판정했다"
    assert fake.init_calls == 1, "문턱 전에 재초기화했다"


def test_consecutive_failures_trigger_reinit(fake):
    """문턱을 넘으면 NVML 을 다시 초기화한다 — 없으면 재기동 전까지 GPU 가 빈다."""
    c = _collector(fake, recover_after_failures=3, recover_backoff_s=0.0)
    fake.alive = False
    for _ in range(3):
        c.tick()
    assert not c.healthy
    assert fake.init_calls >= 2, "연속 실패에도 재초기화를 시도하지 않았다"
    assert fake.shutdown_calls >= 1, "재초기화 전에 shutdown 을 부르지 않았다"


def test_recovers_after_driver_returns(fake):
    """드라이버가 돌아오면 수집이 재개되고 상태가 초기화된다."""
    c = _collector(fake, recover_after_failures=3, recover_backoff_s=0.0)
    fake.alive = False
    for _ in range(4):
        c.tick()
    assert not c.healthy

    fake.alive = True
    c.tick()  # 재초기화가 성공하는 틱
    c.tick()  # 수집이 도는 틱
    assert c.healthy, "드라이버가 돌아왔는데 비정상 상태가 남았다"
    assert c.describe()["recoveries"] >= 1
    assert c.describe()["consecutive_failures"] == 0


def test_backoff_limits_reinit_attempts(fake):
    """백오프가 없으면 드라이버가 죽어 있는 동안 매 틱 재초기화한다 —
    관측자가 병목이 되면 제품은 실패다."""
    c = _collector(fake, recover_after_failures=2, recover_backoff_s=3600.0)
    fake.alive = False
    for _ in range(20):
        c.tick()
    # 최초 1회(setup) + 백오프 안에서 딱 1회
    assert fake.init_calls == 2, f"백오프를 무시하고 {fake.init_calls}회 초기화했다"


def test_failure_state_is_exposed(fake):
    """조용히 실패하지 않는다 — 대시보드가 볼 수 있게 값에 실린다(규칙 4)."""
    c = _collector(fake, recover_after_failures=3, recover_backoff_s=3600.0)
    fake.alive = False
    for _ in range(5):
        c.tick()
    state = c.describe()
    assert state["healthy"] is False
    assert state["consecutive_failures"] == 5
    assert "access violation" in state["last_error"]
    assert state["failing_since"] is not None
    assert state["recover_attempts"] >= 1


def test_unsupported_metric_is_blacklisted_once(fake):
    """지원하지 않는 항목은 영구히 건너뛴다 — 매 초 예외를 만드는 비용이 아깝다."""
    c = _collector(fake)
    fake.temp_supported = False
    c.tick()
    assert any(k.startswith("temp") for k in c._unsupported)


def test_transient_metric_failure_is_not_blacklisted(fake):
    """일시적 실패를 미지원으로 낙인하면 드라이버가 복구된 뒤에도 그 지표가 영영 빈다.

    2026-07-29 의 access violation 이 정확히 이 경로를 탔다.
    """
    c = _collector(fake)
    fake.temp_transient_fail = True
    c.tick()
    assert not c._unsupported, f"일시적 실패를 낙인했다: {c._unsupported}"

    fake.temp_transient_fail = False
    c.tick()
    rows = c.queue.drain(100)
    latest = dict(zip(gpumod.COLUMNS, rows[-1].values))
    assert latest["temp_c"] == 61.0, "복구 후에도 온도가 비어 있다"

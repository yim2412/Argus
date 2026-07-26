"""GPU 수집기 (NVIDIA / NVML).

**스로틀 사유가 이 모듈의 핵심 산출물이다.** GPU 사용률 100% 는 정상적으로 열심히
일하는 중일 수도 있지만, 온도 스로틀이 걸린 상태라면 성능이 깎이고 있다는 뜻이다.
NVML 은 그 이유를 비트마스크로 알려주므로 추측할 필요가 없다.

NVIDIA GPU 가 없는 환경(AMD/Intel/내장/서버)에서는 조용히 비활성화되고, 나머지
수집은 그대로 돈다. 배포 대상이므로 GPU 유무를 가정하지 않는다.
"""

from __future__ import annotations

import time
from typing import Any

from ..logging_setup import get_logger
from ..storage.queue import SampleQueue
from .base import Collector

log = get_logger(__name__)

try:
    import pynvml

    _HAVE_NVML = True
except ImportError:  # pragma: no cover
    pynvml = None  # type: ignore[assignment]
    _HAVE_NVML = False


COLUMNS = (
    "ts",
    "gpu_index",
    "util_percent",
    "mem_util_percent",
    "vram_used_mb",
    "vram_total_mb",
    "temp_c",
    "power_w",
    "power_limit_w",
    "pstate",
    "clock_sm_mhz",
    "clock_mem_mhz",
    "fan_percent",
    "throttle_reasons",
)

_MB = 1024 * 1024

# NVML 스로틀 사유 비트 → 사람이 읽는 이름.
# 상수는 드라이버·바인딩 버전에 따라 없을 수 있어 getattr 로 안전하게 읽는다.
_THROTTLE_BITS: list[tuple[str, str]] = [
    ("nvmlClocksThrottleReasonGpuIdle", "IDLE"),
    ("nvmlClocksThrottleReasonApplicationsClocksSetting", "APP_CLOCK"),
    ("nvmlClocksThrottleReasonSwPowerCap", "SW_POWER_CAP"),
    ("nvmlClocksThrottleReasonHwSlowdown", "HW_SLOWDOWN"),
    ("nvmlClocksThrottleReasonSyncBoost", "SYNC_BOOST"),
    ("nvmlClocksThrottleReasonSwThermalSlowdown", "SW_THERMAL"),
    ("nvmlClocksThrottleReasonHwThermalSlowdown", "HW_THERMAL"),
    ("nvmlClocksThrottleReasonHwPowerBrakeSlowdown", "HW_POWER_BRAKE"),
    ("nvmlClocksThrottleReasonDisplayClockSetting", "DISPLAY_CLOCK"),
]


def _decode_throttle(mask: int) -> str:
    """비트마스크 → "HW_THERMAL,SW_POWER_CAP" 형태 문자열."""
    if not mask:
        return ""
    names = []
    for attr, label in _THROTTLE_BITS:
        bit = getattr(pynvml, attr, None)
        if bit and (mask & bit):
            names.append(label)
    return ",".join(names)


class GpuCollector(Collector):
    """장치별로 한 행씩 낸다."""

    name = "gpu"

    def __init__(self, queue: SampleQueue, *, interval_s: float = 1.0) -> None:
        super().__init__(queue)
        self.interval_s = interval_s
        self._handles: list[Any] = []
        self._names: list[str] = []
        self._enabled = False
        self._reason = ""
        self._samples = 0
        # 조회가 반복 실패하는 항목은 기록해 두고 다시 시도하지 않는다.
        # (지원하지 않는 항목을 매 초 예외로 만들면 그 자체가 비용이다)
        self._unsupported: set[str] = set()

    # ------------------------------------------------------------------ 준비

    def setup(self) -> None:
        if not _HAVE_NVML:
            self._reason = "nvidia-ml-py 없음"
            log.info("GPU 수집 비활성", extra={"reason": self._reason})
            return
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
        except Exception as e:
            # NVIDIA 드라이버가 없거나 GPU 가 다른 벤더인 경우. 정상 상황이다.
            self._reason = f"NVML 초기화 실패: {e}"
            log.info("GPU 수집 비활성 (NVIDIA GPU 없음으로 간주)", extra={"reason": str(e)})
            return

        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                self._handles.append(handle)
                self._names.append(name.decode() if isinstance(name, bytes) else name)
            except Exception as e:
                log.warning("GPU 핸들 획득 실패", extra={"index": i, "error": str(e)})

        self._enabled = bool(self._handles)
        if self._enabled:
            log.info("GPU 수집 활성", extra={"devices": self._names})
        else:
            self._reason = "사용 가능한 장치 없음"

    def teardown(self) -> None:
        if _HAVE_NVML and self._enabled:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------ 수집

    def _try(self, key: str, fn) -> Any:
        """지원하지 않는 항목은 한 번만 시도하고 이후 건너뛴다."""
        if key in self._unsupported:
            return None
        try:
            return fn()
        except Exception:
            self._unsupported.add(key)
            return None

    def collect(self) -> None:
        if not self._enabled:
            return
        now = time.time()

        for index, handle in enumerate(self._handles):
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            except Exception as e:
                # 드라이버 재시작·GPU 리셋 등. 다음 틱에 회복될 수 있으므로 이번만 건너뛴다.
                log.debug("GPU 조회 실패", extra={"index": index, "error": str(e)})
                continue

            temp = self._try(
                f"temp{index}",
                lambda: pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU),
            )
            power_mw = self._try(f"power{index}", lambda: pynvml.nvmlDeviceGetPowerUsage(handle))
            limit_mw = self._try(
                f"plimit{index}", lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(handle)
            )
            pstate = self._try(f"pstate{index}", lambda: pynvml.nvmlDeviceGetPerformanceState(handle))
            clock_sm = self._try(
                f"csm{index}", lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
            )
            clock_mem = self._try(
                f"cmem{index}", lambda: pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            )
            fan = self._try(f"fan{index}", lambda: pynvml.nvmlDeviceGetFanSpeed(handle))
            throttle_mask = self._try(
                f"throttle{index}",
                lambda: pynvml.nvmlDeviceGetCurrentClocksThrottleReasons(handle),
            )

            self.emit(
                "gpu_metrics",
                COLUMNS,
                (
                    now,
                    index,
                    float(util.gpu),
                    float(util.memory),
                    round(mem.used / _MB, 1),
                    round(mem.total / _MB, 1),
                    float(temp) if temp is not None else None,
                    round(power_mw / 1000.0, 2) if power_mw is not None else None,
                    round(limit_mw / 1000.0, 2) if limit_mw is not None else None,
                    int(pstate) if pstate is not None else None,
                    int(clock_sm) if clock_sm is not None else None,
                    int(clock_mem) if clock_mem is not None else None,
                    float(fan) if fan is not None else None,
                    _decode_throttle(throttle_mask) if throttle_mask is not None else None,
                ),
            )
        self._samples += 1

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "enabled": self._enabled,
            "devices": self._names,
            "reason": self._reason,
            "samples": self._samples,
            "unsupported": sorted(self._unsupported),
        }


if __name__ == "__main__":  # 스모크: python -m argus.collector.gpu
    from ..logging_setup import setup
    from ..storage.hot import Database

    setup(level="INFO")
    queue = SampleQueue(maxsize=1000)
    collector = GpuCollector(queue)
    collector.setup()
    try:
        for _ in range(3):
            time.sleep(0.5)
            collector.tick()
        samples = queue.drain(100)
        status = collector.describe()
    finally:
        collector.teardown()

    print(f"  상태: enabled={status['enabled']}  devices={status['devices']}")
    if not status["enabled"]:
        # GPU 없는 환경에서도 이것은 실패가 아니다. 그 사실만 분명히 알린다.
        print(f"  비활성 사유: {status['reason']}")
        print("[OK] collector.gpu (GPU 없음 — 비활성 상태로 정상 동작)")
        raise SystemExit(0)

    print(f"  수집: {len(samples)}행")
    latest = dict(zip(COLUMNS, samples[-1].values))
    for key in COLUMNS:
        if key != "ts":
            print(f"    {key:18} {latest[key]}")
    if status["unsupported"]:
        print(f"  미지원 항목: {status['unsupported']}")

    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM gpu_metrics")[0]["c"]
        db.insert_many("gpu_metrics", COLUMNS, [s.values for s in samples])
        after = db.query("SELECT COUNT(*) AS c FROM gpu_metrics")[0]["c"]
        print(f"  DB 기록: {before} -> {after}")

    if latest["vram_total_mb"] is None or latest["util_percent"] is None:
        print("[FAIL] 필수 GPU 지표가 비어 있다")
        raise SystemExit(1)
    print("[OK] collector.gpu")

"""GPU 수집기 (NVIDIA / NVML).

**스로틀 사유가 이 모듈의 핵심 산출물이다.** GPU 사용률 100% 는 정상적으로 열심히
일하는 중일 수도 있지만, 온도 스로틀이 걸린 상태라면 성능이 깎이고 있다는 뜻이다.
NVML 은 그 이유를 비트마스크로 알려주므로 추측할 필요가 없다.

NVIDIA GPU 가 없는 환경(AMD/Intel/내장/서버)에서는 조용히 비활성화되고, 나머지
수집은 그대로 돈다. 배포 대상이므로 GPU 유무를 가정하지 않는다.

**한 번 살아난 NVML 이 죽는 경우를 다룬다.** 드라이버 업데이트·GPU 리셋이 일어나면
기존 핸들이 무효가 되어 조회가 계속 실패하는데, `nvmlInit()` 을 `setup()` 에서 한 번만
부르면 **재기동 전까지 GPU 가 통째로 빈다.** 2026-07-29 03:58~05:02 에 실제로 그랬다 —
`access violation` 이 3,845번 연속이고 그 구간 `gpu_metrics` 0행이었으며, 실패가
`log.debug` 라 경고도 대시보드 노출도 없었다(규칙 4 위반). 그래서 연속 실패가 문턱을
넘으면 **NVML 을 다시 초기화하고, 그 사실을 드러낸다.**

**"미지원"과 "지금 실패"를 구분한다.** 지원하지 않는 항목은 영구히 건너뛰어야 하지만
(매 초 예외를 만드는 비용), 일시적 실패를 같이 낙인하면 드라이버가 복구된 뒤에도 그
지표가 영영 돌아오지 않는다. NVML 이 미지원을 별도 예외로 알려주므로 그것만 낙인한다.
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


def _is_permanent_error(exc: BaseException) -> bool:
    """이 예외가 "이 GPU 는 이 항목을 지원하지 않는다"는 뜻인가.

    클래스를 `getattr` 로 읽는 이유: 바인딩 버전에 따라 없을 수 있고, 없으면 그 종류의
    영구 실패는 판정하지 않는 것이 맞다(있다고 가정해 import 시점에 터지면 GPU 수집이
    통째로 죽는다).
    """
    for attr in ("NVMLError_NotSupported", "NVMLError_NoPermission"):
        cls = getattr(pynvml, attr, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    return False


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

    def __init__(
        self,
        queue: SampleQueue,
        *,
        interval_s: float = 1.0,
        recover_after_failures: int = 5,
        recover_backoff_s: float = 60.0,
    ) -> None:
        super().__init__(queue)
        self.interval_s = interval_s
        self.recover_after_failures = recover_after_failures
        self.recover_backoff_s = recover_backoff_s
        self._handles: list[Any] = []
        self._names: list[str] = []
        self._enabled = False
        self._reason = ""
        self._samples = 0
        # 조회가 반복 실패하는 항목은 기록해 두고 다시 시도하지 않는다.
        # **미지원 예외일 때만 넣는다** — 일시적 실패를 넣으면 복구 후에도 안 돌아온다.
        self._unsupported: set[str] = set()
        # 조회 건강 상태. WARNING 로그와 describe() 로 드러난다.
        # **대시보드 노출은 아직 없다** — describe() 는 현재 단독 스모크만 읽는다.
        # 수집기 상태를 UI 까지 내보내는 경로는 별도 작업이다(PLAN.md 에 기록).
        self._consecutive_failures = 0
        self._failing_since: float | None = None
        self._last_error = ""
        self._recover_attempts = 0
        self._recoveries = 0
        self._next_recover_at = 0.0

    # ------------------------------------------------------------------ 준비

    def _init_nvml(self) -> bool:
        """NVML 을 초기화하고 장치 핸들을 잡는다. 복구 경로가 이것을 다시 쓴다.

        **성공할 때만 기존 핸들을 교체한다.** 실패했는데 핸들을 비워 버리면 `collect()`
        의 루프가 아무것도 돌지 않아 실패 카운터가 멈추고, 상태가 "실패 중"에서 얼어붙어
        복구 시도도 로그도 끊긴다 — 고치려던 조용한 실패를 다른 모양으로 다시 만드는 것이다.
        죽은 핸들이라도 들고 있으면 조회가 계속 실패하며 그 사실이 계속 드러난다.
        """
        try:
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
        except Exception as e:
            self._reason = f"NVML 초기화 실패: {e}"
            self._last_error = str(e)
            return False

        handles: list[Any] = []
        names: list[str] = []
        for i in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                handles.append(handle)
                names.append(name.decode() if isinstance(name, bytes) else name)
            except Exception as e:
                log.warning("GPU 핸들 획득 실패", extra={"index": i, "error": str(e)})

        if not handles:
            self._reason = "사용 가능한 장치 없음"
            return False

        self._handles = handles
        self._names = names
        # 재초기화 후에는 미지원 낙인을 지운다 — 다른 드라이버 버전에서 지원될 수 있다.
        self._unsupported.clear()
        return True

    def setup(self) -> None:
        if not _HAVE_NVML:
            self._reason = "nvidia-ml-py 없음"
            log.info("GPU 수집 비활성", extra={"reason": self._reason})
            return
        if self._init_nvml():
            self._enabled = True
            log.info("GPU 수집 활성", extra={"devices": self._names})
        else:
            # NVIDIA 드라이버가 없거나 GPU 가 다른 벤더인 경우. 정상 상황이다.
            log.info("GPU 수집 비활성 (NVIDIA GPU 없음으로 간주)", extra={"reason": self._reason})

    def teardown(self) -> None:
        if _HAVE_NVML and self._enabled:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # ------------------------------------------------------------------ 수집

    def _try(self, key: str, fn) -> Any:
        """지원하지 않는 항목은 한 번만 시도하고 이후 건너뛴다.

        **낙인은 미지원 예외에만 찍는다.** 드라이버 재시작 중의 일시적 실패까지 넣으면
        복구된 뒤에도 그 지표가 영영 비어 있고, 아무도 이유를 알 수 없다.
        """
        if key in self._unsupported:
            return None
        try:
            return fn()
        except Exception as e:
            if _is_permanent_error(e):
                self._unsupported.add(key)
            return None

    # -------------------------------------------------------------- 건강 상태

    def _note_failure(self, index: int, exc: BaseException, now: float) -> None:
        self._consecutive_failures += 1
        self._last_error = str(exc)
        if self._failing_since is None:
            self._failing_since = now
        if self._consecutive_failures == self.recover_after_failures:
            # 문턱에 닿는 순간 한 번만 올린다. 매 틱 WARNING 을 내면 로그가 쓸모없어진다.
            log.warning(
                "GPU 조회가 연속 실패한다 — NVML 재초기화를 시도한다",
                extra={
                    "index": index,
                    "failures": self._consecutive_failures,
                    "error": self._last_error,
                },
            )
        else:
            log.debug("GPU 조회 실패", extra={"index": index, "error": self._last_error})

    def _note_success(self) -> None:
        if self._consecutive_failures >= self.recover_after_failures:
            log.warning(
                "GPU 조회가 복구됐다",
                extra={
                    "failures": self._consecutive_failures,
                    "down_s": round(time.time() - (self._failing_since or time.time()), 1),
                    "recover_attempts": self._recover_attempts,
                },
            )
            self._recoveries += 1
        self._consecutive_failures = 0
        self._failing_since = None
        self._next_recover_at = 0.0

    def _maybe_recover(self, now: float) -> None:
        """연속 실패가 문턱을 넘었으면 NVML 을 다시 초기화한다.

        백오프를 두는 이유: 드라이버가 아직 안 올라온 동안 매 초 재초기화를 시도하면
        실패 비용만 늘어난다. 관측자가 병목이 되면 제품은 실패다.
        """
        if self._consecutive_failures < self.recover_after_failures:
            return
        if now < self._next_recover_at:
            return
        self._next_recover_at = now + self.recover_backoff_s
        self._recover_attempts += 1

        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass  # 이미 죽은 상태일 수 있다 — 여기서 막히면 복구 자체가 불가능해진다

        if self._init_nvml():
            log.warning(
                "NVML 재초기화 성공 — 다음 틱부터 수집을 재개한다",
                extra={"attempt": self._recover_attempts, "devices": self._names},
            )
        elif self._recover_attempts == 1:
            log.warning(
                "NVML 재초기화 실패 — 백오프 후 다시 시도한다",
                extra={"reason": self._reason, "backoff_s": self.recover_backoff_s},
            )
        else:
            log.debug("NVML 재초기화 실패", extra={"attempt": self._recover_attempts})

    @property
    def healthy(self) -> bool:
        return self._consecutive_failures < self.recover_after_failures

    def collect(self) -> None:
        if not self._enabled:
            return
        now = time.time()
        emitted = 0

        for index, handle in enumerate(self._handles):
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            except Exception as e:
                # 드라이버 재시작·GPU 리셋 등. 한두 번은 정상이지만 계속되면 복구해야 한다.
                self._note_failure(index, e, now)
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
            emitted += 1

        if emitted:
            self._note_success()
        else:
            self._maybe_recover(now)
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
            # 조용히 죽지 않게 드러낸다(규칙 4). 지금 이 값을 읽는 것은 단독 스모크뿐이고,
            # 사용자에게 닿는 신호는 문턱에서 올리는 WARNING 로그다.
            "healthy": self.healthy,
            "consecutive_failures": self._consecutive_failures,
            "failing_since": self._failing_since,
            "last_error": self._last_error,
            "recover_attempts": self._recover_attempts,
            "recoveries": self._recoveries,
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
    print(
        f"  건강: healthy={status['healthy']}  연속실패={status['consecutive_failures']}"
        f"  재초기화={status['recover_attempts']}회  복구={status['recoveries']}회"
    )
    if status["last_error"]:
        print(f"  마지막 오류: {status['last_error']}")

    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM gpu_metrics")[0]["c"]
        db.insert_many("gpu_metrics", COLUMNS, [s.values for s in samples])
        after = db.query("SELECT COUNT(*) AS c FROM gpu_metrics")[0]["c"]
        print(f"  DB 기록: {before} -> {after}")

    if latest["vram_total_mb"] is None or latest["util_percent"] is None:
        print("[FAIL] 필수 GPU 지표가 비어 있다")
        raise SystemExit(1)
    print("[OK] collector.gpu")

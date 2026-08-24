"""자기 리소스 예산 가드.

**모니터가 병목이 되는 순간 제품은 실패다.** Argus 자신의 CPU/메모리를 계속 재고,
예산을 넘기면 스스로 수집 주기를 늘려 물러난다.

두 가지를 의도적으로 넣었다.

- **정규화**: `cpu_percent` 는 psutil 기준으로 코어 1개를 100% 로 세므로 12스레드
  머신에서는 1200% 까지 나온다. 예산 "2%" 는 *머신 전체의 2%* 를 뜻해야 직관에 맞으므로
  논리 코어 수로 나눠 정규화한다.
- **히스테리시스**: 올릴 때는 빠르게(3회 연속 초과), 내릴 때는 느리게(12회 연속 여유).
  경계값 근처에서 스로틀이 진동하면 수집 주기가 널뛰어 데이터가 지저분해진다.

**RSS 는 단독으로 스로틀을 걸지 않는다 (2026-08-25).** 두 기계 186,840 표본에서 스로틀이
막아낸 사고가 0건이었다 — 스로틀이 걸리지 않은 `lv0` 구간이 곧 "막지 않았으면 무엇이
일어났나"의 대조군인데, 거기서도 `drop_count` 0 이고 큐 최대가 12.0%/4.8% 였다.
반대로 스로틀 3 은 수집 주기를 x10 으로 늘려 관측 해상도를 1/10 로 떨어뜨린다.

원인은 RSS 가 Argus 의 행동이 아니라 **OS 의 워킹셋 트림**을 재기 때문이다. 그래서
경고선은 **실제 압박(큐 적체·표본 유실)이 동반될 때만** 스로틀하고, 압박과 무관한
폭주는 별도의 안전망(`rss_hard_mb`)이 잡는다. 값을 올리는 것이 답이 아닌 이유는
`config/defaults.yaml` 의 `budget` 절에 있다.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import psutil

from ..config.loader import BudgetSettings
from ..logging_setup import get_logger
from .stats import STATS

log = get_logger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True)
class MemorySample:
    """한 번의 `memory_info()` 에서 뽑아낸 메모리 지표들.

    RSS 를 예산 판정에 계속 쓰는 이유: 예산 "RSS 300MB" 는 *지금 물리 메모리를 얼마나
    붙들고 있는가* 라는 뜻이고, 그건 트림된 뒤라면 실제로 줄어든 게 맞다.
    반면 **누수 판정은 `private_mb` 로 한다** — 트림이 가리지 못하는 값이라서다.
    """

    rss_mb: float
    private_mb: float | None
    peak_wset_mb: float | None
    page_faults: int | None


class BudgetGuard:
    """자기 리소스 사용량을 감시하고 스로틀 레벨을 정한다."""

    def __init__(self, settings: BudgetSettings) -> None:
        self.settings = settings
        self._proc = psutil.Process(os.getpid())
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._lock = threading.Lock()
        self._level = 0
        self._breach_streak = 0
        self._calm_streak = 0
        self._last_cpu = 0.0
        self._last_rss_mb = 0.0
        self._last_memory = MemorySample(0.0, None, None, None)
        # `drop_count` 는 **누적값**이라 그대로 보면 한 번 버린 뒤로 영원히 압박 상태가
        # 된다. 직전 값과의 차이를 봐야 "지금 버리고 있는가"에 답한다.
        self._last_drop_count = STATS.snapshot().drop_count
        self._last_pressure = False
        # 첫 호출은 항상 0.0 을 돌려주므로(직전 호출과의 델타로 계산) 미리 한 번 버린다.
        self._proc.cpu_percent(interval=None)

    # ------------------------------------------------------------------ 측정

    def _memory(self) -> MemorySample:
        """`memory_info()` 한 번으로 메모리 지표를 모두 뽑는다.

        `private`·`peak_wset`·`num_page_faults` 는 psutil 의 Windows 전용 필드다.
        다른 플랫폼에서는 없으므로 `getattr` 로 받아 None 을 남긴다 — 개발/CI 가
        비Windows 에서 돌 때 자기 계측이 통째로 죽으면 안 된다.
        """
        info = self._proc.memory_info()
        private = getattr(info, "private", None)
        peak_wset = getattr(info, "peak_wset", None)
        faults = getattr(info, "num_page_faults", None)
        return MemorySample(
            rss_mb=info.rss / _MB,
            private_mb=private / _MB if private is not None else None,
            peak_wset_mb=peak_wset / _MB if peak_wset is not None else None,
            page_faults=int(faults) if faults is not None else None,
        )

    def measure(self) -> tuple[float, float]:
        """(정규화 CPU %, RSS MB). 프로세스가 사라지는 경우는 여기선 없다."""
        raw_cpu = self._proc.cpu_percent(interval=None)
        cpu = raw_cpu / self._cpu_count
        memory = self._memory()
        with self._lock:
            self._last_cpu = cpu
            self._last_rss_mb = memory.rss_mb
            self._last_memory = memory
        return cpu, memory.rss_mb

    def _pressure(self) -> bool:
        """저장이 수집을 못 따라가고 있는가 — **실제 손해가 나는 중인지**를 묻는다.

        둘 중 하나면 압박이다:
        - 직전 주기 사이에 표본을 버렸다 (`drop_count` 증가) — 이미 손해가 났다
        - 큐가 `pressure_queue_ratio` 이상 차 있다 — 버리기 직전이다

        큐 상한을 모르면(`queue_capacity` 0) 비율은 0.0 이다. **모르는 것을 압박으로
        읽지 않는다** — 그러면 상한 등록이 끊긴 순간 상시 스로틀이 된다.
        """
        snap = STATS.snapshot()
        dropped = snap.drop_count > self._last_drop_count
        self._last_drop_count = snap.drop_count
        return dropped or snap.queue_ratio >= self.settings.pressure_queue_ratio

    def update(self) -> int:
        """한 주기 평가하고 현재 스로틀 레벨을 돌려준다."""
        cpu, rss_mb = self.measure()
        s = self.settings
        pressure = self._pressure()
        self._last_pressure = pressure
        over_cpu = cpu > s.cpu_percent
        over_hard = rss_mb > s.rss_hard_mb
        over_soft = rss_mb > s.rss_mb and pressure
        over = over_cpu or over_hard or over_soft

        with self._lock:
            max_level = len(s.throttle_multipliers) - 1
            if over:
                self._calm_streak = 0
                self._breach_streak += 1
                if self._breach_streak >= s.breach_streak_to_throttle and self._level < max_level:
                    self._level += 1
                    self._breach_streak = 0
                    log.warning(
                        "리소스 예산 초과 — 수집 주기를 늦춘다",
                        extra={
                            # `level` 이라고 쓰면 JSON 로그의 로그레벨 자리와 부딪힌다.
                            "throttle_level": self._level,
                            # 어느 조건이 걸었는지 없으면 로그만 보고는 완화가 도는지
                            # 알 수 없다. RSS 경고선은 압박이 동반돼야 걸린다.
                            "reason": (
                                "cpu" if over_cpu
                                else "rss_hard" if over_hard
                                else "rss_soft+pressure"
                            ),
                            "cpu_percent": round(cpu, 2),
                            "rss_mb": round(rss_mb, 1),
                            "queue_ratio": round(STATS.snapshot().queue_ratio, 4),
                            "limit_cpu": s.cpu_percent,
                            "limit_rss_mb": s.rss_mb,
                            "limit_rss_hard_mb": s.rss_hard_mb,
                        },
                    )
            else:
                self._breach_streak = 0
                self._calm_streak += 1
                if self._calm_streak >= s.calm_streak_to_relax and self._level > 0:
                    self._level -= 1
                    self._calm_streak = 0
                    log.info(
                        "리소스 여유 — 수집 주기를 되돌린다",
                        extra={"throttle_level": self._level},
                    )
            return self._level

    # ------------------------------------------------------------------ 조회

    @property
    def level(self) -> int:
        with self._lock:
            return self._level

    @property
    def multiplier(self) -> float:
        """수집 주기에 곱할 배수. 1.0 이면 정상."""
        with self._lock:
            level = min(self._level, len(self.settings.throttle_multipliers) - 1)
            return self.settings.throttle_multipliers[level]

    def last(self) -> tuple[float, float]:
        with self._lock:
            return self._last_cpu, self._last_rss_mb

    def last_memory(self) -> MemorySample:
        with self._lock:
            return self._last_memory


if __name__ == "__main__":  # 스모크: python -m argus.runtime.budget
    import time

    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="INFO")
    guard = BudgetGuard(load_settings().budget)
    print(f"  논리 코어: {guard._cpu_count}")
    for i in range(4):
        time.sleep(0.3)
        cpu, rss = guard.measure()
        level = guard.update()
        print(f"  [{i}] cpu={cpu:5.2f}%  rss={rss:6.1f}MB  level={level}  x{guard.multiplier}")

    # 완화 경로를 실제로 밟아 본다. 위 4회는 RSS 가 32MB 라 어느 조건에도 안 걸려
    # **아무것도 검증하지 않는다** — 통과는 증거가 아니다.
    from ..storage.queue import Sample, SampleQueue

    # 경고선만 넘고 안전망에는 안 닿는 설정. `BudgetSettings` 는 pydantic 모델이다.
    tiny = guard.settings.model_copy(update={"rss_mb": 1, "rss_hard_mb": 10_000})
    soft = BudgetGuard(tiny)
    for _ in range(tiny.breach_streak_to_throttle + 1):
        soft.update()
    quiet = soft.level

    # 같은 조건에 압박만 더한다 — "막지 않았으면 무엇이 일어났을 것인가"의 반대 짝.
    q = SampleQueue(maxsize=10)
    for i in range(10):
        q.put(Sample("t", ("a",), (i,)))  # 큐 100% -> 압박
    loud = BudgetGuard(tiny)
    for _ in range(tiny.breach_streak_to_throttle + 1):
        loud.update()
    pressed = loud.level

    print(f"  경고선 초과 · 압박 없음 -> level={quiet} (0 이어야 한다)")
    print(f"  경고선 초과 · 압박 있음 -> level={pressed} (1 이상이어야 한다)")
    if quiet != 0 or pressed < 1:
        print("[FAIL] RSS 경고선 완화가 동작하지 않는다")
        raise SystemExit(1)
    print("[OK] runtime.budget")

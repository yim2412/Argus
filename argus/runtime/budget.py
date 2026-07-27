"""자기 리소스 예산 가드.

**모니터가 병목이 되는 순간 제품은 실패다.** Argus 자신의 CPU/메모리를 계속 재고,
예산을 넘기면 스스로 수집 주기를 늘려 물러난다.

두 가지를 의도적으로 넣었다.

- **정규화**: `cpu_percent` 는 psutil 기준으로 코어 1개를 100% 로 세므로 12스레드
  머신에서는 1200% 까지 나온다. 예산 "2%" 는 *머신 전체의 2%* 를 뜻해야 직관에 맞으므로
  논리 코어 수로 나눠 정규화한다.
- **히스테리시스**: 올릴 때는 빠르게(3회 연속 초과), 내릴 때는 느리게(12회 연속 여유).
  경계값 근처에서 스로틀이 진동하면 수집 주기가 널뛰어 데이터가 지저분해진다.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import psutil

from ..config.loader import BudgetSettings
from ..logging_setup import get_logger

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

    def update(self) -> int:
        """한 주기 평가하고 현재 스로틀 레벨을 돌려준다."""
        cpu, rss_mb = self.measure()
        s = self.settings
        over = cpu > s.cpu_percent or rss_mb > s.rss_mb

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
                            "level": self._level,
                            "cpu_percent": round(cpu, 2),
                            "rss_mb": round(rss_mb, 1),
                            "limit_cpu": s.cpu_percent,
                            "limit_rss_mb": s.rss_mb,
                        },
                    )
            else:
                self._breach_streak = 0
                self._calm_streak += 1
                if self._calm_streak >= s.calm_streak_to_relax and self._level > 0:
                    self._level -= 1
                    self._calm_streak = 0
                    log.info("리소스 여유 — 수집 주기를 되돌린다", extra={"level": self._level})
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
    print("[OK] runtime.budget")

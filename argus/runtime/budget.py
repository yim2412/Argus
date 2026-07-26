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

import psutil

from ..config.loader import BudgetSettings
from ..logging_setup import get_logger

log = get_logger(__name__)


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
        # 첫 호출은 항상 0.0 을 돌려주므로(직전 호출과의 델타로 계산) 미리 한 번 버린다.
        self._proc.cpu_percent(interval=None)

    # ------------------------------------------------------------------ 측정

    def measure(self) -> tuple[float, float]:
        """(정규화 CPU %, RSS MB). 프로세스가 사라지는 경우는 여기선 없다."""
        raw_cpu = self._proc.cpu_percent(interval=None)
        cpu = raw_cpu / self._cpu_count
        rss_mb = self._proc.memory_info().rss / (1024 * 1024)
        with self._lock:
            self._last_cpu = cpu
            self._last_rss_mb = rss_mb
        return cpu, rss_mb

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

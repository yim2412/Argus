"""시간 공백 감지 — 절전 복귀·시각 변경·심한 정지.

**왜 필요한가**: PC 는 절전에 들어간다. 상주 프로그램이 그걸 겪지 않을 방법은 없다.
깨어나면 다음 세 가지가 한꺼번에 망가진다.

1. **속도 계산이 틀린다.** 디스크 누적 카운터의 차분을 경과 시간으로 나누는데, 경과가
   3시간이면 그 구간 평균이 나온다. 그 값은 "지금"을 대표하지 않는다. 더 나쁜 경우
   장치가 재연결되어 카운터가 되감기면 음수가 된다.
2. **프로세스 이벤트가 폭주한다.** 절전 전후로 프로세스 목록이 크게 달라지면 수백 건의
   생성·종료 이벤트가 한꺼번에 쏟아진다. 실제로는 그 사이에 일어난 일을 못 본 것뿐이다.
3. **이후 단계가 공백을 이상으로 오인한다.** 베이스라인 학습(Phase 3)과 변화점
   탐지(Phase 8)는 이 구간을 반드시 제외해야 한다.

**감지 방법**: 벽시계(`time.time`)와 단조시계(`time.monotonic`) 두 개를 함께 본다.
Windows 에서 단조시계가 절전 시간을 포함하는지는 버전·API 에 따라 다르므로 어느 한쪽에
의존하지 않고, **둘 중 하나라도** 예상 주기보다 크게 튀면 공백으로 본다. 이러면 절전뿐
아니라 시각 수동 변경·NTP 보정·심한 스톨도 같이 잡힌다.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from ..logging_setup import get_logger
from .supervisor import Component

log = get_logger(__name__)


class GapMonitor(Component):
    """주기적으로 시간 흐름을 확인하고, 공백이 생기면 알린다."""

    name = "gap_monitor"
    # 공백 감지는 스로틀 대상이 아니다. 주기가 늘어나면 정상 지연과 공백을 구분하기 어려워진다.
    throttleable = False

    def __init__(
        self,
        *,
        interval_s: float = 1.0,
        threshold_s: float = 30.0,
        on_gap: Callable[[float, dict[str, Any]], None] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.threshold_s = threshold_s
        self._on_gap = on_gap
        self._last_wall = 0.0
        self._last_mono = 0.0
        self._gaps = 0
        self._last_gap_s = 0.0

    def setup(self) -> None:
        self._last_wall = time.time()
        self._last_mono = time.monotonic()

    def tick(self) -> None:
        wall = time.time()
        mono = time.monotonic()
        wall_delta = wall - self._last_wall
        mono_delta = mono - self._last_mono
        self._last_wall = wall
        self._last_mono = mono

        # 벽시계가 거꾸로 갔다 = 시각이 뒤로 조정됐다. 이것도 공백으로 취급해야
        # 이후 단계에서 시간 역행 데이터를 만나지 않는다.
        backwards = wall_delta < -1.0

        observed = max(wall_delta, mono_delta)
        expected = self.interval_s
        if not backwards and observed - expected < self.threshold_s:
            return

        gap = wall_delta if abs(wall_delta) > abs(mono_delta) else mono_delta
        self._gaps += 1
        self._last_gap_s = gap

        detail = {
            "wall_delta_s": round(wall_delta, 3),
            "mono_delta_s": round(mono_delta, 3),
            "expected_s": expected,
            # 벽시계만 튀었으면 시각 조정, 둘 다 튀었으면 절전·정지로 본다.
            "likely_cause": (
                "clock_backwards"
                if backwards
                else "clock_change"
                if mono_delta - expected < self.threshold_s
                else "suspend_or_stall"
            ),
        }
        log.warning("시간 공백 감지 — 수집기 상태를 재설정한다", extra=detail)

        if self._on_gap is not None:
            try:
                self._on_gap(gap, detail)
            except Exception:
                # 복구 처리가 실패해도 감시 자체는 계속되어야 한다.
                log.exception("공백 복구 처리 실패")

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "threshold_s": self.threshold_s,
            "gaps": self._gaps,
            "last_gap_s": round(self._last_gap_s, 1),
        }


def gap_event_row(ts: float, gap: float, detail: dict[str, Any]) -> tuple:
    """`system_events` 한 행."""
    return (ts, "time_gap", round(gap, 3), json.dumps(detail, ensure_ascii=False))


if __name__ == "__main__":  # 스모크: python -m argus.runtime.gapmon
    from ..logging_setup import setup

    setup(level="WARNING")

    fired: list[tuple[float, dict]] = []
    monitor = GapMonitor(
        interval_s=0.1, threshold_s=0.5, on_gap=lambda g, d: fired.append((g, d))
    )
    monitor.setup()

    # 정상 틱 — 공백으로 판정되면 안 된다
    for _ in range(3):
        time.sleep(0.1)
        monitor.tick()
    print(f"  정상 틱 3회 후 감지된 공백: {len(fired)}건 (0 이어야 정상)")
    if fired:
        print("[FAIL] 정상 동작을 공백으로 오인했다")
        raise SystemExit(1)

    # 절전 흉내 — 두 시계를 함께 되돌려 큰 공백을 만든다
    monitor._last_wall -= 3600.0
    monitor._last_mono -= 3600.0
    monitor.tick()
    print(f"  1시간 공백 주입 후: {len(fired)}건")
    if not fired:
        print("[FAIL] 공백을 감지하지 못했다")
        raise SystemExit(1)
    gap, detail = fired[-1]
    print(f"    공백 {gap:.0f}초, 추정 원인 {detail['likely_cause']}")
    if detail["likely_cause"] != "suspend_or_stall":
        print(f"[FAIL] 원인 추정이 잘못됐다: {detail['likely_cause']}")
        raise SystemExit(1)

    # 벽시계만 앞으로 — 시각 변경으로 판정되어야 한다
    monitor._last_wall -= 600.0
    monitor.tick()
    print(f"  벽시계만 10분 점프: 추정 원인 {fired[-1][1]['likely_cause']}")
    if fired[-1][1]["likely_cause"] != "clock_change":
        print(f"[FAIL] 시각 변경을 절전으로 오인했다")
        raise SystemExit(1)

    # 벽시계 역행
    monitor._last_wall += 120.0
    monitor.tick()
    print(f"  벽시계 역행: 추정 원인 {fired[-1][1]['likely_cause']}")
    if fired[-1][1]["likely_cause"] != "clock_backwards":
        print("[FAIL] 시각 역행을 감지하지 못했다")
        raise SystemExit(1)

    print(f"  상태: {monitor.describe()}")
    print("[OK] runtime.gapmon")

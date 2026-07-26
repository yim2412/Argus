"""수집기 공통 골격.

수집기는 큐에 넣기만 하고 DB 는 건드리지 않는다. 쓰기는 `BatchWriter` 하나가 전담한다.

`RateTracker` 를 여기 둔 이유: psutil 이 주는 디스크·네트워크 카운터는 부팅 이후
**누적값**이다. 누적값을 그대로 저장하면 나중에 모든 질의에서 차분을 다시 계산해야 하고,
카운터가 32비트 랩어라운드나 장치 재연결로 되감기면 음수 급증이 그대로 "이상"으로
보인다. 수집 시점에 초당 속도로 바꿔서 넣는다.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..runtime.supervisor import Component
from ..storage.queue import Sample, SampleQueue


class RateTracker:
    """누적 카운터 → 초당 속도."""

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, value: float, now: float | None = None) -> float | None:
        """이전 관측 대비 초당 증가량. 첫 관측이면 None."""
        now = now if now is not None else time.time()
        prev = self._prev.get(key)
        self._prev[key] = (now, value)
        if prev is None:
            return None
        prev_ts, prev_value = prev
        dt = now - prev_ts
        if dt <= 0:
            return None
        delta = value - prev_value
        if delta < 0:
            # 카운터 되감김(랩어라운드·장치 재연결). 음수 급증을 "이상"으로 오인하지
            # 않도록 이번 구간은 버리고 다음 관측부터 다시 잰다.
            return None
        return delta / dt

    def drop(self, key: str) -> None:
        """추적을 그만둔다. 프로세스가 죽으면 반드시 불러야 한다 —
        안 그러면 pid 별 키가 계속 쌓여 상주 프로그램에서 메모리 누수가 된다."""
        self._prev.pop(key, None)

    def reset(self) -> None:
        self._prev.clear()

    def __len__(self) -> int:
        return len(self._prev)


class Collector(Component):
    """큐에 샘플을 넣는 컴포넌트."""

    def __init__(self, queue: SampleQueue) -> None:
        self.queue = queue
        self._pending: list[Sample] = []

    def emit(self, table: str, columns: Sequence[str], values: Sequence[Any]) -> None:
        """한 행을 큐에 넣는다(틱 끝에 한꺼번에 전달)."""
        self._pending.append(Sample(table, tuple(columns), tuple(values)))

    def collect(self) -> None:
        """하위 클래스가 구현한다. `emit()` 으로 결과를 낸다."""
        raise NotImplementedError

    def tick(self) -> None:
        self._pending.clear()
        self.collect()
        if self._pending:
            # 한 틱의 행들을 한 번에 넣어 락 획득 횟수를 줄인다.
            self.queue.put_many(self._pending)
            self._pending.clear()

    def on_time_gap(self, gap_s: float) -> None:
        """절전 복귀·시각 점프 직후에 불린다.

        누적 카운터 기반 상태를 버려야 한다. 공백을 사이에 둔 두 관측의 차분은
        "지금"을 대표하지 않기 때문이다. 기본 동작은 속도 추적 초기화이고,
        더 할 일이 있는 수집기가 덮어쓴다.
        """
        self._rates_reset()

    def _rates_reset(self) -> None:
        tracker = getattr(self, "_rates", None)
        if isinstance(tracker, RateTracker):
            tracker.reset()

    def describe(self) -> dict[str, Any]:
        """스모크·진단용 상태 요약. 하위 클래스가 덮어쓴다."""
        return {"name": self.name, "interval_s": self.interval_s}

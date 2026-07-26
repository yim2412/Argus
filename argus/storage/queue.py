"""수집 → 저장 사이의 큐.

**수집 경로는 절대 블로킹하지 않는다.** 디스크가 잠깐 느려졌다고 수집 스레드가 멈추면
바로 그 순간의 데이터를 잃는데, 하필 그때가 가장 기록이 필요한 순간이다.

그래서 큐가 가득 차면 기다리지 않고 **오래된 것부터 버린다.** 최신 데이터가 더 가치
있고, 버린 사실은 `drop_count` 로 남아 자기 계측에 드러난다. Phase 1 완료 기준이
바로 이 값이 0 인 것이다 — 0 이 아니면 저장이 수집을 못 따라가고 있다는 뜻이다.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Sequence

from ..runtime.stats import STATS


@dataclass(frozen=True, slots=True)
class Sample:
    """DB 한 행. `columns` 가 같은 것끼리 묶여 executemany 로 들어간다."""

    table: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]


class SampleQueue:
    """상한이 있는 FIFO. 넘치면 앞에서 버린다."""

    def __init__(self, maxsize: int) -> None:
        self._dq: deque[Sample] = deque()
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._dropped = 0

    def put(self, sample: Sample) -> None:
        with self._lock:
            if len(self._dq) >= self._maxsize:
                self._dq.popleft()
                self._dropped += 1
                STATS.add_drops(1)
            self._dq.append(sample)
        STATS.set_queue_depth(len(self._dq))

    def put_many(self, samples: Sequence[Sample]) -> None:
        if not samples:
            return
        with self._lock:
            overflow = len(self._dq) + len(samples) - self._maxsize
            if overflow > 0:
                for _ in range(min(overflow, len(self._dq))):
                    self._dq.popleft()
                self._dropped += overflow
                STATS.add_drops(overflow)
            self._dq.extend(samples)
        STATS.set_queue_depth(len(self._dq))

    def drain(self, limit: int) -> list[Sample]:
        """최대 `limit` 개를 꺼낸다. 큐가 비어 있으면 빈 리스트."""
        out: list[Sample] = []
        with self._lock:
            for _ in range(min(limit, len(self._dq))):
                out.append(self._dq.popleft())
            depth = len(self._dq)
        STATS.set_queue_depth(depth)
        return out

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._dq)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped


if __name__ == "__main__":  # 스모크: python -m argus.storage.queue
    q = SampleQueue(maxsize=5)
    for i in range(8):
        q.put(Sample("t", ("a",), (i,)))
    remaining = [s.values[0] for s in q.drain(100)]
    print(f"  8개 투입 / 상한 5 → 남은 값: {remaining}")
    print(f"  버린 수: {q.dropped}")
    if remaining != [3, 4, 5, 6, 7] or q.dropped != 3:
        print("[FAIL] 오래된 것부터 버려지지 않았다")
        raise SystemExit(1)
    print("[OK] storage.queue")

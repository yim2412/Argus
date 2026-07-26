"""런타임 카운터.

수집→저장 경로의 상태를 한 곳에 모아 자기 계측이 읽어간다. Phase 0 에서는 대부분
0 이고, Phase 1 에서 배치 writer 가 실제로 채운다.

`drop_count` 가 0 이 아니면 수집이 저장을 앞지르고 있다는 뜻이다. Phase 1 의 완료
기준이 바로 이 값이 0 인 것이다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class _Snapshot:
    queue_depth: int
    drop_count: int
    write_latency_ms: float


class RuntimeStats:
    """스레드 안전 카운터 모음."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue_depth = 0
        self._drop_count = 0
        self._write_latency_ms = 0.0

    def set_queue_depth(self, value: int) -> None:
        with self._lock:
            self._queue_depth = value

    def add_drops(self, count: int) -> None:
        with self._lock:
            self._drop_count += count

    def record_write_latency(self, ms: float) -> None:
        with self._lock:
            self._write_latency_ms = ms

    def snapshot(self) -> _Snapshot:
        with self._lock:
            return _Snapshot(self._queue_depth, self._drop_count, self._write_latency_ms)


# 프로세스 전역 인스턴스. 컴포넌트들이 공유한다.
STATS = RuntimeStats()

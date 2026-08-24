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
    # 큐 상한. **여기에 숫자를 적지 않는다** — 상한은 `storage.queue_max_rows` 한 곳에
    # 있고, 큐가 생길 때 자기 값을 등록한다. 예산 가드가 "몇 % 찼나"를 물으려면 상한이
    # 필요한데, 그 값을 예산 절에 복제하면 설정이 두 곳이 되어 설계 규칙 3 위반이다.
    queue_capacity: int = 0

    @property
    def queue_ratio(self) -> float:
        """0.0~1.0. 상한이 등록되지 않았으면 0.0 — 모르는 것을 압박으로 읽지 않는다."""
        if self.queue_capacity <= 0:
            return 0.0
        return self.queue_depth / self.queue_capacity


class RuntimeStats:
    """스레드 안전 카운터 모음."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue_depth = 0
        self._queue_capacity = 0
        self._drop_count = 0
        self._write_latency_ms = 0.0

    def set_queue_depth(self, value: int) -> None:
        with self._lock:
            self._queue_depth = value

    def set_queue_capacity(self, value: int) -> None:
        """큐가 자기 상한을 등록한다. 상주에는 큐가 하나뿐이라 마지막 등록이 곧 그것이다."""
        with self._lock:
            self._queue_capacity = value

    def add_drops(self, count: int) -> None:
        with self._lock:
            self._drop_count += count

    def record_write_latency(self, ms: float) -> None:
        with self._lock:
            self._write_latency_ms = ms

    def snapshot(self) -> _Snapshot:
        with self._lock:
            return _Snapshot(
                self._queue_depth,
                self._drop_count,
                self._write_latency_ms,
                self._queue_capacity,
            )


# 프로세스 전역 인스턴스. 컴포넌트들이 공유한다.
STATS = RuntimeStats()

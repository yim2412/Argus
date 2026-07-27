"""자기 계측 — Argus 자신의 리소스 사용량을 DB 에 기록한다.

수집기보다 먼저 만드는 이유: 모니터링 도구의 1순위 실패 모드가 "관측 행위가 관측
대상을 오염시키는 것"이기 때문이다. 나중에 CPU 수집기를 붙였을 때 CPU 가 올라가면,
그게 실제 부하인지 우리 탓인지 구분할 근거가 여기 있어야 한다.

핸들 수를 같이 남긴다 — Windows 에서 핸들 누수는 메모리보다 먼저 드러나는 신호다.
"""

from __future__ import annotations

import os
import time

import psutil

from ..logging_setup import get_logger
from ..storage.hot import Database
from .budget import BudgetGuard
from .stats import STATS
from .supervisor import Component

log = get_logger(__name__)

_COLUMNS = [
    "ts",
    "cpu_percent",
    "rss_mb",
    "private_mb",
    "peak_wset_mb",
    "page_faults",
    "threads",
    "handles",
    "queue_depth",
    "drop_count",
    "write_latency_ms",
    "throttle_level",
]


class SelfTelemetry(Component):
    """주기적으로 자기 상태를 `self_telemetry` 에 한 행씩 넣는다."""

    name = "self_telemetry"
    # 자기 계측은 스로틀 대상이 아니다. 부하가 걸려 스로틀이 켜졌을 때가
    # 오히려 기록이 가장 필요한 순간이다.
    throttleable = False

    def __init__(self, db: Database, guard: BudgetGuard, interval_s: float = 5.0) -> None:
        self.db = db
        self.guard = guard
        self.interval_s = interval_s
        self._proc = psutil.Process(os.getpid())

    def tick(self) -> None:
        cpu, rss_mb = self.guard.last()
        memory = self.guard.last_memory()
        snapshot = STATS.snapshot()

        try:
            threads = self._proc.num_threads()
        except psutil.Error:
            threads = None
        try:
            # num_handles 는 Windows 전용. 다른 플랫폼에서는 없다.
            handles = self._proc.num_handles() if hasattr(self._proc, "num_handles") else None
        except psutil.Error:
            handles = None

        self.db.insert_many(
            "self_telemetry",
            _COLUMNS,
            [
                (
                    time.time(),
                    round(cpu, 3),
                    round(rss_mb, 2),
                    round(memory.private_mb, 2) if memory.private_mb is not None else None,
                    round(memory.peak_wset_mb, 2) if memory.peak_wset_mb is not None else None,
                    memory.page_faults,
                    threads,
                    handles,
                    snapshot.queue_depth,
                    snapshot.drop_count,
                    round(snapshot.write_latency_ms, 3),
                    self.guard.level,
                )
            ],
        )


class BudgetMonitor(Component):
    """예산 가드를 주기적으로 갱신하는 컴포넌트."""

    name = "budget"
    throttleable = False  # 스로틀을 정하는 주체가 스로틀을 받으면 회복이 느려진다

    def __init__(self, guard: BudgetGuard) -> None:
        self.guard = guard
        self.interval_s = guard.settings.check_interval_s

    def tick(self) -> None:
        self.guard.update()


if __name__ == "__main__":  # 스모크: python -m argus.runtime.selftel
    from ..config.loader import load_settings
    from ..logging_setup import setup
    from .supervisor import Supervisor

    setup(level="INFO")
    settings = load_settings()
    guard = BudgetGuard(settings.budget)

    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]

        sup = Supervisor(multiplier_fn=lambda: guard.multiplier)
        sup.add(BudgetMonitor(guard))
        sup.add(SelfTelemetry(db, guard, interval_s=0.3))
        sup.start()
        time.sleep(1.5)
        sup.stop()

        after = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]
        rows = db.query("SELECT * FROM self_telemetry ORDER BY ts DESC LIMIT 3")
        print(f"  기록: {before} -> {after}행 (+{after - before})")
        for row in rows:
            print(
                f"    cpu={row['cpu_percent']}%  rss={row['rss_mb']}MB  "
                f"private={row['private_mb']}MB  peak_wset={row['peak_wset_mb']}MB  "
                f"faults={row['page_faults']}  threads={row['threads']}  "
                f"handles={row['handles']}  level={row['throttle_level']}"
            )
        if after <= before:
            print("[FAIL] 자기 계측이 기록되지 않았다")
            raise SystemExit(1)
        # 누수 판정의 정본이 될 컬럼이므로, 비어 있으면 실패로 본다(Windows 기준).
        if os.name == "nt" and rows and rows[0]["private_mb"] is None:
            print("[FAIL] private_mb 가 기록되지 않았다 — 누수 추세를 판정할 수 없다")
            raise SystemExit(1)
    print("[OK] runtime.selftel")

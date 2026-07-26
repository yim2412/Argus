"""시스템 카운터 수집기 (T1, 1Hz).

psutil 과 PDH 를 합쳐 `metrics_raw` 한 행을 만든다.
  - psutil : CPU/메모리/디스크·네트워크 처리량 — "얼마나 썼는가"
  - PDH    : 디스크 응답시간/큐, 컨텍스트 스위치, 실효 클럭 — "얼마나 기다렸는가"

PDH 가 없거나 일부 카운터를 못 쓰는 환경에서도 psutil 몫은 그대로 수집된다.
해당 컬럼만 NULL 이 되고, 그 사실은 `describe()` 와 로그에 남는다.
"""

from __future__ import annotations

import json
import time
from typing import Any

import psutil

from ..logging_setup import get_logger
from ..storage.queue import SampleQueue
from .base import Collector, RateTracker
from .pdh import PdhCounters

log = get_logger(__name__)

COLUMNS = (
    "ts",
    "cpu_total",
    "cpu_per_core",
    "cpu_max_core",
    "cpu_freq_mhz",
    "cpu_perf_percent",
    "mem_used_mb",
    "mem_avail_mb",
    "mem_percent",
    "swap_used_mb",
    "disk_read_bps",
    "disk_write_bps",
    "disk_read_iops",
    "disk_write_iops",
    "disk_queue",
    "disk_resp_ms",
    "net_rx_bps",
    "net_tx_bps",
    "ctx_switches_ps",
    "proc_count",
    "thread_count",
)

_MB = 1024 * 1024


class SystemCollector(Collector):
    """1Hz 시스템 스냅샷."""

    name = "system"

    def __init__(
        self, queue: SampleQueue, *, interval_s: float = 1.0, pdh_enabled: bool = True
    ) -> None:
        super().__init__(queue)
        self.interval_s = interval_s
        self._rates = RateTracker()
        self._pdh = PdhCounters() if pdh_enabled else None
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._samples = 0

    def setup(self) -> None:
        # psutil 의 cpu_percent 는 직전 호출과의 차이로 계산한다. 첫 호출은 항상 0.0 이
        # 나오므로 여기서 미리 한 번 버려, 첫 저장 값부터 의미가 있게 한다.
        psutil.cpu_percent(percpu=True)
        psutil.cpu_percent()

        if self._pdh is not None:
            self._pdh.open()
            if self._pdh.available:
                log.info(
                    "PDH 카운터 활성",
                    extra={"counters": sorted(self._pdh.resolved_paths)},
                )
            else:
                log.warning(
                    "PDH 를 쓸 수 없다 — 디스크 응답시간 등 증상 지표 없이 동작한다",
                    extra={"failures": self._pdh.failures},
                )

    def teardown(self) -> None:
        if self._pdh is not None:
            self._pdh.close()

    def on_time_gap(self, gap_s: float) -> None:
        """절전 복귀 처리.

        누적 카운터 차분을 버리는 것에 더해 PDH 질의를 새로 연다. 절전 동안 성능
        카운터 제공자가 재시작되면 기존 핸들이 무효가 될 수 있고, 속도형 카운터는
        어차피 표본을 다시 쌓아야 한다.
        """
        self._rates.reset()
        if self._pdh is not None:
            self._pdh.close()
            self._pdh = PdhCounters().open()
            if not self._pdh.available:
                log.warning("복귀 후 PDH 재개방 실패", extra={"failures": self._pdh.failures})
        # psutil 의 CPU 사용률도 직전 호출과의 차분이라 기준점을 다시 잡는다.
        psutil.cpu_percent(percpu=True)
        psutil.cpu_percent()
        log.info("시스템 수집기 재설정 완료", extra={"gap_s": round(gap_s, 1)})

    # ------------------------------------------------------------------ 수집

    def collect(self) -> None:
        now = time.time()

        per_core = psutil.cpu_percent(percpu=True)
        cpu_total = sum(per_core) / len(per_core) if per_core else None
        cpu_max_core = max(per_core) if per_core else None

        freq = psutil.cpu_freq()
        cpu_freq_mhz = round(freq.current, 1) if freq and freq.current else None

        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()

        disk_read_bps = disk_write_bps = disk_read_iops = disk_write_iops = None
        if disk is not None:
            disk_read_bps = self._rates.rate("disk_read", disk.read_bytes, now)
            disk_write_bps = self._rates.rate("disk_write", disk.write_bytes, now)
            disk_read_iops = self._rates.rate("disk_rc", disk.read_count, now)
            disk_write_iops = self._rates.rate("disk_wc", disk.write_count, now)

        net_rx_bps = net_tx_bps = None
        if net is not None:
            net_rx_bps = self._rates.rate("net_rx", net.bytes_recv, now)
            net_tx_bps = self._rates.rate("net_tx", net.bytes_sent, now)

        pdh_values: dict[str, float] = {}
        if self._pdh is not None and self._pdh.available:
            pdh_values = self._pdh.collect()

        def rounded(value: float | None, digits: int = 2) -> float | None:
            return round(value, digits) if value is not None else None

        def pdh(key: str, digits: int = 3) -> float | None:
            value = pdh_values.get(key)
            return round(value, digits) if value is not None else None

        proc_count = pdh_values.get("proc_count")
        thread_count = pdh_values.get("thread_count")

        self.emit(
            "metrics_raw",
            COLUMNS,
            (
                now,
                rounded(cpu_total),
                json.dumps([round(c, 1) for c in per_core]) if per_core else None,
                rounded(cpu_max_core),
                cpu_freq_mhz,
                pdh("cpu_perf_percent", 2),
                rounded(vm.used / _MB, 1),
                rounded(vm.available / _MB, 1),
                rounded(vm.percent),
                rounded(swap.used / _MB, 1),
                rounded(disk_read_bps, 1),
                rounded(disk_write_bps, 1),
                rounded(disk_read_iops, 1),
                rounded(disk_write_iops, 1),
                pdh("disk_queue"),
                pdh("disk_resp_ms"),
                rounded(net_rx_bps, 1),
                rounded(net_tx_bps, 1),
                pdh("ctx_switches_ps", 1),
                int(proc_count) if proc_count is not None else None,
                int(thread_count) if thread_count is not None else None,
            ),
        )
        self._samples += 1

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "interval_s": self.interval_s,
            "samples": self._samples,
            "pdh": bool(self._pdh and self._pdh.available),
        }
        if self._pdh is not None and self._pdh.failures:
            out["pdh_failures"] = self._pdh.failures
        return out


if __name__ == "__main__":  # 스모크: python -m argus.collector.system
    from ..logging_setup import setup
    from ..storage.hot import Database

    setup(level="INFO")
    queue = SampleQueue(maxsize=1000)
    collector = SystemCollector(queue, interval_s=1.0)
    collector.setup()
    try:
        for i in range(3):
            time.sleep(1.0)
            collector.tick()
        samples = queue.drain(100)
        # teardown 이 PDH 를 닫으므로 상태는 그 전에 찍어야 실제 값이 나온다.
        status = collector.describe()
    finally:
        collector.teardown()

    print(f"  수집: {len(samples)}행")
    latest = dict(zip(COLUMNS, samples[-1].values))
    for key in COLUMNS:
        if key == "cpu_per_core":
            cores = json.loads(latest[key]) if latest[key] else []
            print(f"    {key:18} {len(cores)}코어  최대 {max(cores) if cores else '-'}%")
        elif key != "ts":
            print(f"    {key:18} {latest[key]}")

    print(f"  상태: {status}")

    missing = [k for k, v in latest.items() if v is None]
    if missing:
        print(f"[FAIL] 값이 비어 있는 컬럼: {missing}")
        raise SystemExit(1)

    # 실제로 DB 에 들어가는지까지 확인한다
    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM metrics_raw")[0]["c"]
        db.insert_many("metrics_raw", COLUMNS, [s.values for s in samples])
        after = db.query("SELECT COUNT(*) AS c FROM metrics_raw")[0]["c"]
        print(f"  DB 기록: {before} -> {after}")
    print("[OK] collector.system")

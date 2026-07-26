"""네트워크 연결 수집기.

**이 테이블이 저장소에서 가장 민감하다.** 어떤 프로그램이 어디에 접속했는지가 그대로
남는다. 로컬에만 저장되고 외부로 나가지 않지만, 그래도 설정으로 끌 수 있어야 하고
(`collector.network.enabled: false`) 보존 기간도 짧게 잡는다.

**용도**: Phase 6 프로세스 지문의 "평소 접속하는 대상" 항목과 Phase 13 보안 탐지의
"짧은 시간에 낯선 IP 다수 연결" 판정.

빈도를 낮게(30초) 잡는 이유는 연결 목록 조회가 무겁기도 하지만, 연결은 리소스 사용량과
달리 초 단위로 볼 이유가 없기 때문이다.

**DNS 역조회는 하지 않는다.** 매 연결마다 이름을 물으면 그 자체가 네트워크 트래픽을
만들어 관측 대상을 오염시키고, 느리고, 조회 기록이 외부에 남는다. 원격 IP 그대로 둔다.
"""

from __future__ import annotations

import time
from typing import Any

import psutil

from ..logging_setup import get_logger
from ..storage.queue import SampleQueue
from .base import Collector
from .procsource import normalize_name

log = get_logger(__name__)

COLUMNS = ("ts", "pid", "name", "laddr", "lport", "raddr", "rport", "status", "family")

# 원격 주소가 없거나 의미 없는 상태는 저장하지 않는다. LISTEN 은 예외로 남긴다 —
# 열린 포트 자체가 보안 탐지의 관심사이기 때문이다.
_SKIP_STATUS = {"NONE"}


class NetworkCollector(Collector):
    """활성 연결 스냅샷 (저빈도)."""

    name = "network"

    def __init__(
        self, queue: SampleQueue, *, interval_s: float = 30.0, max_rows: int = 500
    ) -> None:
        super().__init__(queue)
        self.interval_s = interval_s
        self.max_rows = max_rows
        self._name_cache: dict[int, str] = {}
        self._degraded = False
        self._reason = ""
        self._snapshots = 0
        self._last_rows = 0
        self._last_ms = 0.0

    def _proc_name(self, pid: int | None) -> str | None:
        """pid → 프로세스명 (캐시). 연결마다 psutil.Process 를 만들면 비싸다."""
        if not pid:
            return None
        cached = self._name_cache.get(pid)
        if cached is not None:
            return cached
        try:
            name = normalize_name(psutil.Process(pid).name())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = ""
        self._name_cache[pid] = name
        return name or None

    def collect(self) -> None:
        started = time.perf_counter()
        now = time.time()

        try:
            connections = psutil.net_connections(kind="inet")
        except psutil.AccessDenied:
            # 권한이 부족하면 자기 프로세스 범위로 축소한다. 조용히 비지 않게 이유를 남긴다.
            if not self._degraded:
                self._degraded = True
                self._reason = "권한 부족 — 자기 프로세스 연결만 수집한다"
                log.info("네트워크 수집 축소", extra={"reason": self._reason})
            try:
                connections = psutil.Process().net_connections(kind="inet")
            except Exception as e:
                self._reason = f"연결 조회 불가: {e}"
                return
        except Exception as e:
            log.debug("연결 조회 실패", extra={"error": str(e)})
            return

        # pid 가 재사용되므로 캐시가 무한정 자라지 않게 스냅샷마다 정리한다.
        live_pids = {c.pid for c in connections if c.pid}
        self._name_cache = {p: n for p, n in self._name_cache.items() if p in live_pids}

        rows = 0
        for conn in connections:
            if rows >= self.max_rows:
                break
            status = conn.status or ""
            if status in _SKIP_STATUS:
                continue

            laddr = conn.laddr.ip if conn.laddr else None
            lport = conn.laddr.port if conn.laddr else None
            raddr = conn.raddr.ip if conn.raddr else None
            rport = conn.raddr.port if conn.raddr else None

            # 원격도 없고 듣고 있지도 않으면 남길 정보가 없다.
            if raddr is None and status != "LISTEN":
                continue

            self.emit(
                "net_connections",
                COLUMNS,
                (
                    now,
                    conn.pid,
                    self._proc_name(conn.pid),
                    laddr,
                    lport,
                    raddr,
                    rport,
                    status,
                    "IPv6" if conn.family.name == "AF_INET6" else "IPv4",
                ),
            )
            rows += 1

        self._snapshots += 1
        self._last_rows = rows
        self._last_ms = (time.perf_counter() - started) * 1000
        if rows >= self.max_rows:
            log.debug("연결 스냅샷이 상한에 걸렸다", extra={"limit": self.max_rows})

    def on_time_gap(self, gap_s: float) -> None:
        # pid 는 재부팅·절전 후 재사용되므로 이름 캐시를 버린다. 안 그러면 엉뚱한
        # 프로세스 이름이 연결에 붙는다.
        self._name_cache.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "snapshots": self._snapshots,
            "last_rows": self._last_rows,
            "last_ms": round(self._last_ms, 1),
            "degraded": self._degraded,
            "reason": self._reason,
        }


if __name__ == "__main__":  # 스모크: python -m argus.collector.network
    from ..logging_setup import setup
    from ..storage.hot import Database

    setup(level="INFO")
    queue = SampleQueue(maxsize=5000)
    collector = NetworkCollector(queue, interval_s=1.0)
    collector.tick()
    status = collector.describe()
    samples = queue.drain(10000)

    print(f"  스냅샷: {len(samples)}행  소요 {status['last_ms']}ms  축소모드={status['degraded']}")
    if status["reason"]:
        print(f"  사유: {status['reason']}")

    listening = [s for s in samples if s.values[7] == "LISTEN"]
    established = [s for s in samples if s.values[7] == "ESTABLISHED"]
    print(f"  LISTEN {len(listening)}건 · ESTABLISHED {len(established)}건")

    print("  연결 예시 (원격 주소는 개인정보라 일부만):")
    for s in established[:5]:
        v = s.values
        print(f"    {v[2] or '(알 수 없음)':24} -> {v[5]}:{v[6]}  [{v[8]}]")

    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM net_connections")[0]["c"]
        db.insert_many("net_connections", COLUMNS, [s.values for s in samples])
        after = db.query("SELECT COUNT(*) AS c FROM net_connections")[0]["c"]
        print(f"  DB 기록: {before} -> {after}")

    if not samples:
        print("[FAIL] 연결을 하나도 수집하지 못했다")
        raise SystemExit(1)
    print("[OK] collector.network")

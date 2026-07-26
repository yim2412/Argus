"""프로세스 수집기 (T2).

**계층은 수집이 아니라 저장에 둔다.**

원래 계획은 "전체는 10초마다, 활성 집합만 1초마다 수집"이었다. psutil 로는 전체
스캔이 1.4초나 걸려 수집 자체를 아껴야 했기 때문이다. 그런데 PDH `Process V2`
와일드카드 질의가 전 프로세스를 13ms 에 주므로(`procsource` 참조) 수집을 아낄 이유가
사라졌다.

비싼 것은 저장이다. 프로세스 330개를 1초마다 넣으면 하루 2,800만 행이다. 그래서
  - **매 틱**: 전부 수집 → 그중 활성 집합(CPU/메모리 상위 + 포어그라운드)만 tier 1 로 저장
  - **주기적**: 전체를 tier 2 로 저장 (기본 30초)
전체 그림은 30초 해상도로 남고, 문제를 일으키는 프로세스는 1초 해상도로 남는다.

프로세스 생성/종료는 스냅샷 간 pid 집합 차이로 잡는다. 폴링 근사라 1초 미만 단명
프로세스는 놓친다 — 정확한 포착은 ETW(Phase 12)의 몫이다.
"""

from __future__ import annotations

import time
from typing import Any

import psutil

from ..logging_setup import get_logger
from ..storage.queue import SampleQueue
from .base import Collector
from .procsource import ProcSample, create_source, normalize_name

log = get_logger(__name__)

try:
    import win32gui
    import win32process

    _HAVE_FOREGROUND = True
except ImportError:  # pragma: no cover
    _HAVE_FOREGROUND = False


METRIC_COLUMNS = (
    "ts",
    "pid",
    "name",
    "cpu_percent",
    "rss_mb",
    "io_read_bps",
    "io_write_bps",
    "handles",
    "threads",
    "tier",
    "foreground",
)

EVENT_COLUMNS = ("ts", "event", "pid", "ppid", "name", "exe", "username")

TIER_ACTIVE = 1
TIER_FULL = 2


def foreground_pid() -> int | None:
    """지금 사용자가 보고 있는 창의 프로세스.

    Phase 4 의 활동 레짐 추론에서 "무엇을 하는 중인가"를 가르는 가장 강한 단서다.
    실패하면 조용히 None — 잠금 화면이나 창 전환 순간에는 정상적으로 없다.
    """
    if not _HAVE_FOREGROUND:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid or None
    except Exception:
        return None


class ProcessCollector(Collector):
    """전 프로세스를 매 틱 수집하고, 저장은 선별한다."""

    name = "process"

    def __init__(
        self,
        queue: SampleQueue,
        *,
        collect_interval_s: float = 1.0,
        top_cpu: int = 15,
        top_memory: int = 10,
        full_store_interval_s: float = 30.0,
        fallback_interval_s: float = 15.0,
        prefer_pdh: bool = True,
    ) -> None:
        super().__init__(queue)
        self.interval_s = collect_interval_s
        self.top_cpu = top_cpu
        self.top_memory = top_memory
        self.full_store_interval_s = full_store_interval_s
        self._fallback_interval_s = fallback_interval_s
        self._prefer_pdh = prefer_pdh

        self._source: Any = None
        self._known: set[int] = set()
        self._last_full_store = 0.0
        self._ticks = 0
        self._stored_active = 0
        self._stored_full = 0
        self._events = 0

    # ------------------------------------------------------------------ 준비

    def setup(self) -> None:
        self._source = create_source(prefer_pdh=self._prefer_pdh)

        if self._source.kind == "psutil":
            # 폴백 경로는 프로세스당 개별 시스템 호출이라 1초 주기로는 감당이 안 된다.
            self.interval_s = max(self.interval_s, self._fallback_interval_s)
            log.warning(
                "프로세스 수집이 폴백 모드다 — 주기를 늘렸다",
                extra={"interval_s": self.interval_s},
            )

        # 첫 스냅샷은 기준점 확보용이다. 이때의 pid 들은 "이미 돌고 있던" 것이므로
        # 시작 이벤트로 기록하지 않는다(부팅 직후 300건이 쏟아지면 의미가 없다).
        snapshot = self._source.snapshot()
        self._known = set(snapshot)
        log.info(
            "프로세스 수집 시작",
            extra={
                "source": self._source.kind,
                "processes": len(self._known),
                "query_ms": round(self._source.last_ms, 1),
            },
        )

    def teardown(self) -> None:
        if self._source is not None:
            self._source.close()

    # ------------------------------------------------------------------ 내부

    def _select_active(self, snapshot: dict[int, ProcSample], fg_pid: int | None) -> set[int]:
        """저장할 활성 집합: CPU 상위 + 메모리 상위 + 포어그라운드."""
        samples = list(snapshot.values())
        by_cpu = sorted(samples, key=lambda s: s.cpu_percent or 0.0, reverse=True)[: self.top_cpu]
        by_mem = sorted(samples, key=lambda s: s.rss_mb or 0.0, reverse=True)[: self.top_memory]
        active = {s.pid for s in by_cpu} | {s.pid for s in by_mem}
        if fg_pid and fg_pid in snapshot:
            active.add(fg_pid)
        return active

    def _emit_metric(self, sample: ProcSample, now: float, tier: int, fg_pid: int | None) -> None:
        self.emit(
            "process_metrics",
            METRIC_COLUMNS,
            (
                now,
                sample.pid,
                sample.name,
                sample.cpu_percent,
                sample.rss_mb,
                sample.io_read_bps,
                sample.io_write_bps,
                sample.handles,
                sample.threads,
                tier,
                1 if sample.pid == fg_pid else 0,
            ),
        )

    def _emit_start_event(self, sample: ProcSample, now: float) -> None:
        """신규 프로세스의 부모·경로·사용자를 psutil 로 보강한다.

        PDH 는 이런 정보를 주지 않는다. 신규 프로세스는 드물어 개별 조회 비용이 문제되지
        않는다(초당 수 건). 실행 경로는 Phase 13 의 서명 검증에 필요하다.
        """
        ppid = exe = username = None
        try:
            proc = psutil.Process(sample.pid)
            with proc.oneshot():
                ppid = proc.ppid()
                try:
                    exe = proc.exe()
                except (psutil.AccessDenied, OSError):
                    pass
                try:
                    username = proc.username()
                except (psutil.AccessDenied, OSError):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # 이미 죽었거나 시스템 프로세스. 이름만이라도 남긴다.
            pass

        self.emit(
            "process_events",
            EVENT_COLUMNS,
            (now, "start", sample.pid, ppid, normalize_name(sample.name), exe, username),
        )
        self._events += 1

    # ------------------------------------------------------------------ 수집

    def collect(self) -> None:
        if self._source is None:
            return

        snapshot = self._source.snapshot()
        if not snapshot:
            return

        now = time.time()
        fg_pid = foreground_pid()
        current = set(snapshot)

        # --- 생성/종료 이벤트
        for pid in current - self._known:
            self._emit_start_event(snapshot[pid], now)
        for pid in self._known - current:
            self.emit("process_events", EVENT_COLUMNS, (now, "exit", pid, None, None, None, None))
            self._events += 1
        self._known = current

        # --- 저장 대상 선별
        store_full = (now - self._last_full_store) >= self.full_store_interval_s
        if store_full:
            for sample in snapshot.values():
                self._emit_metric(sample, now, TIER_FULL, fg_pid)
            self._last_full_store = now
            self._stored_full += len(snapshot)
        else:
            active = self._select_active(snapshot, fg_pid)
            for pid in active:
                self._emit_metric(snapshot[pid], now, TIER_ACTIVE, fg_pid)
            self._stored_active += len(active)

        self._ticks += 1

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval_s,
            "source": self._source.kind if self._source else None,
            "query_ms": round(self._source.last_ms, 1) if self._source else None,
            "processes": len(self._known),
            "ticks": self._ticks,
            "stored_active": self._stored_active,
            "stored_full": self._stored_full,
            "events": self._events,
        }


if __name__ == "__main__":  # 스모크: python -m argus.collector.process
    from ..logging_setup import setup
    from ..storage.hot import Database

    setup(level="INFO")
    queue = SampleQueue(maxsize=50000)
    collector = ProcessCollector(
        queue, collect_interval_s=1.0, full_store_interval_s=4.0
    )
    collector.setup()
    try:
        for _ in range(8):
            time.sleep(1.0)
            collector.tick()
        status = collector.describe()
    finally:
        collector.teardown()

    samples = queue.drain(100000)
    metrics = [s for s in samples if s.table == "process_metrics"]
    events = [s for s in samples if s.table == "process_events"]
    tier1 = [s for s in metrics if s.values[9] == TIER_ACTIVE]
    tier2 = [s for s in metrics if s.values[9] == TIER_FULL]

    print(f"  소스: {status['source']}  질의 {status['query_ms']}ms  프로세스 {status['processes']}개")
    print(f"  8틱 저장: tier1 {len(tier1)}행 · tier2 {len(tier2)}행 · 이벤트 {len(events)}건")
    print(f"  활성 집합 크기: 약 {len(tier1) // max(1, status['ticks'] - 2)}개/틱")

    latest_ts = max(s.values[0] for s in tier1) if tier1 else 0
    top = sorted(
        (s for s in tier1 if s.values[0] == latest_ts), key=lambda s: s.values[3] or 0, reverse=True
    )[:6]
    print("  마지막 틱 CPU 상위:")
    for s in top:
        v = s.values
        fg = "  [포어그라운드]" if v[10] else ""
        io_w = f"{v[6]/1024:.0f}KB/s" if v[6] else "-"
        print(f"    {v[2]:24} cpu={v[3]:6.2f}%  rss={v[4]:8.1f}MB  쓰기={io_w:>9}{fg}")

    if events:
        print(f"  이벤트 예시 ({len(events)}건 중 5건):")
        for s in events[:5]:
            v = s.values
            print(f"    {v[1]:5} pid={v[2]:<7} {v[4] or '(알 수 없음)'}")

    # 하루 행 수 추정 — 보존 정책이 감당 가능한지 여기서 판단한다
    per_tick_active = len(tier1) / max(1, status["ticks"] - 2)
    daily = per_tick_active * 86400 + status["processes"] * (86400 / 30)
    print(f"  하루 행 수 추정: 약 {daily/1e6:.1f}M행 (활성 1초 + 전체 30초 기준)")

    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"]
        db.insert_many("process_metrics", METRIC_COLUMNS, [s.values for s in metrics])
        if events:
            db.insert_many("process_events", EVENT_COLUMNS, [s.values for s in events])
        after = db.query("SELECT COUNT(*) AS c FROM process_metrics")[0]["c"]
        print(f"  DB 기록: {before} -> {after}")

    problems = []
    if not tier1:
        problems.append("활성 집합이 저장되지 않았다")
    if not tier2:
        problems.append("전체 저장이 일어나지 않았다")
    if any(s.values[2] == "idle" or s.values[1] == 0 for s in metrics):
        problems.append("Idle 가짜 프로세스가 걸러지지 않았다")
    if status["query_ms"] and status["query_ms"] > 100:
        problems.append(f"질의가 너무 느리다 ({status['query_ms']}ms)")
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] collector.process")

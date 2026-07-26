"""프로세스 스냅샷 소스 — 전 프로세스 상태를 한 번에 가져오는 두 가지 방법.

**왜 두 벌인가**: psutil 로 프로세스 하나의 CPU·메모리·핸들·IO 를 읽는 데 약 4ms 가
든다. 300개면 1.4초다. 10초마다 돌려도 한 스레드의 14% 를 먹어, 관측자가 관측 대상을
오염시킨다.

Windows 성능 카운터의 `Process V2` 객체는 **와일드카드 질의 한 번으로 전 프로세스를
약 11ms 에** 가져온다. 122배 차이다. 그래서 이쪽을 기본으로 쓰고, 쓸 수 없는 환경에서만
psutil 로 내려간다.

세 가지 함정이 있었고 모두 실측으로 확인했다.

1. **`Process` (V1) 객체는 쓸 수 없다.** 인스턴스 이름이 프로세스명뿐이라 같은 이름이
   여러 개면 합쳐진다. 실측 결과 336개 중 200개를 놓쳤고, 하필 chrome·Discord·Steam
   처럼 우리가 가장 보고 싶은 것들이었다. `Process V2` 는 이름이 `name:pid` 라 충돌이 없다.
2. **백분율이 100 에서 잘린다.** PDH 는 기본적으로 `% Processor Time` 을 100 으로
   캡한다. `NOCAP100` 없이 읽으면 1코어 넘게 쓰는 프로세스가 전부 100 으로 보고되어
   기여도 계산이 조용히 망가진다(실측: Idle 이 capped 100 / nocap 976).
3. **카운터 이름이 V1 과 다르다.** V1 의 `ID Process` 가 V2 에서는 `Process ID` 다.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any

import psutil

from ..logging_setup import get_logger
from .pdh import english_counter_indices

log = get_logger(__name__)

try:
    import win32pdh

    _HAVE_PDH = sys.platform == "win32"
except ImportError:  # pragma: no cover
    win32pdh = None  # type: ignore[assignment]
    _HAVE_PDH = False


@dataclass(slots=True)
class ProcSample:
    """한 프로세스의 한 시점 상태.

    두 소스가 같은 의미의 값을 내도록 맞춰 둔 것이 중요하다. 소스가 바뀌었다고 값이
    달라지면 Phase 6 의 프로세스 지문이 통째로 어긋난다.

    - `name`  : 확장자 없는 소문자 (`chrome`). PDH 인스턴스명 규칙에 psutil 을 맞췄다.
    - `rss_mb`: 공유 페이지를 제외한 전용 메모리. `memory_info().rss`(공유 포함)를 쓰면
      공유 라이브러리가 프로세스마다 중복 계상되어 기여도가 부풀려진다
      (실측: dwm 이 618MB vs 70MB).

    **주의 — 두 소스의 메모리 지표는 완전히 같지 않다.** PDH 는 `Working Set - Private`
    (실제 상주 중인 전용 페이지), psutil 폴백은 `Private Bytes`(커밋된 전용 메모리)로
    후자가 다소 크게 나온다(실측: 152MB vs 236MB). Windows 가 정확히 대응하는 값을
    psutil 로 싸게 주지 않기 때문이다(`memory_full_info().uss` 는 근접하지만 매우 느리다).

    한 머신에서 소스는 바뀌지 않으므로 베이스라인·지문의 일관성은 유지된다. 다만
    **서로 다른 머신의 절대값을 직접 비교하면 안 된다** — 어차피 하드웨어 무가정 원칙상
    비교는 각 머신의 기준선 대비 상대값으로만 한다.
    """

    pid: int
    name: str
    cpu_percent: float | None  # 머신 전체 대비 % (논리 코어 수로 정규화)
    rss_mb: float | None
    io_read_bps: float | None
    io_write_bps: float | None
    handles: int | None
    threads: int | None


def normalize_name(raw: str) -> str:
    """프로세스명 표기 통일 — 확장자 제거 + 소문자."""
    name = raw
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


# PDH Process V2 에서 읽을 카운터. (키, 카운터명, 정수형 여부)
_V2_COUNTERS: list[tuple[str, str, bool]] = [
    ("pid", "Process ID", True),
    ("cpu", "% Processor Time", False),
    ("rss", "Working Set - Private", True),
    ("handles", "Handle Count", True),
    ("threads", "Thread Count", True),
    ("io_read", "IO Read Bytes/sec", False),
    ("io_write", "IO Write Bytes/sec", False),
]

# 집계 인스턴스와 가짜 프로세스. Idle 은 "유휴 시간"을 CPU 사용률로 보고하므로
# 그냥 두면 항상 CPU 1위가 되어 모든 순위를 무의미하게 만든다.
_EXCLUDED_INSTANCES = {"_Total", "Idle", "Idle:0"}

_MB = 1024 * 1024


class PdhProcessSource:
    """`Process V2` 와일드카드 질의. 전 프로세스를 한 번에."""

    kind = "pdh"

    def __init__(self) -> None:
        self._query: Any = None
        self._handles: dict[str, Any] = {}
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._reason = ""
        self._last_ms = 0.0

    @property
    def available(self) -> bool:
        return bool(self._handles)

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def last_ms(self) -> float:
        return self._last_ms

    def open(self) -> "PdhProcessSource":
        if not _HAVE_PDH:
            self._reason = "pywin32 없음 또는 Windows 아님"
            return self

        index = english_counter_indices().get("Process V2")
        if index is None:
            # Windows 10 이하. V1 은 인스턴스 이름이 충돌해 쓰지 않는다.
            self._reason = "Process V2 객체 없음 (Windows 11 미만으로 추정)"
            return self

        try:
            obj = win32pdh.LookupPerfNameByIndex(None, index)
            self._query = win32pdh.OpenQuery()
        except Exception as e:
            self._reason = f"질의 생성 실패: {e}"
            return self

        for key, counter, _is_int in _V2_COUNTERS:
            try:
                path = win32pdh.MakeCounterPath((None, obj, "*", None, -1, counter))
                self._handles[key] = win32pdh.AddCounter(self._query, path)
            except Exception as e:
                # pid 나 cpu 가 없으면 이 소스는 쓸 수 없다. 나머지는 없어도 된다.
                if key in ("pid", "cpu"):
                    self._reason = f"필수 카운터 '{counter}' 없음: {e}"
                    self.close()
                    return self
                log.info("프로세스 카운터 비활성", extra={"counter": counter, "error": str(e)})

        try:
            # 속도·백분율 카운터는 두 번째 표본부터 값이 나온다.
            win32pdh.CollectQueryData(self._query)
        except Exception as e:
            self._reason = f"초기 표본 실패: {e}"
            self.close()
        return self

    def close(self) -> None:
        if self._query is not None:
            try:
                win32pdh.CloseQuery(self._query)
            except Exception:
                pass
            self._query = None
        self._handles.clear()

    def _array(self, key: str, *, integer: bool) -> dict[str, float]:
        handle = self._handles.get(key)
        if handle is None:
            return {}
        if integer:
            fmt = win32pdh.PDH_FMT_LARGE
        else:
            # NOCAP100 이 없으면 1코어를 넘는 사용률이 전부 100 으로 잘린다.
            fmt = win32pdh.PDH_FMT_DOUBLE | win32pdh.PDH_FMT_NOCAP100
        try:
            return win32pdh.GetFormattedCounterArray(handle, fmt)
        except Exception:
            # 인스턴스가 통째로 바뀌는 순간 등. 이번 틱만 건너뛴다.
            return {}

    def snapshot(self) -> dict[int, ProcSample]:
        if not self.available or self._query is None:
            return {}

        started = time.perf_counter()
        try:
            win32pdh.CollectQueryData(self._query)
        except Exception as e:
            log.debug("프로세스 표본 수집 실패", extra={"error": str(e)})
            return {}

        arrays = {key: self._array(key, integer=is_int) for key, _c, is_int in _V2_COUNTERS}
        pids = arrays.get("pid", {})

        out: dict[int, ProcSample] = {}
        for instance, raw_pid in pids.items():
            if instance in _EXCLUDED_INSTANCES:
                continue
            pid = int(raw_pid)
            if pid <= 0:  # Idle(0) 및 비정상 항목
                continue

            # 인스턴스 이름은 "name:pid". 프로세스명에 ':' 가 들어갈 수 있으니 뒤에서 자른다.
            name = normalize_name(instance.rsplit(":", 1)[0] if ":" in instance else instance)

            cpu = arrays.get("cpu", {}).get(instance)
            rss = arrays.get("rss", {}).get(instance)
            handles = arrays.get("handles", {}).get(instance)
            threads = arrays.get("threads", {}).get(instance)
            io_read = arrays.get("io_read", {}).get(instance)
            io_write = arrays.get("io_write", {}).get(instance)

            out[pid] = ProcSample(
                pid=pid,
                name=name,
                # psutil 과 마찬가지로 코어 1개가 100 이므로 머신 전체 대비로 정규화한다.
                cpu_percent=round(cpu / self._cpu_count, 3) if cpu is not None else None,
                rss_mb=round(rss / _MB, 2) if rss is not None else None,
                io_read_bps=round(io_read, 1) if io_read is not None else None,
                io_write_bps=round(io_write, 1) if io_write is not None else None,
                handles=int(handles) if handles is not None else None,
                threads=int(threads) if threads is not None else None,
            )

        self._last_ms = (time.perf_counter() - started) * 1000
        return out


class PsutilProcessSource:
    """psutil 폴백. 정확하지만 프로세스당 개별 시스템 호출이라 느리다.

    PDH 를 쓸 수 없는 환경(구형 Windows, 성능 카운터 손상)에서만 쓴다.
    호출자가 주기를 크게 잡아야 한다 — 300개 스캔에 1초 이상 걸린다.
    """

    kind = "psutil"

    def __init__(self) -> None:
        self._procs: dict[int, psutil.Process] = {}
        self._cpu_count = psutil.cpu_count(logical=True) or 1
        self._prev_io: dict[int, tuple[float, int, int]] = {}
        self._last_ms = 0.0

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str:
        return ""

    @property
    def last_ms(self) -> float:
        return self._last_ms

    def open(self) -> "PsutilProcessSource":
        self.snapshot()  # cpu_percent 기준점 만들기 (첫 값은 항상 0.0 이라 버린다)
        return self

    def close(self) -> None:
        self._procs.clear()
        self._prev_io.clear()

    def snapshot(self) -> dict[int, ProcSample]:
        started = time.perf_counter()
        now = time.time()
        out: dict[int, ProcSample] = {}
        alive: set[int] = set()

        for proc in psutil.process_iter(["pid"]):
            pid = proc.info["pid"]
            if pid <= 0:
                continue
            alive.add(pid)
            tracked = self._procs.get(pid)
            if tracked is None:
                tracked = proc
                self._procs[pid] = proc

            try:
                with tracked.oneshot():
                    name = normalize_name(tracked.name())
                    cpu = tracked.cpu_percent(interval=None) / self._cpu_count
                    memory = tracked.memory_info()
                    # PDH 와 같은 의미가 되도록 전용 작업 집합을 쓴다. Windows 에서는
                    # private 필드가 있고, 없는 플랫폼에서만 rss 로 떨어진다.
                    private = getattr(memory, "private", None)
                    rss_mb = (private if private is not None else memory.rss) / _MB
                    threads = tracked.num_threads()
                    try:
                        handles = tracked.num_handles() if hasattr(tracked, "num_handles") else None
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        handles = None
                    try:
                        io = tracked.io_counters()
                        read_bps, write_bps = self._io_rate(pid, now, io.read_bytes, io.write_bytes)
                    except (psutil.AccessDenied, psutil.NoSuchProcess, AttributeError):
                        read_bps = write_bps = None
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # 조회 도중 종료됐거나 시스템 프로세스. 둘 다 정상 상황이다.
                continue
            except OSError:
                continue

            out[pid] = ProcSample(
                pid, name, round(cpu, 3), round(rss_mb, 2), read_bps, write_bps, handles, threads
            )

        # 죽은 프로세스 정리 — 안 하면 상주 중에 계속 쌓인다.
        for pid in list(self._procs):
            if pid not in alive:
                self._procs.pop(pid, None)
                self._prev_io.pop(pid, None)

        self._last_ms = (time.perf_counter() - started) * 1000
        return out

    def _io_rate(
        self, pid: int, now: float, read_bytes: int, write_bytes: int
    ) -> tuple[float | None, float | None]:
        prev = self._prev_io.get(pid)
        self._prev_io[pid] = (now, read_bytes, write_bytes)
        if prev is None:
            return None, None
        prev_ts, prev_read, prev_write = prev
        dt = now - prev_ts
        if dt <= 0:
            return None, None
        read_delta = read_bytes - prev_read
        write_delta = write_bytes - prev_write
        if read_delta < 0 or write_delta < 0:
            return None, None
        return round(read_delta / dt, 1), round(write_delta / dt, 1)


def create_source(*, prefer_pdh: bool = True) -> PdhProcessSource | PsutilProcessSource:
    """쓸 수 있는 가장 싼 소스를 고른다."""
    if prefer_pdh:
        pdh = PdhProcessSource().open()
        if pdh.available:
            return pdh
        log.warning(
            "PDH 프로세스 소스를 쓸 수 없다 — psutil 로 내려간다 (느림)",
            extra={"reason": pdh.reason},
        )
    return PsutilProcessSource().open()


if __name__ == "__main__":  # 스모크: python -m argus.collector.procsource
    from ..logging_setup import setup

    setup(level="INFO")

    for prefer in (True, False):
        source = create_source(prefer_pdh=prefer)
        try:
            time.sleep(1.0)
            snap = source.snapshot()
            label = f"{source.kind:6}"
            print(f"  [{label}] 프로세스 {len(snap)}개  질의 {source.last_ms:.1f}ms")
            top = sorted(snap.values(), key=lambda s: s.cpu_percent or 0, reverse=True)[:4]
            for s in top:
                print(
                    f"      {s.name:24} pid={s.pid:<7} cpu={s.cpu_percent:6.2f}%  "
                    f"rss={s.rss_mb:8.1f}MB  hnd={s.handles}  thr={s.threads}"
                )
            if not snap:
                print("[FAIL] 스냅샷이 비어 있다")
                raise SystemExit(1)
        finally:
            source.close()

    # 커버리지 비교 — V1 객체를 썼다면 여기서 대량 누락이 드러난다.
    pdh = PdhProcessSource().open()
    if pdh.available:
        time.sleep(1.0)
        pdh_pids = set(pdh.snapshot())
        pdh.close()
        ps_pids = {p for p in psutil.pids() if p > 0}
        missing = ps_pids - pdh_pids
        ratio = len(pdh_pids) / max(1, len(ps_pids))
        print(f"  커버리지: PDH {len(pdh_pids)} / psutil {len(ps_pids)}  (누락 {len(missing)})")
        if ratio < 0.9:
            print(f"[FAIL] PDH 가 프로세스를 대량 누락한다 (커버리지 {ratio:.0%})")
            raise SystemExit(1)
    print("[OK] collector.procsource")

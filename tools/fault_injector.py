"""결함 주입기 — 이상을 인위로 만들어 정답 라벨을 생성한다.

**왜 필요한가**: 이상 탐지에는 정답이 없다. "어제 14시가 진짜 이상이었는지" 말해 줄
사람이 없으니 탐지기를 만들어도 잘 맞는지 잴 수 없고, 결국 임계값을 감으로 만지게 된다.

결함을 우리가 만들면 **언제부터 언제까지가 이상인지 아는 구간**이 생긴다. 그 구간이
`fault_injections` 에 기록되고, 스코어보드의 유일한 채점 기준이 된다.

**가장 중요한 시나리오는 `--ramp` 다.** 갑자기 CPU 가 100% 가 되는 것은 어떤 탐지기든
잡는다. 어려운 것은 30분에 걸쳐 서서히 나빠지는 것이다 — 메모리 누수, 팬 열화로 인한
스로틀링, 디스크 노후화가 전부 이 모양이고, 사람이 "요즘 좀 느려진 것 같은데"라고
느끼는 것도 이쪽이다. Phase 7 시퀀스 모델의 존재 이유가 바로 이 시나리오다.

---

**안전이 최우선이다.** 이 도구는 개발자 본인의 실제 PC 에서 돈다. 다음을 반드시 지킨다.

- 모든 자원에 **하드 상한**을 두고, 설정값이 그보다 커도 상한이 이긴다
- 메모리는 **가용 메모리의 일정 비율**을 넘지 않는다 (다른 프로그램이 죽으면 안 된다)
- CPU 는 **논리 코어 수 - 1** 을 넘지 않는다 (PC 가 먹통이 되면 안 된다)
- 디스크는 스크래치 폴더에만 쓰고 **종료 시 반드시 지운다**
- Ctrl+C·예외·정상 종료 어느 경로로든 정리가 돌아간다

    python tools/fault_injector.py --list
    python tools/fault_injector.py memory_leak --duration 120
    python tools/fault_injector.py cpu_spin --duration 300 --ramp
    python tools/fault_injector.py --dry-run memory_leak --duration 60
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil  # noqa: E402

from argus.logging_setup import get_logger, setup  # noqa: E402
from argus.machine.calibration import load_profile  # noqa: E402
from argus.paths import cache_dir  # noqa: E402
from argus.storage.hot import Database  # noqa: E402

log = get_logger("fault_injector")

# ------------------------------------------------------------------ 안전 상한

# 가용 메모리의 이 비율을 넘게 잡지 않는다. 넘기면 다른 프로그램이 스왑되거나 죽는다.
MAX_MEMORY_FRACTION = 0.25
# 폭주 방지용 최후 상한. 이 값을 낮게 잡으면(예전 4096) 64GB PC 에서 상대 강도가
# 통째로 무력화된다 — 전체의 20% 를 요청해도 6% 만 잡혀 `mem_percent` 가 안 움직인다.
# 실질 보호는 위의 가용 메모리 비율이 하고, 이건 자릿수 실수만 막는다.
MAX_MEMORY_MB_ABSOLUTE = 24576
# 논리 코어를 전부 태우면 PC 조작이 불가능해진다. 최소 1개는 남긴다.
MAX_CPU_THREADS = max(1, (psutil.cpu_count(logical=True) or 2) - 1)
MAX_DISK_MB = 8192
MAX_HANDLES = 5000
# 디스크 큐를 올리려면 미결 I/O 가 여러 개여야 한다. 단일 스레드 동기 쓰기는 큐 깊이가
# 항상 1 이라 어떤 장비에서도 `disk_queue` 를 못 올린다 — 2026-07-27 에 이것 때문에
# 271MB/s 를 퍼붓고도 큐 최대치가 1.0 이었다.
MAX_DISK_WORKERS = 8
# 실수로 며칠짜리를 걸어 두는 것을 막는다.
MAX_DURATION_S = 6 * 3600


# ------------------------------------------------------------------ 머신 스케일


@dataclass
class MachineScale:
    """이 PC 의 능력치. 상대 강도를 절대값으로 바꾸는 환산표.

    **왜 절대값을 쓰면 안 되는가** — 2026-07-27 에 실측으로 확인했다.
    `cpu_spin --cpu-threads 3` 은 12코어 PC 에서 `cpu_total` 을 26.8% 로 올렸는데,
    같은 PC 의 정상 구간 평균이 20.7%(최대 46.3%)였다. **주입 구간이 평소보다 조용했다.**
    `disk_thrash` 는 271MB/s 를 퍼부었지만 NVMe 가 그대로 삼켜 `disk_resp_ms` 최대가
    0.3ms 였다. 둘 다 "정답 라벨은 있는데 대응하는 열화가 없는" 구간을 만들었고,
    그런 라벨 위에서는 어떤 탐지기도 정답일 수 없다.

    4코어 노트북과 12코어 데스크톱에서 같은 강도의 이상을 만들려면 절대량이 아니라
    **그 PC 능력 대비 비율**로 말해야 한다. CLAUDE.md 설계 규칙 2 를 주입기에도 적용한 것이다.
    """

    logical_cores: int
    total_memory_mb: float
    seq_write_mbps: float
    media_type: str
    source: str  # 'machine_profile' | 'psutil' — 어디서 온 값인지 드러낸다

    @classmethod
    def detect(cls) -> "MachineScale":
        profile = load_profile()
        if profile is not None:
            data = profile.to_dict()
            cpu = data.get("cpu") or {}
            memory = data.get("memory") or {}
            disk = data.get("disk") or {}
            cores = cpu.get("logical") or psutil.cpu_count(logical=True) or 2
            total_gb = memory.get("total_gb")
            seq = disk.get("seq_write_mbps")
            if total_gb and seq:
                return cls(
                    logical_cores=int(cores),
                    total_memory_mb=float(total_gb) * 1024,
                    seq_write_mbps=float(seq),
                    media_type=str(disk.get("media_type") or "unknown"),
                    source="machine_profile",
                )

        # 프로필이 없으면(첫 실행 전) psutil 로 때운다. 디스크 속도는 알 수 없으므로
        # 보수적으로 잡는다 — 과소평가는 주입이 약해질 뿐이지만 과대평가는 PC 를 멈춘다.
        return cls(
            logical_cores=psutil.cpu_count(logical=True) or 2,
            total_memory_mb=psutil.virtual_memory().total / (1024 * 1024),
            seq_write_mbps=150.0,
            media_type="unknown",
            source="psutil",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "logical_cores": self.logical_cores,
            "total_memory_mb": round(self.total_memory_mb),
            "seq_write_mbps": round(self.seq_write_mbps, 1),
            "media_type": self.media_type,
            "source": self.source,
        }


@dataclass
class Limits:
    """실제로 적용된 상한. 요청값과 다르면 이유를 남긴다."""

    memory_mb: int
    cpu_threads: int
    disk_mb: int
    disk_rate_mbps: float
    disk_workers: int
    handles: int
    duration_s: float
    scale: MachineScale
    loads: dict[str, float] = field(default_factory=dict)
    clamped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_mb": self.memory_mb,
            "cpu_threads": self.cpu_threads,
            "disk_mb": self.disk_mb,
            "disk_rate_mbps": round(self.disk_rate_mbps, 1),
            "disk_workers": self.disk_workers,
            "handles": self.handles,
            "duration_s": self.duration_s,
            "loads": self.loads,
            "machine": self.scale.as_dict(),
            "clamped": self.clamped,
        }


def resolve_limits(args: argparse.Namespace) -> Limits:
    """상대 강도(0~1) → 이 PC 의 절대값. 안전 상한은 그 뒤에 걸린다."""
    clamped: list[str] = []
    scale = MachineScale.detect()

    # --- CPU: 논리 코어의 비율
    cpu_threads = max(1, round(scale.logical_cores * args.cpu_load))
    if cpu_threads > MAX_CPU_THREADS:
        clamped.append(f"cpu_threads {cpu_threads} -> {MAX_CPU_THREADS} (조작 가능성 유지)")
        cpu_threads = MAX_CPU_THREADS

    # --- 메모리: 전체 RAM 의 비율. 단, 가용 메모리 보호가 항상 이긴다.
    memory_mb = int(scale.total_memory_mb * args.mem_load)
    available_mb = psutil.virtual_memory().available / (1024 * 1024)
    memory_cap = int(min(available_mb * MAX_MEMORY_FRACTION, MAX_MEMORY_MB_ABSOLUTE))
    if memory_mb > memory_cap:
        clamped.append(f"memory_mb {memory_mb} -> {memory_cap} (가용 메모리 보호)")
        memory_mb = memory_cap

    # --- 디스크: 순차 쓰기 실측치의 배수를 목표 속도로. 장치가 못 따라오게 만드는 게 목적이다.
    disk_rate = scale.seq_write_mbps * args.disk_load
    disk_mb = int(min(disk_rate * args.duration, MAX_DISK_MB))
    if disk_mb >= MAX_DISK_MB:
        clamped.append(f"disk_mb -> {MAX_DISK_MB} (누적 쓰기 상한)")

    return Limits(
        memory_mb=memory_mb,
        cpu_threads=cpu_threads,
        disk_mb=disk_mb,
        disk_rate_mbps=disk_rate,
        disk_workers=min(MAX_DISK_WORKERS, max(2, scale.logical_cores // 2)),
        handles=min(args.handles, MAX_HANDLES),
        duration_s=min(args.duration, MAX_DURATION_S),
        scale=scale,
        loads={"cpu": args.cpu_load, "mem": args.mem_load, "disk": args.disk_load},
        clamped=clamped,
    )


# ------------------------------------------------------------------ 시나리오


class Scenario:
    """결함 하나.

    `step(intensity)` 가 주기적으로 불린다. `intensity` 는 0~1 이고, `--ramp` 면
    0 에서 1 까지 서서히 오른다. 이 한 가지 규약으로 급격한 결함과 점진적 열화를
    같은 코드로 표현한다.
    """

    name = "scenario"
    description = ""

    def __init__(self, limits: Limits) -> None:
        self.limits = limits

    def setup(self) -> None: ...

    def step(self, intensity: float, dt: float) -> None:
        raise NotImplementedError

    def cleanup(self) -> None: ...

    def status(self) -> str:
        return ""

    def params(self) -> dict[str, Any]:
        return {}

    def expected_effect(self) -> tuple[str, float]:
        """(관측할 지표, 최소 변화량). 이만큼도 안 움직이면 라벨로 못 쓴다.

        기준값은 "탐지기가 볼 수 있는 최소한"이다. 정상 구간의 자연스러운 변동보다
        확실히 커야 하고, 그렇지 않으면 정답 구간과 정상 구간이 구분되지 않는다.
        """
        raise NotImplementedError


class MemoryLeak(Scenario):
    """서서히 메모리를 붙잡고 놓지 않는다.

    바이트 배열을 채워서(touch) 실제 상주 메모리(RSS)가 늘게 한다. 단순히 할당만
    하면 Windows 가 페이지를 배정하지 않아 관측되지 않는다.
    """

    name = "memory_leak"
    description = "메모리를 초당 N MB 씩 붙잡고 놓지 않음 (누수 흉내)"

    def __init__(self, limits: Limits, rate_mb_s: float | None = None) -> None:
        super().__init__(limits)
        # 고정 속도(예전의 8MB/s)를 쓰면 64GB PC 에서는 2분 주입에 1GB 도 못 채워
        # `mem_percent` 가 1.5%p 밖에 안 움직인다. 지속 시간의 80% 지점에서 목표에
        # 닿도록 속도를 역산해야 어느 PC 에서든 같은 세기의 이상이 된다.
        # (`--ramp` 는 평균 강도가 0.5 이므로 2배로 잡는다.)
        self.rate_mb_s = rate_mb_s if rate_mb_s is not None else (
            limits.memory_mb / max(1.0, limits.duration_s * 0.8) * 2.0
        )
        self._blocks: list[bytearray] = []
        self._held_mb = 0.0
        # 매 틱 요청량이 1MB 미만이면 버리는 방식은 실제 누수율을 크게 떨어뜨린다
        # (틱이 0.2초면 8MB/s 요청이 틱당 1.6MB, ramp 초반에는 0.5MB 라 전부 버려진다).
        # 못 채운 소수점을 다음 틱으로 넘겨 누적 속도를 정확히 맞춘다.
        self._debt_mb = 0.0

    def step(self, intensity: float, dt: float) -> None:
        if self._held_mb >= self.limits.memory_mb:
            return
        self._debt_mb += self.rate_mb_s * intensity * dt
        chunk = int(min(self._debt_mb, self.limits.memory_mb - self._held_mb))
        if chunk <= 0:
            return
        self._debt_mb -= chunk
        block = bytearray(chunk * 1024 * 1024)
        # 페이지를 실제로 건드려야 RSS 가 오른다. 4KB 간격으로 찍는다.
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        self._blocks.append(block)
        self._held_mb += chunk

    def cleanup(self) -> None:
        self._blocks.clear()
        self._held_mb = 0.0
        self._debt_mb = 0.0

    def status(self) -> str:
        return f"보유 {self._held_mb:.0f}MB / 상한 {self.limits.memory_mb}MB"

    def params(self) -> dict[str, Any]:
        return {"rate_mb_s": round(self.rate_mb_s, 2), "cap_mb": self.limits.memory_mb}

    def expected_effect(self) -> tuple[str, float]:
        return "mem_percent", 3.0


_BURN_SLICE_S = 0.05


def _burn_worker(duty: Any, stop: Any) -> None:
    """워커 프로세스 하나. 듀티 사이클만큼 CPU 를 태운다.

    **스레드가 아니라 프로세스인 이유는 GIL 이다.** 파이썬 스레드로 CPU 바운드
    루프를 9개 돌려도 한 번에 하나만 바이트코드를 실행한다. 2026-07-27 실측에서
    12코어 PC 에 9스레드를 태우고도 `cpu_total` 이 20.9% → 41.2% 밖에 안 올랐다
    (9/12 = 75% 를 기대했다). 프로세스는 각자 GIL 을 가지므로 실제로 병렬이다.

    모듈 최상위 함수여야 한다 — Windows 의 spawn 방식은 워커를 만들 때 이 모듈을
    다시 import 하고 함수를 이름으로 찾는다. 메서드나 클로저는 그 과정에서 깨진다.
    """
    x = 0
    while not stop.value:
        d = duty.value
        if d <= 0.01:
            time.sleep(_BURN_SLICE_S)
            continue
        work_end = time.perf_counter() + _BURN_SLICE_S * d
        while time.perf_counter() < work_end:
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        idle = _BURN_SLICE_S * (1.0 - d)
        if idle > 0:
            time.sleep(idle)


class CpuSpin(Scenario):
    """CPU 를 태운다. intensity 가 듀티 사이클을 정한다."""

    name = "cpu_spin"
    description = "N 개 프로세스로 CPU 점유 (듀티 사이클로 강도 조절)"

    def __init__(self, limits: Limits) -> None:
        super().__init__(limits)
        self._procs: list[multiprocessing.Process] = []
        self._duty: Any = None
        self._stop: Any = None

    def setup(self) -> None:
        self._duty = multiprocessing.Value("d", 0.0)
        self._stop = multiprocessing.Value("b", 0)
        for i in range(self.limits.cpu_threads):
            p = multiprocessing.Process(
                target=_burn_worker, args=(self._duty, self._stop), name=f"burn-{i}", daemon=True
            )
            p.start()
            self._procs.append(p)

    def step(self, intensity: float, dt: float) -> None:
        if self._duty is not None:
            self._duty.value = intensity

    def cleanup(self) -> None:
        if self._stop is not None:
            self._stop.value = 1
        for p in self._procs:
            p.join(timeout=3.0)
            if p.is_alive():
                # 상한을 넘겨 도는 부하 프로세스를 남기면 안 된다. 실제 PC 다.
                p.terminate()
                p.join(timeout=2.0)
        self._procs.clear()

    def status(self) -> str:
        duty = self._duty.value if self._duty is not None else 0.0
        alive = sum(1 for p in self._procs if p.is_alive())
        return f"{alive}/{len(self._procs)}프로세스 듀티 {duty*100:.0f}%"

    def params(self) -> dict[str, Any]:
        return {"threads": self.limits.cpu_threads, "cores": self.limits.scale.logical_cores}

    def expected_effect(self) -> tuple[str, float]:
        return "cpu_percent", 25.0


class DiskThrash(Scenario):
    """랜덤 쓰기로 디스크 큐와 응답시간을 밀어 올린다.

    응답시간(`disk_resp_ms`)을 움직이는 것이 목적이므로 순차 대량 쓰기가 아니라
    작은 랜덤 쓰기 + 동기화를 반복한다.
    """

    name = "disk_thrash"
    description = "랜덤 4K 쓰기 + fsync 반복 (디스크 응답시간 상승)"

    def __init__(self, limits: Limits) -> None:
        super().__init__(limits)
        self.rate_mb_s = limits.disk_rate_mbps
        self.workers = limits.disk_workers
        self._dir = cache_dir()
        self._paths: list[Path] = []
        self._files: list[Any] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._size = 0
        self._written_bytes = 0
        self._lock = threading.Lock()
        self._intensity = 0.0
        self._block = os.urandom(4096)

    def setup(self) -> None:
        # 워커마다 자기 파일을 쓴다. 한 파일을 공유하면 파일 단위 락에 직렬화되어
        # 스레드를 늘려도 미결 I/O 가 늘지 않는다 — 큐 깊이를 만들려는 목적이 무너진다.
        self._size = max(64, min(256, self.limits.disk_mb // max(1, self.workers))) * 1024 * 1024
        for i in range(self.workers):
            path = self._dir / f"_fault_disk_{os.getpid()}_{i}.tmp"
            with open(path, "wb") as f:
                f.truncate(self._size)
            self._paths.append(path)
            self._files.append(open(path, "r+b"))

        for i in range(self.workers):
            t = threading.Thread(target=self._hammer, args=(i,), name=f"disk-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _hammer(self, index: int) -> None:
        """워커 하나. 랜덤 4K 쓰기 + fsync 를 반복한다.

        fsync 가 핵심이다. 없으면 전부 페이지 캐시에 들어가 장치까지 내려가지 않고,
        `disk_resp_ms`(PDH Avg. Disk sec/Transfer)는 꿈쩍도 하지 않는다.
        """
        handle = self._files[index]
        rng = random.Random(index)          # 워커별 고정 시드 — 재현 가능하게
        budget_per_worker = self.limits.disk_mb * 1024 * 1024 / max(1, self.workers)
        written = 0

        while not self._stop.is_set() and written < budget_per_worker:
            with self._lock:
                duty = self._intensity
            if duty <= 0.01:
                self._stop.wait(0.05)
                continue

            burst = max(1, int(16 * duty))   # 강도가 낮으면 적게 쓴다
            try:
                for _ in range(burst):
                    handle.seek(rng.randrange(0, max(1, self._size - 4096)))
                    handle.write(self._block)
                    written += 4096
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as exc:
                log.warning("디스크 워커 쓰기 실패", extra={"worker": index, "error": str(exc)})
                return

            with self._lock:
                self._written_bytes += burst * 4096

            # 목표 속도를 넘지 않도록 쉰다. 넘겨 봐야 PC 만 멈춘다.
            per_worker_bps = self.rate_mb_s * 1024 * 1024 * duty / max(1, self.workers)
            if per_worker_bps > 0:
                self._stop.wait(max(0.0, (burst * 4096) / per_worker_bps * 0.5))

    def step(self, intensity: float, dt: float) -> None:
        with self._lock:
            self._intensity = intensity

    def cleanup(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3.0)
        for handle in self._files:
            try:
                handle.close()
            except OSError:
                pass
        self._files.clear()
        for path in self._paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("임시 파일 삭제 실패", extra={"path": str(path)})
        self._paths.clear()

    def status(self) -> str:
        with self._lock:
            written_mb = self._written_bytes / (1024 * 1024)
        return f"{self.workers}워커 누적 {written_mb:.0f}MB / 상한 {self.limits.disk_mb}MB"

    def params(self) -> dict[str, Any]:
        return {
            "rate_mb_s": round(self.rate_mb_s, 1),
            "workers": self.workers,
            "cap_mb": self.limits.disk_mb,
        }

    def expected_effect(self) -> tuple[str, float]:
        # 이 PC 순차 쓰기 실측치의 20% 만큼은 실제로 장치까지 내려가야 한다.
        return "disk_write_mbps", max(20.0, self.limits.scale.seq_write_mbps * 0.2)


class HandleLeak(Scenario):
    """파일 핸들을 열고 닫지 않는다.

    Windows 에서 핸들 누수는 메모리보다 먼저 드러나는 신호다. 자기 계측에 핸들 수를
    넣어 둔 이유이기도 하다.
    """

    name = "handle_leak"
    description = "파일 핸들을 열고 닫지 않음"

    def __init__(self, limits: Limits, rate_per_s: float = 20.0) -> None:
        super().__init__(limits)
        self.rate_per_s = rate_per_s
        self._files: list[Any] = []
        self._path = cache_dir() / f"_fault_handle_{os.getpid()}.tmp"
        self._debt = 0.0

    def setup(self) -> None:
        self._path.write_bytes(b"argus fault injector")

    def step(self, intensity: float, dt: float) -> None:
        # 소수점을 버리면 ramp 초반 속도가 0 이 된다 (MemoryLeak 의 `_debt_mb` 참고).
        self._debt += self.rate_per_s * intensity * dt
        want = int(self._debt)
        self._debt -= want
        for _ in range(want):
            if len(self._files) >= self.limits.handles:
                return
            try:
                self._files.append(open(self._path, "rb"))
            except OSError:
                return

    def cleanup(self) -> None:
        for f in self._files:
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    def status(self) -> str:
        return f"열린 핸들 {len(self._files)} / 상한 {self.limits.handles}"

    def params(self) -> dict[str, Any]:
        return {"rate_per_s": self.rate_per_s, "cap": self.limits.handles}

    def expected_effect(self) -> tuple[str, float]:
        # 핸들 수는 하드웨어에 비례하지 않으므로 절대값이 맞다.
        return "handles", min(500.0, self.limits.handles * 0.5)


SCENARIOS: dict[str, type[Scenario]] = {
    s.name: s for s in (MemoryLeak, CpuSpin, DiskThrash, HandleLeak)
}


# ------------------------------------------------------------------ 효과 검증


@dataclass
class Effect:
    """주입 전후 관측치. 라벨이 실제 열화를 가리키는지 확인하는 근거."""

    metric: str
    baseline: float
    peak: float
    mean: float
    required_delta: float

    @property
    def delta(self) -> float:
        return self.peak - self.baseline

    @property
    def observable(self) -> bool:
        return self.delta >= self.required_delta


class EffectMonitor:
    """주입이 실제로 시스템을 움직였는지 잰다.

    **이게 없으면 조용히 쓸모없는 라벨이 쌓인다.** 주입기는 "부하를 걸었다"까지만 알고,
    그 부하가 관측 가능한 열화를 만들었는지는 모른다. 12코어 PC 에서 3스레드를 태우면
    부하는 걸렸지만 `cpu_total` 은 평소 수준이고, 그 구간을 정답이라고 우기면 탐지기
    채점이 통째로 틀어진다. 그래서 주입기가 스스로 확인하고, 아니면 그 자리에서 말한다.
    """

    SAMPLE_S = 1.0

    def __init__(self, scenario: "Scenario") -> None:
        self.metric, self.required = scenario.expected_effect()
        self._samples: list[float] = []
        self._baseline = 0.0
        self._last_disk = psutil.disk_io_counters()
        self._last_at = time.monotonic()

    def _read(self) -> float:
        if self.metric == "cpu_percent":
            return psutil.cpu_percent(interval=None)
        if self.metric == "mem_percent":
            return psutil.virtual_memory().percent
        if self.metric == "disk_write_mbps":
            now = time.monotonic()
            counters = psutil.disk_io_counters()
            elapsed = now - self._last_at
            if counters is None or self._last_disk is None or elapsed <= 0:
                return 0.0
            rate = (counters.write_bytes - self._last_disk.write_bytes) / elapsed / (1024 * 1024)
            self._last_disk, self._last_at = counters, now
            return max(0.0, rate)
        if self.metric == "handles":
            try:
                return float(psutil.Process().num_handles())
            except (psutil.Error, AttributeError):
                return 0.0
        return 0.0

    def measure_baseline(self, seconds: float = 3.0) -> None:
        psutil.cpu_percent(interval=None)  # 첫 호출은 0 을 준다. 버린다.
        values = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(self.SAMPLE_S)
            values.append(self._read())
        self._baseline = sum(values) / len(values) if values else 0.0

    @property
    def baseline(self) -> float:
        return self._baseline

    def sample(self) -> None:
        self._samples.append(self._read())

    def result(self) -> Effect:
        peak = max(self._samples) if self._samples else 0.0
        mean = sum(self._samples) / len(self._samples) if self._samples else 0.0
        return Effect(self.metric, self._baseline, peak, mean, self.required)


# ------------------------------------------------------------------ 실행


class Injector:
    """시나리오를 돌리고 주입 구간을 정답 라벨로 기록한다."""

    def __init__(self, scenario: Scenario, *, ramp: bool, duration_s: float, dry_run: bool) -> None:
        self.scenario = scenario
        self.ramp = ramp
        self.duration_s = duration_s
        self.dry_run = dry_run
        self._stop = threading.Event()
        self._db: Database | None = None
        self._row_id: int | None = None

    def _begin_label(self) -> None:
        if self.dry_run:
            return
        self._db = Database().open()
        params = {**self.scenario.params(), "limits": self.scenario.limits.as_dict()}
        with self._db._lock:  # noqa: SLF001
            cursor = self._db.conn.execute(
                "INSERT INTO fault_injections (scenario, ts_start, pid, params, ramp, completed) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    self.scenario.name,
                    time.time(),
                    os.getpid(),
                    json.dumps(params, ensure_ascii=False),
                    1 if self.ramp else 0,
                ),
            )
            self._db.conn.commit()
            self._row_id = cursor.lastrowid

    def _end_label(self, *, completed: bool, effect: "Effect | None" = None) -> None:
        if self._db is None or self._row_id is None:
            return
        # 열화가 관측되지 않은 주입은 completed=0 으로 남긴다. 스코어보드가 completed=1
        # 만 채점하므로, 쓸모없는 라벨이 자동으로 걸러진다.
        usable = completed and (effect is None or effect.observable)
        notes = ""
        if effect is not None:
            notes = json.dumps(
                {
                    "metric": effect.metric,
                    "baseline": round(effect.baseline, 2),
                    "peak": round(effect.peak, 2),
                    "mean": round(effect.mean, 2),
                    "required_delta": round(effect.required_delta, 2),
                    "observable": effect.observable,
                },
                ensure_ascii=False,
            )
        try:
            with self._db._lock:  # noqa: SLF001
                self._db.conn.execute(
                    "UPDATE fault_injections SET ts_end=?, completed=?, notes=? WHERE id=?",
                    (time.time(), 1 if usable else 0, notes, self._row_id),
                )
                self._db.conn.commit()
        finally:
            self._db.close()
            self._db = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> int:
        limits = self.scenario.limits
        print(f"  시나리오 : {self.scenario.name} — {self.scenario.description}")
        print(f"  지속     : {self.duration_s:.0f}초  ({'점진 증가' if self.ramp else '즉시 최대'})")
        print(f"  상한     : {limits.as_dict()}")
        for note in limits.clamped:
            print(f"    ! 상한 적용: {note}")
        if self.dry_run:
            print("  (dry-run — 실제 부하도 라벨 기록도 하지 않습니다)")
            return 0

        monitor = EffectMonitor(self.scenario)
        print("  기준선   : 주입 전 3초 측정 중...", flush=True)
        monitor.measure_baseline()
        print(f"             {monitor.metric} = {monitor.baseline:.1f}")

        self._begin_label()
        print(f"  라벨     : fault_injections#{self._row_id}  pid={os.getpid()}")

        completed = False
        try:
            self.scenario.setup()
            started = time.monotonic()
            last = started
            next_report = started + 5.0
            next_sample = started + EffectMonitor.SAMPLE_S

            while not self._stop.is_set():
                now = time.monotonic()
                elapsed = now - started
                if elapsed >= self.duration_s:
                    completed = True
                    break

                dt = now - last
                last = now
                intensity = min(1.0, elapsed / self.duration_s) if self.ramp else 1.0
                self.scenario.step(intensity, dt)

                if now >= next_sample:
                    next_sample = now + EffectMonitor.SAMPLE_S
                    monitor.sample()

                if now >= next_report:
                    next_report = now + 5.0
                    current = monitor.result()
                    print(
                        f"    [{elapsed:5.0f}s] 강도 {intensity*100:5.1f}%  "
                        f"{self.scenario.status()}  "
                        f"({monitor.metric} {current.peak:.1f}, 기준 {current.baseline:.1f})",
                        flush=True,
                    )
                self._stop.wait(0.2)
        except KeyboardInterrupt:
            print("\n  중단 요청 — 정리합니다")
        except Exception:
            log.exception("주입 중 오류")
            raise
        finally:
            # 어느 경로로 빠져나가든 반드시 정리한다. 실제 PC 에서 도는 도구다.
            self.scenario.cleanup()
            effect = monitor.result()
            self._end_label(completed=completed, effect=effect)
            print(f"  정리 완료 (정상 종료: {completed})")

        print()
        print(f"  효과 검증 : {effect.metric}")
        print(f"    기준선  {effect.baseline:7.1f}")
        print(f"    최대    {effect.peak:7.1f}   (변화 {effect.delta:+.1f}, 최소 요구 {effect.required_delta:.1f})")
        print(f"    평균    {effect.mean:7.1f}")

        if not completed:
            return 130
        if not effect.observable:
            # 실패로 취급한다. 조용히 넘어가면 쓸모없는 정답 라벨이 쌓이고,
            # 그 위에서 탐지기를 채점하면 전부 오답으로 나온다.
            print()
            print("[FAIL] 주입은 됐지만 관측 가능한 열화가 없다 — 이 라벨은 채점에 쓸 수 없다.")
            print(f"       강도를 올릴 것: --{effect.metric.split('_')[0]}-load 를 높이거나 --duration 을 늘린다.")
            print("       (라벨은 completed=0 으로 기록돼 스코어보드가 자동으로 제외한다)")
            return 2
        print("\n[OK] 관측 가능한 열화 확인 — 채점에 쓸 수 있는 라벨이다.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fault_injector",
        description="결함을 인위로 주입해 탐지기 채점용 정답 라벨을 만든다",
    )
    parser.add_argument("scenario", nargs="?", choices=sorted(SCENARIOS), help="시나리오 이름")
    parser.add_argument("--list", action="store_true", help="시나리오 목록")
    parser.add_argument("--duration", type=float, default=120.0, help="지속 시간(초)")
    parser.add_argument(
        "--ramp",
        action="store_true",
        help="0 에서 최대까지 서서히 올린다 (점진적 열화 — 가장 중요한 시나리오)",
    )
    # 강도는 전부 이 PC 능력 대비 비율이다. 절대량(스레드 수·MB)을 쓰면 PC 마다
    # 전혀 다른 세기의 이상이 되고, 빠른 PC 에서는 아무 일도 일어나지 않는다.
    parser.add_argument("--cpu-load", type=float, default=0.75,
                        help="논리 코어 대비 점유 비율 (기본 0.75)")
    parser.add_argument("--mem-load", type=float, default=0.20,
                        help="전체 RAM 대비 점유 목표 비율 (기본 0.20, 가용 메모리 보호가 우선)")
    parser.add_argument("--disk-load", type=float, default=1.2,
                        help="실측 순차쓰기 대비 목표 속도 배수 (기본 1.2 — 장치가 못 따라오게)")
    parser.add_argument("--handles", type=int, default=2000, help="핸들 시나리오 상한")
    parser.add_argument("--dry-run", action="store_true", help="상한만 보여주고 실행하지 않는다")
    args = parser.parse_args(argv)

    if args.list or not args.scenario:
        print("사용 가능한 시나리오:")
        for name, cls in sorted(SCENARIOS.items()):
            print(f"  {name:14} {cls.description}")
        print()
        print("가장 중요한 것은 --ramp 다. 급격한 결함은 어떤 탐지기든 잡는다.")
        print("어려운 것은 30분에 걸쳐 서서히 나빠지는 쪽이고, 실사용에서 문제가 되는 것도 그쪽이다.")
        return 0

    setup(level="INFO", console=False)  # 콘솔은 진행 표시가 쓰므로 파일 로그만
    limits = resolve_limits(args)
    scenario = SCENARIOS[args.scenario](limits)
    injector = Injector(
        scenario, ramp=args.ramp, duration_s=limits.duration_s, dry_run=args.dry_run
    )

    def handler(signum, _frame):
        injector.stop()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass

    return injector.run()


if __name__ == "__main__":
    sys.exit(main())

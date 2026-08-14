"""이 PC 의 성능 기준선(`machine_profile`)을 만든다.

**왜 필요한가**: "디스크 응답시간 50ms 이상이면 병목" 같은 절대 임계값은 HDD 사용자에겐
상시 오탐이고 NVMe 사용자에겐 영원히 안 걸린다. 배포 대상 프로그램이므로 임계값을
코드에 박을 수 없다. 대신 첫 실행 때 이 PC 가 어느 정도 성능을 내는지 재 두고,
탐지 임계값을 **그 기준선에 상대적으로** 표현한다.

벤치는 3초 내외로 짧게 끝낸다. 정밀 측정이 목적이 아니라 **자릿수(order of magnitude)**
파악이 목적이다. HDD 인지 NVMe 인지만 갈라져도 임계값 스케일은 맞출 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import psutil

from ..logging_setup import get_logger
from ..paths import cache_dir, machine_profile_path

log = get_logger(__name__)

PROFILE_VERSION = 1


@dataclass
class MachineProfile:
    profile_version: int
    created_at: float
    signature: str
    cpu: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    disk: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- 시그니처


def hardware_signature() -> str:
    """하드웨어 구성을 요약한 해시.

    이게 그대로면 굳이 다시 벤치하지 않는다. RAM 증설·CPU 교체 같은 변화가 있으면
    값이 바뀌어 자동으로 재캘리브레이션된다.
    """
    parts = [
        platform.processor(),
        str(psutil.cpu_count(logical=True)),
        str(psutil.cpu_count(logical=False)),
        # 총 메모리는 부팅마다 미세하게 다를 수 있어 GB 단위로 뭉갠다.
        str(round(psutil.virtual_memory().total / (1024**3))),
        platform.machine(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- 벤치


def _bench_cpu_single(duration_s: float = 0.4) -> float:
    """단일 스레드 정수 연산 처리량 (백만 회/초).

    절대 성능 지표가 아니라 **이 PC 안에서 비교 가능한 상대 기준**이다.
    부하 중에 측정하면 낮게 나오므로 재캘리브레이션 조건을 보수적으로 잡는다.
    """
    end = time.perf_counter() + duration_s
    count = 0
    x = 0
    while time.perf_counter() < end:
        # 100 회를 한 묶음으로 — perf_counter 호출 오버헤드를 희석한다.
        for _ in range(100):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        count += 100
    elapsed = duration_s
    return round(count / elapsed / 1_000_000, 3)


def _bench_memory_copy(size_mb: int = 64, rounds: int = 3) -> float:
    """메모리 복사 대역폭 (GB/s)."""
    size = size_mb * 1024 * 1024
    src = bytearray(os.urandom(min(size, 4 * 1024 * 1024))) * max(1, size // (4 * 1024 * 1024))
    src = memoryview(bytes(src[:size]))
    best = 0.0
    for _ in range(rounds):
        start = time.perf_counter()
        dst = bytes(src)  # 실제 복사 발생
        elapsed = time.perf_counter() - start
        if elapsed > 0:
            best = max(best, len(dst) / elapsed / (1024**3))
    return round(best, 2)


def _disk_media_type(drive_letter: str) -> str:
    """데이터 디렉터리가 놓인 물리 디스크의 매체 종류.

    psutil 로는 알 수 없어 Windows 저장소 관리 API 를 PowerShell 로 한 번 조회한다.
    캘리브레이션 시 1회뿐이라 상주 비용은 없다. 실패해도 프로필 생성은 계속한다.
    """
    if sys.platform != "win32":
        return "unknown"
    script = (
        f"$p = Get-Partition -DriveLetter {drive_letter} -ErrorAction Stop; "
        "$d = Get-PhysicalDisk -ErrorAction Stop | "
        "Where-Object { $_.DeviceId -eq $p.DiskNumber }; "
        "if ($d) { $d.MediaType } else { 'unknown' }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            # MediaType 값 자체는 영문이라 로캘이 달라도 대개 맞는다. 그래도 못박는 이유는
            # PowerShell 오류 메시지가 실행 PC 의 언어로 나오기 때문이다 — 그때 디코딩이
            # 깨지면 `subprocess` 가 예외를 던지지 않고 **`stdout` 을 `None` 으로** 돌려주고,
            # 아래 `(result.stdout or "")` 가 조용히 "unknown" 으로 떨어진다.
            # HDD/SSD 판정이 틀린 채로 machine_profile 이 굳는 게 최악이다(규칙 2).
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("디스크 매체 조회 실패", extra={"error": str(e)})
        return "unknown"
    value = (result.stdout or "").strip()
    return value if value else "unknown"


def _bench_disk(size_mb: int) -> dict[str, Any]:
    """데이터 디렉터리가 놓인 볼륨의 디스크 성능.

    SQLite DB 가 여기 놓이므로, 재는 대상이 곧 우리가 쓸 디스크다.

    - **순차 쓰기 + fsync**: 캐시를 우회하는 유일하게 믿을 만한 수치. HDD/SATA/NVMe 가 갈린다.
    - **랜덤 4K 읽기**: 방금 쓴 파일이라 OS 페이지 캐시에 남아 있어 **낙관적으로 나온다.**
      절대값으로 쓰지 말고, 같은 조건에서 잰 값끼리만 비교할 것.
    """
    target_dir = cache_dir()
    path = target_dir / "_calib_bench.tmp"
    size = size_mb * 1024 * 1024
    block = os.urandom(1024 * 1024)
    out: dict[str, Any] = {"bench_size_mb": size_mb}

    try:
        # --- 순차 쓰기 + fsync
        start = time.perf_counter()
        with open(path, "wb") as f:
            for _ in range(size_mb):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        write_elapsed = time.perf_counter() - start
        out["seq_write_mbps"] = round(size_mb / write_elapsed, 1) if write_elapsed > 0 else None

        # --- fsync 단독 지연 (작은 쓰기 후 동기화). DB 커밋 비용의 근사치.
        fsync_samples = []
        with open(path, "r+b") as f:
            for i in range(5):
                f.seek(i * 4096)
                f.write(b"\x00" * 4096)
                f.flush()
                t0 = time.perf_counter()
                os.fsync(f.fileno())
                fsync_samples.append((time.perf_counter() - t0) * 1000)
        fsync_samples.sort()
        out["fsync_ms_median"] = round(fsync_samples[len(fsync_samples) // 2], 3)

        # --- 랜덤 4K 읽기 (캐시 영향 있음 — 위 주석 참고)
        import random

        offsets = [random.randrange(0, max(1, size - 4096)) for _ in range(200)]
        start = time.perf_counter()
        with open(path, "rb") as f:
            for off in offsets:
                f.seek(off)
                f.read(4096)
        read_elapsed = time.perf_counter() - start
        out["rand_read_4k_us_mean"] = round(read_elapsed / len(offsets) * 1_000_000, 1)
        out["rand_read_cached"] = True  # 해석 시 반드시 참고할 것

    except OSError as e:
        log.warning("디스크 벤치 실패", extra={"error": str(e)})
        out["error"] = str(e)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    drive = str(target_dir)[:1]
    out["drive"] = drive
    out["media_type"] = _disk_media_type(drive)
    return out


# --------------------------------------------------------------------------- 조립


def _cpu_info() -> dict[str, Any]:
    freq = psutil.cpu_freq()
    return {
        "processor": platform.processor(),
        "logical": psutil.cpu_count(logical=True),
        "physical": psutil.cpu_count(logical=False),
        "freq_max_mhz": round(freq.max, 1) if freq and freq.max else None,
        "single_thread_mops": _bench_cpu_single(),
    }


def _memory_info() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "total_gb": round(vm.total / (1024**3), 2),
        "swap_total_gb": round(sm.total / (1024**3), 2),
        "copy_gbps": _bench_memory_copy(),
    }


def calibrate(*, disk_bench_mb: int = 16) -> MachineProfile:
    """벤치를 돌려 새 프로필을 만든다."""
    started = time.perf_counter()
    profile = MachineProfile(
        profile_version=PROFILE_VERSION,
        created_at=time.time(),
        signature=hardware_signature(),
        cpu=_cpu_info(),
        memory=_memory_info(),
        disk=_bench_disk(disk_bench_mb),
    )
    profile.notes.append(
        "랜덤 읽기 수치는 OS 페이지 캐시 영향을 받아 낙관적이다. 절대값으로 해석하지 말 것."
    )
    if psutil.cpu_percent(interval=None) > 50:
        profile.notes.append("캘리브레이션 중 시스템 부하가 높았다. 수치가 낮게 나왔을 수 있다.")
    log.info(
        "캘리브레이션 완료",
        extra={"elapsed_s": round(time.perf_counter() - started, 2), "signature": profile.signature},
    )
    return profile


def load_profile() -> MachineProfile | None:
    path = machine_profile_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return MachineProfile(**data)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("machine_profile 읽기 실패 — 재생성한다", extra={"error": str(e)})
        return None


def save_profile(profile: MachineProfile) -> None:
    try:
        machine_profile_path().write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning("machine_profile 저장 실패", extra={"error": str(e)})


def ensure_profile(
    *, disk_bench_mb: int = 16, reuse_days: int = 90, force: bool = False
) -> MachineProfile:
    """프로필을 확보한다. 유효한 게 있으면 재사용, 없으면 새로 만든다.

    재캘리브레이션 조건: 없음 / 버전 불일치 / 하드웨어 시그니처 변경 / 기한 초과.
    """
    if not force:
        existing = load_profile()
        if existing is not None:
            fresh_enough = reuse_days == 0 or (
                time.time() - existing.created_at < reuse_days * 86400
            )
            if (
                existing.profile_version == PROFILE_VERSION
                and existing.signature == hardware_signature()
                and fresh_enough
            ):
                return existing
            log.info("기준선을 다시 잰다 (하드웨어 변경 또는 만료)")

    profile = calibrate(disk_bench_mb=disk_bench_mb)
    save_profile(profile)
    return profile


if __name__ == "__main__":  # 스모크: python -m argus.machine.calibration
    from ..logging_setup import setup

    setup(level="INFO")
    p = ensure_profile(force="--force" in sys.argv)
    print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))
    print(f"  저장: {machine_profile_path()}")
    print("[OK] machine.calibration")

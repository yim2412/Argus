"""이 PC 에서 어떤 계측 소스를 쓸 수 있는지 탐지한다.

Argus 는 관리자 권한을 **요구하지 않는다**. ETW·서명검증 같은 것은 권한이 있을 때만
켜지는 부가 기능이고, 없으면 그 기능만 끄고 나머지는 정상 동작해야 한다.

중요한 건 **조용히 실패하지 않는 것**이다. 왜 꺼졌는지(`reason`)를 같이 기록해서
대시보드에 "이 기능은 관리자 권한이 필요합니다"로 드러낼 수 있게 한다.
"""

from __future__ import annotations

import ctypes
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..paths import capabilities_path

log = get_logger(__name__)


@dataclass
class Capability:
    """계측 소스 하나의 가용 여부."""

    available: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capabilities:
    os: dict[str, str]
    is_admin: Capability
    psutil: Capability
    pdh: Capability
    nvml: Capability
    etw: Capability
    code_signing: Capability

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> list[str]:
        """사람이 읽는 한 줄씩. 스모크·대시보드용."""
        out = []
        for name in ("is_admin", "psutil", "pdh", "nvml", "etw", "code_signing"):
            cap: Capability = getattr(self, name)
            mark = "OK  " if cap.available else "----"
            note = f"  ({cap.reason})" if cap.reason else ""
            out.append(f"  [{mark}] {name}{note}")
        return out


def _detect_admin() -> Capability:
    if sys.platform != "win32":
        return Capability(False, "Windows 아님")
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError) as e:
        return Capability(False, f"확인 실패: {e}")
    return Capability(is_admin, "" if is_admin else "관리자 권한 없음 (정상 — 필수 아님)")


def _detect_psutil() -> Capability:
    try:
        import psutil
    except ImportError as e:
        return Capability(False, f"psutil 없음: {e}")
    return Capability(True, detail={"version": psutil.__version__})


def _detect_pdh() -> Capability:
    """Windows 성능 카운터.

    카운터명은 로케일별로 번역되어 있어 영문 문자열로 조회하면 한국어 Windows 에서
    전부 실패한다. 반드시 인덱스 → 지역화된 이름으로 변환해서 써야 한다.
    여기서는 그 변환이 실제로 되는지까지 확인한다.
    """
    if sys.platform != "win32":
        return Capability(False, "Windows 아님")
    try:
        import win32pdh
    except ImportError as e:
        return Capability(False, f"pywin32 없음: {e}")

    try:
        # 238 = "Processor", 6 = "% Processor Time" (인덱스는 로케일 무관)
        localized_object = win32pdh.LookupPerfNameByIndex(None, 238)
        localized_counter = win32pdh.LookupPerfNameByIndex(None, 6)
    except Exception as e:  # win32pdh 는 pywintypes.error 등 다양한 예외를 던진다
        return Capability(False, f"카운터 이름 조회 실패: {e}")

    return Capability(
        True,
        detail={
            "object_238": localized_object,
            "counter_6": localized_counter,
            "locale_note": "인덱스 기반 조회 확인됨",
        },
    )


def _detect_nvml() -> Capability:
    """NVIDIA GPU. 없으면 GPU 메트릭만 빠지고 나머지는 그대로 동작한다."""
    try:
        import pynvml
    except ImportError as e:
        return Capability(False, f"nvidia-ml-py 없음: {e}")

    try:
        pynvml.nvmlInit()
    except Exception as e:
        # NVIDIA 드라이버가 없거나 GPU 가 AMD/Intel 인 경우. 정상 상황이다.
        return Capability(False, f"NVML 초기화 실패 (NVIDIA GPU 없음으로 간주): {e}")

    try:
        count = pynvml.nvmlDeviceGetCount()
        names = []
        for i in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            names.append(name.decode() if isinstance(name, bytes) else name)
        driver = pynvml.nvmlSystemGetDriverVersion()
        return Capability(
            True,
            detail={
                "device_count": count,
                "devices": names,
                "driver": driver.decode() if isinstance(driver, bytes) else driver,
            },
        )
    except Exception as e:
        return Capability(False, f"NVML 조회 실패: {e}")
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _detect_etw(is_admin: bool) -> Capability:
    """커널 이벤트 추적. Phase 12 에서 실제로 쓰고, 여기서는 가능 여부만 본다."""
    if sys.platform != "win32":
        return Capability(False, "Windows 아님")
    if not is_admin:
        return Capability(False, "관리자 권한 필요")
    return Capability(True, detail={"note": "권한 충족 — 실제 세션 생성은 Phase 12 에서 검증"})


def _detect_code_signing() -> Capability:
    """실행 파일 서명 검증(WinVerifyTrust). Phase 13 보안 탐지에서 쓴다."""
    if sys.platform != "win32":
        return Capability(False, "Windows 아님")
    try:
        ctypes.WinDLL("wintrust")
    except OSError as e:
        return Capability(False, f"wintrust 로드 실패: {e}")
    return Capability(True)


def detect() -> Capabilities:
    """전체 탐지. 개별 탐지가 실패해도 나머지는 계속한다."""
    admin = _detect_admin()
    caps = Capabilities(
        os={
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        is_admin=admin,
        psutil=_detect_psutil(),
        pdh=_detect_pdh(),
        nvml=_detect_nvml(),
        etw=_detect_etw(admin.available),
        code_signing=_detect_code_signing(),
    )
    return caps


def load_or_detect(*, refresh: bool = False) -> Capabilities:
    """저장된 결과를 쓰거나 새로 탐지한다.

    capabilities 는 드라이버 설치·권한 상승으로 바뀔 수 있으므로 캐시를 오래 믿지 않고
    실행할 때마다 다시 탐지한다. 저장은 진단·대시보드 표시용이다.
    """
    caps = detect()
    try:
        capabilities_path().write_text(
            json.dumps(caps.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        log.warning("capabilities 저장 실패", extra={"error": str(e)})
    return caps


if __name__ == "__main__":  # 스모크: python -m argus.machine.capabilities
    from ..logging_setup import setup

    setup(level="WARNING")
    c = load_or_detect()
    print(f"  OS: {c.os['system']} {c.os['release']} ({c.os['machine']})")
    for line in c.summary():
        print(line)
    if c.nvml.available:
        print(f"  GPU: {c.nvml.detail.get('devices')}")
    if c.pdh.available:
        print(f"  PDH 로케일 확인: 238 -> {c.pdh.detail.get('object_238')!r}")
    print(f"  저장: {capabilities_path()}")
    print("[OK] machine.capabilities")

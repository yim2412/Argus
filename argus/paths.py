"""파일 경로 결정.

두 종류를 구분한다.
  - **데이터 경로**: 사용자별 영구 저장 위치(`%APPDATA%\\Argus`). DB·설정·로그·모델.
    exe(onefile)는 실행 폴더가 임시 디렉터리라 그 옆에 쓰면 재부팅 후 사라진다.
  - **리소스 경로**: 패키지에 동봉된 읽기 전용 파일(기본 설정 YAML, 스키마 SQL).
    PyInstaller 로 묶이면 `sys._MEIPASS` 아래로 풀리므로 상대경로는 조용히 깨진다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Argus"

# 데이터 위치 오버라이드. 테스트·개발 시 실사용 데이터를 건드리지 않으려면 이걸 쓴다.
ENV_DATA_DIR = "ARGUS_DATA_DIR"

# 알림 억제. **`ARGUS_DATA_DIR` 로 격리되지 않는 것이 하나 있다 — 화면이다.**
# DB·설정·로그는 임시 폴더로 보낼 수 있지만 트레이 풍선은 사용자에게 그대로 간다.
# 2026-08-06 에 `test_shutdown` 이 진짜 상주를 8번 띄우면서 "Argus 감시 시작" 을
# 8번 발송했다. 테스트가 실사용자의 화면을 건드린 것이다.
ENV_NO_NOTIFY = "ARGUS_NO_NOTIFY"


def notifications_suppressed() -> bool:
    """이 실행에서 풍선 알림을 띄우면 안 되는가.

    **매번 환경을 다시 본다.** 기동 시 한 번 읽어 두면 값을 바꿔 확인하는 테스트가
    프로세스 재시작을 요구하게 된다 — 비용은 `os.environ` 조회 한 번이라 없다.

    빈 값·`0`·`false`·`no` 는 끄는 것으로 본다. 그 외 값은 전부 켬이다 —
    `ARGUS_NO_NOTIFY=0` 을 "억제 켬"으로 읽으면 정반대로 동작한다.
    """
    return os.environ.get(ENV_NO_NOTIFY, "").strip().lower() not in ("", "0", "false", "no")


def is_frozen() -> bool:
    """PyInstaller 로 묶인 상태인가."""
    return getattr(sys, "frozen", False)


def resource_path(relative: str) -> Path:
    """패키지 동봉 리소스의 실제 경로.

    묶인 상태에서는 `sys._MEIPASS`, 아니면 소스 트리 기준.
    """
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    return base / relative


def icon_path() -> Path:
    """트레이·창·exe 가 공유하는 아이콘.

    **동봉 리소스라 `_MEIPASS` 를 거쳐야 한다.** 상대경로로 열면 소스에서는 되고
    exe 에서는 조용히 깨진다(CLAUDE.md 배포 규칙).
    """
    return resource_path("assets/argus.ico")


def data_dir() -> Path:
    """영구 데이터 루트. 없으면 만든다."""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        root = Path(override).expanduser()
    else:
        appdata = os.environ.get("APPDATA")
        # APPDATA 는 Windows 에서 항상 있지만, 서비스 계정이나 비-Windows 개발
        # 환경에서 비어 있을 수 있어 홈 디렉터리로 떨어뜨린다.
        base = Path(appdata) if appdata else Path.home() / ".local" / "share"
        root = base / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sub(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    return _sub("logs")


def models_dir() -> Path:
    return _sub("models")


def cache_dir() -> Path:
    return _sub("cache")


def db_path() -> Path:
    return data_dir() / "argus.db"


def user_config_path() -> Path:
    """사용자가 편집하는 설정 파일. 첫 실행 시 기본값으로 생성된다."""
    return data_dir() / "settings.yaml"


def runtime_config_path() -> Path:
    """UI 에서 바꾼 값이 저장되는 곳. **`settings.yaml` 과 나누는 이유가 있다.**

    `settings.yaml` 은 `defaults.yaml` 사본이라 **주석이 곧 설명서**다. 프로그램이
    YAML 을 다시 쓰면 그 주석이 전부 날아간다 — 사람이 편집하는 파일과 프로그램이
    쓰는 파일을 나눈다.
    """
    return data_dir() / "runtime.yaml"


def window_state_path() -> Path:
    """창 크기·위치. **`runtime.yaml` 과 나누는 이유가 있다.**

    저것은 상주가 매초 읽는 설정이고 이것은 창이 닫힐 때만 쓰는 화면 상태다.
    섞으면 창을 옮길 때마다 상주가 설정 변경으로 받아들여 다시 읽는다.
    """
    return data_dir() / "window.json"


def machine_profile_path() -> Path:
    return data_dir() / "machine_profile.json"


def capabilities_path() -> Path:
    return data_dir() / "capabilities.json"


def stop_file_path() -> Path:
    """이 파일이 생기면 상주 인스턴스가 스스로 정상 종료한다.

    **Windows 에는 남의 프로세스에 "곱게 죽어라"를 전할 방법이 없다.** SIGTERM 은
    존재하지 않고, `pythonw` 로 콘솔 없이 도는 프로세스에는 Ctrl 이벤트도 닿지 않으며,
    창이 없으니 `taskkill` 의 WM_CLOSE 도 무의미하다. 남는 것은 강제 종료뿐인데,
    그러면 정상적으로 끈 것까지 전부 `unclean_shutdown` 으로 기록된다 —
    사후 진단이 크래시와 사용자 의도를 구분하지 못하게 된다.

    그래서 파일 하나를 신호로 쓴다. 권한도, 창도, 콘솔도 필요 없다.
    """
    return data_dir() / "STOP"


def describe() -> dict[str, str]:
    """진단·스모크 출력용."""
    return {
        "frozen": str(is_frozen()),
        "data_dir": str(data_dir()),
        "db": str(db_path()),
        "config": str(user_config_path()),
        "logs": str(logs_dir()),
    }


if __name__ == "__main__":  # 스모크: python -m argus.paths
    for k, v in describe().items():
        print(f"  {k:10} = {v}")
    print("[OK] paths")

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


def machine_profile_path() -> Path:
    return data_dir() / "machine_profile.json"


def capabilities_path() -> Path:
    return data_dir() / "capabilities.json"


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

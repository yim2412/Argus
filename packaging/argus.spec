# PyInstaller 빌드 정의. `--onedir` 이다 — onefile 은 시작이 느리고 DLL 문제가 잦다.
#
# 빌드:  .venv\Scripts\pyinstaller.exe packaging\argus.spec --noconfirm
#
# **대시보드(Streamlit)는 넣지 않는다.** 배포의 최소 단위는 "수집하고 탐지하는 상주
# 프로세스"이고, Streamlit 은 자체 자원 파일·동적 import 가 많아 별도 문제다. 그것 때문에
# 전체 빌드가 막히면 무엇이 진짜 장애물인지 알 수 없게 된다.
#
# **리소스는 `paths.resource_path()` 가 `_MEIPASS` 를 거쳐 찾는다.** 그래서 datas 의
# 목적지 경로가 소스 트리에서의 상대경로와 **정확히 같아야** 한다:
#     resource_path("config/defaults.yaml")     → _MEIPASS/config/defaults.yaml
#     resource_path("storage/migrations")       → _MEIPASS/storage/migrations/
# 이게 어긋나면 exe 가 기본 설정을 못 찾아 조용히 기본값으로 돌거나 기동에 실패한다.

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# **상대경로를 쓰지 않는다.** spec 안의 상대경로는 spec 파일 위치가 아니라 빌드를
# **실행한 작업 디렉터리** 기준으로 풀린다. 첫 빌드에서 `pathex=[".."]` 가 프로젝트의
# 부모를 가리켜 `argus` 패키지가 통째로 빠졌고, 빌드는 성공한 뒤 실행에서
# `ModuleNotFoundError: No module named 'argus'` 로 죽었다 — 빌드 성공은 절반이다.
# `SPECPATH` 는 PyInstaller 가 넣어 주는 spec 파일의 디렉터리다.
PROJECT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller 제공
sys.path.insert(0, PROJECT)  # collect_submodules("argus") 가 찾을 수 있게

# 아이콘은 두 곳에 필요하다 — exe 자원(`icon=`, 탐색기가 본다)과 동봉 파일(트레이가
# 런타임에 `LoadImage` 로 읽는다). exe 자원만 넣으면 트레이가 파일을 못 찾아 시스템
# 아이콘으로 떨어진다.
ICON = os.path.join(PROJECT, "argus", "assets", "argus.ico")

datas = [
    (os.path.join(PROJECT, "argus", "config", "defaults.yaml"), "config"),
    (os.path.join(PROJECT, "argus", "config", "rules.yaml"), "config"),
    (os.path.join(PROJECT, "argus", "storage", "migrations"), "storage/migrations"),
    (ICON, "assets"),
]

# PDH·NVML 은 문자열로 늦게 import 되는 자리가 있어 정적 분석에 안 잡힐 수 있다.
hiddenimports = [
    "win32pdh",
    "win32api",
    "win32con",
    "win32process",
    "win32security",
    "pywintypes",
    "pynvml",
    "duckdb",
    "pyarrow",
    "pyarrow.parquet",
]
hiddenimports += collect_submodules("argus")

# 넣지 않는 것. 빌드 크기와 시간을 좌우한다.
excludes = [
    "streamlit",
    "plotly",
    "altair",
    "torch",
    "sklearn",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
    "notebook",
]

a = Analysis(
    [os.path.join(SPECPATH, "argus_entry.py")],  # noqa: F821
    pathex=[PROJECT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="argus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 는 백신 오탐을 부른다. 배포용이라 켜지 않는다.
    console=True,       # 1차 빌드는 콘솔을 남긴다 — 실패 원인을 봐야 한다.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="argus",
)

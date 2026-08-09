# 데스크톱 창(PySide6) 빌드. 상주(`argus.spec`)와 **별도 exe** 다.
#
# 나누는 이유는 구조 그대로다 — 창과 상주는 다른 프로세스이고, 창이 죽어도 수집은
# 계속돼야 한다(설계 규칙 1). 한 exe 에 넣으면 Qt 런타임이 상주에도 딸려 들어간다.
#
# 빌드:  .venv\Scripts\pyinstaller.exe packaging\argus_ui.spec --noconfirm
#
# **PySide6 는 설치본이 643MB 다**(2026-08-03 실측). 대부분 `pyside6-addons` 의
# WebEngine·3D·Multimedia 인데 이 앱은 위젯과 차트만 쓴다. 아래 excludes 가 그것을
# 걷어낸다 — 빼지 않으면 배포판이 쓸모없이 커진다.

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

PROJECT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - PyInstaller 제공
sys.path.insert(0, PROJECT)

# 상주 exe 와 **같은 아이콘**을 쓴다. 사용자에게는 한 앱이고, AppUserModelID 로 둘을
# 한 그룹에 묶어 두었으므로 아이콘이 갈리면 그 묶음이 어색해진다.
ICON = os.path.join(PROJECT, "argus", "assets", "argus.ico")

datas = [
    (os.path.join(PROJECT, "argus", "config", "defaults.yaml"), "config"),
    (os.path.join(PROJECT, "argus", "config", "rules.yaml"), "config"),
    (os.path.join(PROJECT, "argus", "storage", "migrations"), "storage/migrations"),
    (ICON, "assets"),
]

hiddenimports = ["pyqtgraph", "duckdb", "pyarrow", "pyarrow.parquet"]
hiddenimports += collect_submodules("argus.dashboard")
hiddenimports += collect_submodules("argus.desktop")

# 쓰지 않는 Qt 모듈. 크기의 대부분이 여기 있다.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtCharts",  # 차트는 pyqtgraph 가 그린다
    "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtWebSockets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    # 상주 쪽과 같은 이유로 뺀다
    "torch",
    "sklearn",
    "matplotlib",
    "tkinter",
    "pytest",
    "IPython",
]

a = Analysis(
    [os.path.join(SPECPATH, "argus_ui_entry.py")],  # noqa: F821
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
    name="argus-ui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 는 백신 오탐을 부른다
    console=True,       # 1차 빌드는 콘솔을 남긴다 — 실패 원인을 봐야 한다
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
    name="argus-ui",
)

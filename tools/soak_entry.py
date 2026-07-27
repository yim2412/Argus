"""창 없는 진입점. 작업 스케줄러가 이것을 base pythonw 로 실행한다.

**왜 venv 의 pythonw 를 직접 쓰지 않는가** — uv 가 만든 `.venv\\Scripts\\pythonw.exe` 는
45KB 짜리 **트램폴린**이다. 실행하면 uv 가 관리하는 진짜 인터프리터를 다시 띄우는데,
그때 뜨는 것이 `python.exe`(콘솔 서브시스템)라서 **콘솔 창이 생긴다.**

콘솔이 생기면 두 가지가 따라온다.
- 창을 닫을 수 있다. 닫으면 Argus 가 죽는다.
- Ctrl+C 가 도달할 수 있다. 2026-07-27 에 작업 종료 코드 0xC000013A
  (STATUS_CONTROL_C_EXIT) 로 실제로 죽었다.

base `pythonw.exe` 는 GUI 서브시스템이라 콘솔을 아예 만들지 않는다. 대신 venv 를
쓰지 않으므로 여기서 경로를 세워 준다.

**`sys.path.insert` 로는 부족하다.** 경로만 넣으면 `.pth` 파일이 실행되지 않는다.
pywin32 는 `pywin32.pth` 가 DLL 폴더와 `win32/` 하위 디렉터리를 등록해 줘야 import 되고,
그게 없으면 PDH 가 통째로 죽는다 — 2026-07-27 에 실제로 그렇게 되어 프로세스 수집이
psutil 폴백(15초 주기)으로 내려가고 `disk_resp_ms`·`disk_queue` 가 전부 NULL 로 쌓였다.
조용히 죽지는 않았다(경고 로그는 남았다). `site.addsitedir()` 는 `.pth` 를 처리한다.

`tools/README.md` 에 네 번의 사망 원인이 정리돼 있다.
"""

from __future__ import annotations

import site
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SITE_PACKAGES = PROJECT / ".venv" / "Lib" / "site-packages"

if SITE_PACKAGES.is_dir():
    site.addsitedir(str(SITE_PACKAGES))   # .pth 실행 — pywin32 에 필수
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def _verify() -> list[str]:
    """필수 의존성이 실제로 import 되는지 확인한다.

    조용히 성능 저하 모드로 도는 것이 가장 나쁘다. 몇 시간 뒤 데이터를 열어 보고서야
    PDH 가 없었다는 걸 알게 된다.
    """
    missing = []
    for module in ("psutil", "yaml", "win32pdh"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    return missing


if __name__ == "__main__":
    absent = _verify()
    if absent:
        print(f"[FAIL] 의존성 없음: {', '.join(absent)} — sys.path 설정 확인", file=sys.stderr)
        sys.exit(1)

    from argus.__main__ import main

    sys.exit(main())

"""창 없는 진입점. 작업 스케줄러가 이것을 base pythonw 로 실행한다.

**왜 venv 의 pythonw 를 직접 쓰지 않는가** — uv 가 만든 `.venv\\Scripts\\pythonw.exe` 는
45KB 짜리 **트램폴린**이다. 실행하면 uv 가 관리하는 진짜 인터프리터를 다시 띄우는데,
그때 뜨는 것이 `python.exe`(콘솔 서브시스템)라서 **콘솔 창이 생긴다.**

콘솔이 생기면 두 가지가 따라온다.
- 창을 닫을 수 있다. 닫으면 Argus 가 죽는다.
- Ctrl+C 가 도달할 수 있다. 2026-07-27 에 작업 종료 코드 0xC000013A
  (STATUS_CONTROL_C_EXIT) 로 실제로 죽었다.

base `pythonw.exe` 는 GUI 서브시스템이라 콘솔을 아예 만들지 않는다. 대신 venv 를
쓰지 않으므로 여기서 sys.path 를 직접 세워 준다 — venv 활성화가 하는 일이 결국
site-packages 를 경로에 넣는 것뿐이다.

`tools/README.md` 에 세 번의 사망 원인이 정리돼 있다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SITE_PACKAGES = PROJECT / ".venv" / "Lib" / "site-packages"

for path in (SITE_PACKAGES, PROJECT):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

if __name__ == "__main__":
    from argus.__main__ import main

    sys.exit(main())

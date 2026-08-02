"""exe 진입점.

`python -m argus` 는 `argus/__main__.py` 를 실행하지만 PyInstaller 에는 `-m` 이 없다.
패키지 안의 `__main__.py` 를 직접 스크립트로 지정하면 상대 import 가 깨지므로,
얇은 진입 스크립트를 하나 두고 거기서 정상 import 로 부른다.

**여기에 로직을 넣지 않는다.** 소스 실행과 exe 실행이 같은 경로를 타야 "개발에서는
되는데 exe 에서만 다르다"가 생기지 않는다.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # 묶인 exe 에서 자식 프로세스가 자신을 다시 실행하며 무한 증식하는 것을 막는다.
    # PyInstaller 공식 권고이고, 빠뜨리면 exe 를 켜는 순간 프로세스가 폭증한다.
    multiprocessing.freeze_support()

    from argus.__main__ import main

    sys.exit(main())

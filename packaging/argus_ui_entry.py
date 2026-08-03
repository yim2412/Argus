"""데스크톱 창 exe 진입점.

상주 진입점(`argus_entry.py`)과 나눠 두는 이유는 프로세스가 별개이기 때문이다 —
창이 죽어도 수집은 계속돼야 한다. 여기에도 로직을 넣지 않는다.
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()

    from argus.desktop.app import cli

    sys.exit(cli())

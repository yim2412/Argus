"""파일 하나로 요청하는 정상 종료.

Windows 에서 콘솔 없이 도는 상주 프로세스를 **깨끗하게** 끄는 방법이 없다는 것이
출발점이다. 자세한 배경은 `paths.stop_file_path` 참조.

동작은 단순하다. `%APPDATA%\\Argus\\STOP` 이 생기면 지우고 종료를 요청한다. 지우는 쪽이
만든 쪽이 아니라 **읽는 쪽**인 이유는, 파일이 남아 있으면 다음 기동이 즉시 다시 죽기
때문이다. 소비하는 순간 신호는 사라져야 한다.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..logging_setup import get_logger
from ..paths import stop_file_path
from .supervisor import Component

log = get_logger(__name__)


def request_stop(path: Path | None = None) -> Path:
    """종료를 요청한다. 상주 인스턴스가 없으면 파일만 남고 다음 기동이 이를 치운다."""
    target = path or stop_file_path()
    target.write_text("", encoding="utf-8")
    return target


def clear_stale(path: Path | None = None) -> bool:
    """기동 시 남아 있는 신호를 치운다.

    직전 세션이 이 파일을 소비하기 전에 강제 종료됐다면 파일이 남는다. 그대로 두면
    새 인스턴스가 뜨자마자 죽어 사용자는 "실행이 안 된다"만 보게 된다.
    """
    target = path or stop_file_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        log.warning("남아 있는 종료 신호를 지우지 못했다", extra={"path": str(target)})
        return False
    log.info("직전 세션이 남긴 종료 신호를 치웠다", extra={"path": str(target)})
    return True


class StopFileMonitor(Component):
    """종료 신호 파일을 지켜본다."""

    name = "stopfile"
    # 종료 요청은 사람이 기다리는 동작이라 반응이 빨라야 한다. `os.path.exists` 한 번은
    # 비용이 없다시피 해서 예산에 영향을 주지 않는다.
    interval_s = 2.0
    # 스로틀이 걸린 상태(= 이미 부하가 높은 상태)에서야말로 사용자가 끄고 싶어 한다.
    # 종료 반응이 같이 느려지면 안 된다.
    throttleable = False

    def __init__(self, on_stop: Callable[[], None], path: Path | None = None) -> None:
        self._on_stop = on_stop
        self._path = path or stop_file_path()

    def tick(self) -> None:
        if not os.path.exists(self._path):
            return
        # **먼저 지우고 종료를 요청한다.** 순서가 반대면, 지우기 전에 프로세스가 내려가
        # 파일이 남고 다음 기동이 곧바로 다시 죽는다.
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # 지우지 못했더라도 요청 자체는 존중한다. 잔재는 다음 기동이 치운다.
            log.warning("종료 신호 파일을 지우지 못했다", extra={"path": str(self._path)})
        log.info("종료 신호 파일 감지 — 정상 종료한다", extra={"path": str(self._path)})
        self._on_stop()


if __name__ == "__main__":  # 스모크: python -m argus.runtime.stopfile
    import tempfile
    import time

    from ..logging_setup import setup
    from .supervisor import Supervisor

    setup(level="INFO")

    with tempfile.TemporaryDirectory() as tmp:
        signal_path = Path(tmp) / "STOP"

        sup = Supervisor()
        sup.add(StopFileMonitor(sup.request_stop, path=signal_path))
        sup.start()

        time.sleep(0.5)
        if sup.stopping:
            print("[FAIL] 신호가 없는데 종료를 요청했다")
            raise SystemExit(1)

        request_stop(signal_path)
        deadline = time.monotonic() + 10.0
        while not sup.stopping and time.monotonic() < deadline:
            time.sleep(0.1)
        sup.stop()

        if not sup.stopping:
            print("[FAIL] 종료 신호를 감지하지 못했다")
            raise SystemExit(1)
        if signal_path.exists():
            print("[FAIL] 신호 파일이 소비되지 않았다 — 다음 기동이 즉시 죽는다")
            raise SystemExit(1)

    print("  종료 신호 감지 및 파일 소비 확인")
    print("[OK] runtime.stopfile")

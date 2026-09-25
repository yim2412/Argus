"""F-029 프로브 — 창이 곧바로 죽을 때 트레이가 보여 주는 이유가 CP949 PC 에서 깨지는가.

`TrayIcon._watch_dashboard` 는 창 자식의 stderr 를 **바이트로** 받아 `decode("utf-8", "replace")` 한다.
창 프로세스(`argus.desktop.app`)는 `logging_setup.setup()` 을 부르지 않아 stderr 인코딩을 못박지 않는다 →
실행 PC 의 로캘을 따른다. 배포 대상(CP949)에서 한글 오류 문구가 UTF-8 로 읽혀 `�` 가 된다.
이 PC 는 UTF-8 로캘이라 **여기서는 재현되지 않는다** — 자식 stdio 를 `PYTHONIOENCODING=cp949` 로 되돌려 흉내 낸다
(전역 인코딩 규칙 6-a).

PASS = 트레이가 넘긴 이유에 원문 한글이 그대로 있다.  FAIL = 깨진다.
대조: 자식이 UTF-8 이면(이 PC 의 기본) 원문이 그대로여야 한다.
"""
import logging
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.CRITICAL)

MSG = "창 설정 파일을 읽을 수 없습니다"
CHILD = f"import sys; sys.stderr.write('RuntimeError: {MSG}\\n'); sys.exit(1)"


def reason_for(child_encoding: str) -> str:
    from argus.ui.tray import TrayIcon

    shown: list[str] = []
    tray = object.__new__(TrayIcon)
    tray.notify = lambda title, message, severity="warning", incident_id=None: shown.append(message) or True
    env = dict(os.environ, PYTHONIOENCODING=child_encoding)
    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    TrayIcon._watch_dashboard(tray, proc)
    return shown[0] if shown else ""


ctrl = reason_for("utf-8")
print(f"대조(자식 UTF-8): {ctrl}")
if MSG not in ctrl:
    print("[FAIL] 대조가 성립하지 않는다 — 프로브를 먼저 고친다")
    sys.exit(2)
got = reason_for("cp949")
print(f"자식 CP949(배포 대상 흉내): {got}")
if MSG in got:
    print("[PASS] CP949 PC 에서도 창 실패 이유가 읽힌다")
    sys.exit(0)
print("[FAIL] CP949 PC 에서 창 실패 이유의 한글이 깨져 사용자에게 보인다")
sys.exit(1)

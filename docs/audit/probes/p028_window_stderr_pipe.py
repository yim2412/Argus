"""F-028 프로브 — 트레이가 창을 `stderr=PIPE` 로 띄우고 5초 뒤 손을 떼면, 창이 stderr 를 채우는 순간 얼어붙는가.

`TrayIcon._open_dashboard` 는 `Popen(..., stderr=PIPE)` 로 창을 띄우고, `_watch_dashboard` 가
`communicate(timeout=5)` 로 5초만 지켜본 뒤 돌아간다. 그 뒤로는 파이프를 아무도 읽지 않는다.
창은 로깅을 설정하지 않아 WARNING 이상이 `logging.lastResort` 로 stderr 에 간다(Qt 경고·슬롯 예외
트레이스백도). 파이프 버퍼가 차면 다음 쓰기가 **창의 GUI 스레드를 막는다.**

제품과 같은 `Popen` 모양 + 같은 감시 함수(`_watch_dashboard`)를 쓰고, 자식은 5초 뒤부터 경고 로그를
한 줄씩(약 100바이트) 쓴다. 자식이 N줄을 다 쓰고 끝냈는지 본다.

PASS = 자식이 제시간(20초)에 끝난다.  FAIL = 쓰기에서 막혀 안 끝난다.
대조: 부모가 stderr 를 계속 읽으면(DEVNULL) 끝나야 한다.
"""
import logging
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.CRITICAL)

CHILD = r"""
import logging, time
log = logging.getLogger("argus.dashboard.data")   # 창이 쓰는 조회 계층과 같은 모양 — 핸들러 없음
time.sleep(6)                                      # 트레이의 5초 감시가 끝난 뒤
for i in range(400):                               # 약 40KB
    log.warning("조회 실패 — 다음 틱에 다시 시도한다 %03d %s", i, "x" * 40)
print("done", flush=True)
"""


def run(stderr_target) -> bool:
    from argus.ui.tray import TrayIcon

    proc = subprocess.Popen([sys.executable, "-c", CHILD], stdout=subprocess.DEVNULL, stderr=stderr_target,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if stderr_target is subprocess.PIPE:
        TrayIcon._watch_dashboard(object.__new__(TrayIcon), proc)   # 제품의 감시 — 5초 뒤 돌아온다
    try:
        proc.wait(timeout=20)
        return True
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


t0 = time.time()
ctrl = run(subprocess.DEVNULL)
print(f"대조(stderr 버림): 제시간에 끝남 {ctrl} ({time.time() - t0:.1f}초)")
if not ctrl:
    print("[FAIL] 대조가 성립하지 않는다 — 프로브를 먼저 고친다")
    sys.exit(2)
t0 = time.time()
ok = run(subprocess.PIPE)
print(f"제품 모양(stderr=PIPE, 5초 감시 뒤 방치): 제시간에 끝남 {ok} ({time.time() - t0:.1f}초)")
if ok:
    print("[PASS] 창이 stderr 를 채워도 막히지 않는다")
    sys.exit(0)
print("[FAIL] 5초 감시가 끝난 뒤 아무도 stderr 를 안 읽어, 경고 로그 몇 KB 에 창 프로세스가 멈춘다")
sys.exit(1)

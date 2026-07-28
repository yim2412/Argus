"""정상 종료 검증.

Ctrl+C 를 눌렀을 때 프로세스가 살아남거나 DB 가 잠긴 채로 남으면 상주 프로그램으로서
치명적이다. 실제로 시그널을 보내서 확인한다.

Windows 에서는 다른 프로세스로 CTRL_C_EVENT 를 보낼 수 없어 CTRL_BREAK_EVENT 를 쓴다.
둘 다 `Supervisor.install_signal_handlers` 가 같은 핸들러로 받는다.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # 리눅스/개발자 환경 대비
    PYTHON = Path(sys.executable)


# sup.start() 와 install_signal_handlers() 가 끝난 뒤에만 출력된다. 이 줄을 보기 전에
# 시그널을 보내면 핸들러가 없어 기본 동작(0xC000013A STATUS_CONTROL_C_EXIT)으로 죽는다.
READY_MARKER = "실행 중입니다"


def _run_and_interrupt(
    data_dir: Path,
    ready_timeout_s: float = 60.0,
    stopper: "Callable[[subprocess.Popen, Path], None] | None" = None,
) -> tuple[int, str]:
    # PYTHONUNBUFFERED 가 없으면 stdout 이 파이프에 물릴 때 블록 버퍼링돼 기동 완료
    # 표시가 종료 시점까지 나오지 않는다 — 준비를 기다릴 방법이 사라진다.
    env = dict(
        os.environ,
        ARGUS_DATA_DIR=str(data_dir),
        PYTHONIOENCODING="utf-8",
        PYTHONUNBUFFERED="1",
    )
    kwargs: dict = {}
    if sys.platform == "win32":
        # 새 프로세스 그룹이어야 CTRL_BREAK_EVENT 를 이 프로세스만 받는다.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [str(PYTHON), "-m", "argus", "--log-level", "INFO"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )

    # 고정 시간을 자면 안 된다. 첫 실행은 데이터 디렉터리가 비어 있어 캘리브레이션
    # (디스크 벤치)이 돌고, 디스크가 바쁘면 그 시간이 늘어난다. 준비되기 전에 시그널을
    # 보내면 핸들러가 아직 없어 테스트가 무작위로 깨진다 — 실제로 그렇게 깨졌다.
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")))
    reader.daemon = True
    reader.start()

    deadline = time.monotonic() + ready_timeout_s
    while time.monotonic() < deadline:
        if any(READY_MARKER in line for line in lines):
            break
        assert proc.poll() is None, f"프로세스가 준비 전에 종료됐다\n{''.join(lines)}"
        time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError(
            f"{ready_timeout_s}초 안에 기동하지 않았다\n{''.join(lines)}"
        )

    # 자기 계측이 최소 한 번은 기록돼야 아래 행 수 단언이 성립한다.
    time.sleep(2.0)
    assert proc.poll() is None, "프로세스가 시그널 전에 이미 종료됐다"

    if stopper is not None:
        stopper(proc, data_dir)
    elif sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)

    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("시그널을 보낸 뒤 15초 안에 종료하지 않았다")

    reader.join(timeout=5)
    return proc.returncode, "".join(lines)


def test_graceful_shutdown(tmp_path: Path) -> None:
    data_dir = tmp_path / "argusdata"
    code, output = _run_and_interrupt(data_dir)

    assert code == 0, f"종료 코드가 0 이 아니다: {code}\n{output}"
    assert "모든 컴포넌트 종료 완료" in output, f"컴포넌트가 정리되지 않았다\n{output}"

    db_file = data_dir / "argus.db"
    assert db_file.exists(), "DB 파일이 생성되지 않았다"

    # 종료 후 DB 를 정상적으로 열 수 있어야 한다 (잠김·손상 없음)
    conn = sqlite3.connect(str(db_file))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"DB 무결성 실패: {integrity}"
        rows = conn.execute("SELECT COUNT(*) FROM self_telemetry").fetchone()[0]
        assert rows > 0, "자기 계측이 한 행도 기록되지 않았다"
        _assert_shutdown_recorded(conn)
    finally:
        conn.close()


def _assert_shutdown_recorded(conn: sqlite3.Connection) -> None:
    """정상 종료가 `shutdown` 으로 남았는가.

    남지 않으면 다음 기동이 이 세션을 `unclean_shutdown` 으로 판정한다 — 사용자가 곱게
    끈 것이 크래시로 기록되고, 사후 진단의 근거가 오염된다.

    실제로 그렇게 새고 있었다. 종료 이벤트를 큐에 넣던 시절에는 writer 스레드가 같은
    신호를 받고 먼저 끝나 버려, 넣기 전에 flush 가 끝나면 아무도 쓰지 않았다.
    """
    events = [r[0] for r in conn.execute("SELECT event FROM system_events ORDER BY ts")]
    assert "shutdown" in events, f"정상 종료가 기록되지 않았다: {events}"


def test_stop_file_shutdown(tmp_path: Path) -> None:
    """종료 신호 파일로도 같은 경로를 타는가.

    Windows 에는 콘솔 없이 도는 프로세스에 종료를 곱게 전할 방법이 없어 이 경로가
    배포판의 유일한 정상 종료 수단이다.
    """
    data_dir = tmp_path / "argusdata"

    def drop_stop_file(_proc: subprocess.Popen, root: Path) -> None:
        (root / "STOP").write_text("", encoding="utf-8")

    code, output = _run_and_interrupt(data_dir, stopper=drop_stop_file)

    assert code == 0, f"종료 코드가 0 이 아니다: {code}\n{output}"
    assert "모든 컴포넌트 종료 완료" in output, f"컴포넌트가 정리되지 않았다\n{output}"
    # 소비되지 않고 남으면 다음 기동이 뜨자마자 다시 죽는다.
    assert not (data_dir / "STOP").exists(), "종료 신호 파일이 소비되지 않았다"

    conn = sqlite3.connect(str(data_dir / "argus.db"))
    try:
        _assert_shutdown_recorded(conn)
    finally:
        conn.close()


if __name__ == "__main__":  # 스모크: python tests/test_shutdown.py
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        started = time.perf_counter()
        try:
            test_graceful_shutdown(Path(tmp))
        except AssertionError as e:
            print(f"[FAIL] {e}")
            raise SystemExit(1)
        print(f"  소요: {time.perf_counter() - started:.1f}초")
    print("[OK] 정상 종료 (시그널 → 컴포넌트 정리 → DB 무결성 확인)")

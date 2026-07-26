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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # 리눅스/개발자 환경 대비
    PYTHON = Path(sys.executable)


def _run_and_interrupt(data_dir: Path, wait_s: float = 8.0) -> tuple[int, str]:
    env = dict(os.environ, ARGUS_DATA_DIR=str(data_dir), PYTHONIOENCODING="utf-8")
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

    time.sleep(wait_s)  # 자기 계측이 최소 한 번은 기록될 시간을 준다
    assert proc.poll() is None, "프로세스가 시그널 전에 이미 종료됐다"

    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)

    try:
        output, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("시그널을 보낸 뒤 15초 안에 종료하지 않았다")

    return proc.returncode, output


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

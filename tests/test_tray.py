"""트레이와 대시보드 실행 경로.

**GUI 를 띄우지 않는다.** 아이콘과 풍선이 실제로 화면에 보이는지는 사람이 봐야 하고,
마우스를 움직이는 자동화는 쓰지 않는다(CLAUDE.md). 여기서 고정하는 것은 "어떤
프로세스를 어떤 환경으로 띄우는가"이고, 그건 `Popen` 을 가로채면 전부 확인된다.

**왜 이 파일이 생겼나 (2026-08-03)**: 트레이 메뉴의 "대시보드 열기"가 아무 일도 하지
않았다. 상주는 base `pythonw.exe` 로 도는데(`tools/soak_entry.py` — venv 트램폴린이
콘솔 창을 만들기 때문) 그 인터프리터에는 `streamlit` 이 없고, `CREATE_NO_WINDOW` 뒤에서
`ModuleNotFoundError` 가 그대로 묻혔다. **조용한 실패를 내가 만들었다**(규칙 4).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from argus.ui.tray import TrayIcon


class _Recorder:
    """`subprocess.Popen` 대역. 인자와 환경만 기록한다."""

    def __init__(self, *, returncode: int | None = None, stderr: bytes = b"") -> None:
        self.args = None
        self.env = None
        self.returncode = returncode
        self._stderr = stderr

    def __call__(self, args, **kwargs):
        self.args = args
        self.env = kwargs.get("env")
        return self

    def communicate(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="dashboard", timeout=timeout or 0)
        return b"", self._stderr


def test_dashboard_inherits_the_current_interpreter_path(monkeypatch) -> None:
    """**`sys.executable` 만으로는 부족하다.**

    상주가 base 인터프리터로 돌면 그쪽에는 `streamlit` 이 없다. 현재 프로세스의
    `sys.path`(= `soak_entry` 가 venv 를 얹어 둔 것)를 `PYTHONPATH` 로 물려줘야
    자식이 찾을 수 있다. 실측으로 확인했다 — 물려주기 전 `exit 1`, 물려준 뒤 기동.
    """
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    TrayIcon()._open_dashboard()

    assert recorder.args is not None, "대시보드를 띄우지 않았다"
    assert recorder.args[1:] == ["-m", "argus.dashboard"]
    assert recorder.env is not None and recorder.env.get("PYTHONPATH"), (
        "PYTHONPATH 를 물려주지 않았다 — base 인터프리터에서 streamlit 을 못 찾는다"
    )

    import sys

    inherited = recorder.env["PYTHONPATH"].split(os.pathsep)
    meaningful = [p for p in sys.path if p]
    assert set(meaningful) <= set(inherited), "현재 sys.path 가 온전히 전달되지 않았다"


def test_dashboard_is_not_launched_when_frozen(monkeypatch) -> None:
    """exe 에는 대시보드를 아직 묶지 않았다. **조용히 실패하는 대신 말한다**(규칙 4)."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr("argus.paths.is_frozen", lambda: True)

    tray = TrayIcon()
    told: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tray, "notify", lambda t, m, s="warning": told.append((t, m, s)) or True
    )

    tray._open_dashboard()

    assert recorder.args is None, "exe 에 없는 대시보드를 띄우려 했다"
    assert told, "띄우지 못한다는 사실을 사용자에게 알리지 않았다"
    assert "exe" in told[0][1]


def test_immediate_dashboard_death_is_reported(monkeypatch) -> None:
    """**띄운 것과 뜬 것은 다르다.**

    `Popen` 은 즉시 돌아오므로 거기서 성공을 선언하면 실패가 묻힌다. 정확히 그래서
    이 버그가 조용했다.
    """
    proc = _Recorder(returncode=1, stderr="ModuleNotFoundError: streamlit".encode())
    tray = TrayIcon()
    told: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tray, "notify", lambda t, m, s="warning": told.append((t, m, s)) or True
    )

    tray._watch_dashboard(proc)

    assert told, "대시보드가 곧바로 죽었는데 아무 말도 하지 않았다"
    assert "streamlit" in told[0][1], f"실패 이유가 전달되지 않았다: {told[0][1]}"
    assert told[0][2] == "warning"


def test_surviving_dashboard_is_not_reported_as_failure(monkeypatch) -> None:
    """5초를 살아남으면 뜬 것으로 본다. 정상 기동에 경고를 띄우면 안 된다."""
    proc = _Recorder(returncode=None)  # communicate 가 TimeoutExpired 를 낸다
    tray = TrayIcon()
    told: list = []
    monkeypatch.setattr(tray, "notify", lambda *a, **k: told.append(a) or True)

    tray._watch_dashboard(proc)

    assert not told, "정상 기동인데 실패라고 알렸다"


def test_notify_is_a_no_op_without_an_icon() -> None:
    """아이콘 등록에 실패했으면 알림도 없다 — 예외 대신 False 를 돌려준다.

    트레이 실패가 융합을 죽이면 안 되기 때문이다(설계 규칙 1).
    """
    assert TrayIcon().notify("제목", "본문") is False


def test_describe_exposes_failure() -> None:
    """규칙 4 — 조용히 실패하지 않는다. 상태를 읽을 수 있어야 한다."""
    state = TrayIcon().describe()
    assert set(state) == {"active", "error"}
    assert state["active"] == "False"

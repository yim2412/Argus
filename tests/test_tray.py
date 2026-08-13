"""트레이와 대시보드 실행 경로.

**GUI 를 띄우지 않는다.** 아이콘과 풍선이 실제로 화면에 보이는지는 사람이 봐야 하고,
마우스를 움직이는 자동화는 쓰지 않는다(CLAUDE.md). 여기서 고정하는 것은 "어떤
프로세스를 어떤 환경으로 띄우는가"이고, 그건 `Popen` 을 가로채면 전부 확인된다.

**왜 이 파일이 생겼나 (2026-08-03)**: 트레이 메뉴의 "창 열기"가 아무 일도 하지
않았다. 상주는 base `pythonw.exe` 로 도는데(`tools/soak_entry.py` — venv 트램폴린이
콘솔 창을 만들기 때문) 그 인터프리터에는 창이 쓰는 패키지가 없고, `CREATE_NO_WINDOW` 뒤에서
`ModuleNotFoundError` 가 그대로 묻혔다. **조용한 실패를 내가 만들었다**(규칙 4).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from argus.ui import tray as tray_module
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


def test_window_inherits_the_current_interpreter_path(monkeypatch) -> None:
    """**`sys.executable` 만으로는 부족하다.**

    상주가 base 인터프리터로 돌면 그쪽에는 PySide6 가 없다. 현재 프로세스의
    `sys.path`(= `soak_entry` 가 venv 를 얹어 둔 것)를 `PYTHONPATH` 로 물려줘야
    자식이 찾을 수 있다. 실측으로 확인했다 — 물려주기 전 `exit 1`, 물려준 뒤 기동.
    """
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    TrayIcon()._open_dashboard()

    assert recorder.args is not None, "창을 띄우지 않았다"
    assert recorder.args[1:] == ["-m", "argus.desktop.app"], (
        f"네이티브 창이 아니라 다른 것을 띄운다: {recorder.args}"
    )
    assert recorder.env is not None and recorder.env.get("PYTHONPATH"), (
        "PYTHONPATH 를 물려주지 않았다 — base 인터프리터에서 PySide6 를 못 찾는다"
    )

    import sys

    inherited = recorder.env["PYTHONPATH"].split(os.pathsep)
    meaningful = [p for p in sys.path if p]
    assert set(meaningful) <= set(inherited), "현재 sys.path 가 온전히 전달되지 않았다"


def test_frozen_looks_for_the_window_executable(monkeypatch, tmp_path) -> None:
    """**묶인 상태에서는 창이 별도 exe 다.**

    상주와 프로세스를 나눠 창이 죽어도 수집이 계속되게 했으므로, `argus-ui.exe` 를
    찾아 실행한다. 상주 exe 옆이나 `argus-ui/` 하위 둘 다 본다 — 배포 폴더 구조가
    아직 정해지지 않았다.
    """
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr("argus.paths.is_frozen", lambda: True)

    fake_exe = tmp_path / "argus" / "argus.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.touch()
    window_exe = tmp_path / "argus-ui" / "argus-ui.exe"
    window_exe.parent.mkdir(parents=True)
    window_exe.touch()
    monkeypatch.setattr("sys.executable", str(fake_exe))

    TrayIcon()._open_dashboard()

    assert recorder.args == [str(window_exe)], f"창 exe 를 못 찾았다: {recorder.args}"


def test_missing_window_executable_is_reported(monkeypatch, tmp_path) -> None:
    """창 exe 를 함께 배포하지 않은 경우. **조용히 실패하지 않는다**(규칙 4)."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr("argus.paths.is_frozen", lambda: True)
    lonely = tmp_path / "argus.exe"
    lonely.touch()
    monkeypatch.setattr("sys.executable", str(lonely))

    tray = TrayIcon()
    told: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tray, "notify", lambda t, m, s="warning": told.append((t, m, s)) or True
    )

    tray._open_dashboard()

    assert recorder.args is None, "없는 exe 를 띄우려 했다"
    assert told, "띄우지 못한다는 사실을 사용자에게 알리지 않았다"
    assert "argus-ui" in told[0][1]


def test_immediate_dashboard_death_is_reported(monkeypatch) -> None:
    """**띄운 것과 뜬 것은 다르다.**

    `Popen` 은 즉시 돌아오므로 거기서 성공을 선언하면 실패가 묻힌다. 정확히 그래서
    이 버그가 조용했다.
    """
    proc = _Recorder(returncode=1, stderr="ModuleNotFoundError: PySide6".encode())
    tray = TrayIcon()
    told: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tray, "notify", lambda t, m, s="warning": told.append((t, m, s)) or True
    )

    tray._watch_dashboard(proc)

    assert told, "창이 곧바로 죽었는데 아무 말도 하지 않았다"
    assert "PySide6" in told[0][1], f"실패 이유가 전달되지 않았다: {told[0][1]}"
    assert told[0][2] == "warning"


def test_surviving_dashboard_is_not_reported_as_failure(monkeypatch) -> None:
    """5초를 살아남으면 뜬 것으로 본다. 정상 기동에 경고를 띄우면 안 된다."""
    proc = _Recorder(returncode=None)  # communicate 가 TimeoutExpired 를 낸다
    tray = TrayIcon()
    told: list = []
    monkeypatch.setattr(tray, "notify", lambda *a, **k: told.append(a) or True)

    tray._watch_dashboard(proc)

    assert not told, "정상 기동인데 실패라고 알렸다"


def test_start_announcement_is_informational(monkeypatch) -> None:
    """**기동 알림은 `info` 다.**

    탐지가 조용하면 알림도 없어서(실측: 20번 수정 이후 8시간 0건) 그 상태로는 "알림이
    켜져 있는지"를 사용자가 알 수 없다. 기동마다 한 번이면 하루 한두 번이라 소음이
    아니다. 다만 이건 이상이 아니므로 `warning` 으로 올리면 안 된다 — 등급이 뜻을
    잃는다.
    """
    tray = TrayIcon()
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        tray, "notify", lambda t, m, s="warning": sent.append((t, m, s)) or True
    )

    assert tray.announce_start() is True
    assert len(sent) == 1
    title, message, severity = sent[0]
    assert severity == "info", f"기동 알림이 {severity} 다 — 이상이 아닌데 등급이 높다"
    assert "Argus" in title and message


class _FakeShell:
    """`win32gui.Shell_NotifyIcon` 대역. 실제 풍선을 띄우지 않고 호출만 센다."""

    NIM_MODIFY = 1
    NIF_INFO = 0x10

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def Shell_NotifyIcon(self, action, payload):  # noqa: N802 - win32 이름을 흉내낸다
        self.calls.append((action, payload))


def _armed_tray(monkeypatch) -> tuple[TrayIcon, _FakeShell]:
    """아이콘이 등록된 상태의 트레이. 발송 직전까지 진짜 경로를 탄다."""
    import sys

    shell = _FakeShell()
    monkeypatch.setitem(sys.modules, "win32gui", shell)
    tray = TrayIcon()
    tray._added = True
    tray._hwnd = 1
    tray._hicon = 1
    return tray, shell


def _info_flags(shell: _FakeShell) -> int:
    """마지막 풍선의 `dwInfoFlags`. `Shell_NotifyIcon` 페이로드의 마지막 칸이다."""
    return shell.calls[-1][1][9]


def test_balloon_is_silent_by_default(monkeypatch) -> None:
    """**기본은 무음이다.** 상주의 알림은 사용자가 부른 것이 아니라 끼어드는 것이라,
    소리까지 나면 오탐 한 번의 비용이 훨씬 커진다(탐지 규칙 1).
    """
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.delenv("ARGUS_NO_NOTIFY", raising=False)

    assert tray.notify("제목", "본문") is True
    assert _info_flags(shell) & tray_module._NIIF_NOSOUND, "NIIF_NOSOUND 없이 띄웠다 — 소리가 난다"


def test_balloon_keeps_the_severity_icon_while_silencing(monkeypatch) -> None:
    """소리를 끄면서 등급 아이콘까지 지우면 안 된다. 플래그를 덮어쓰면 여기서 걸린다."""
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.delenv("ARGUS_NO_NOTIFY", raising=False)

    tray.notify("제목", "본문", severity="critical")
    assert _info_flags(shell) & 0x03 == 0x03, "NIIF_ERROR 아이콘이 사라졌다"


def test_balloon_makes_a_sound_when_asked(monkeypatch) -> None:
    """반대쪽. 이게 없으면 "항상 무음"으로 못 박아도 위 테스트가 통과한다."""
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.delenv("ARGUS_NO_NOTIFY", raising=False)
    tray.notify_sound = True

    tray.notify("제목", "본문")
    assert not (_info_flags(shell) & tray_module._NIIF_NOSOUND), "소리를 켰는데 무음 플래그가 붙었다"


def test_notify_sound_setting_reaches_the_tray() -> None:
    """**배선을 로직과 따로 잰다.** 위 테스트들은 판정만 보고 `general.notify_sound` 가
    실제로 트레이에 닿는지는 보지 않는다 — 조립부에서 인자를 빠뜨려도 전부 통과한다.

    코드 기본값과 YAML 기본값이 둘 다 `False` 라 **기본값이 아닌 값으로 잰다**
    (2026-08-04 에 같은 유형의 구멍이 네 번 나왔다).
    """
    import ast
    import inspect

    import argus.__main__ as main_module

    tree = ast.parse(inspect.getsource(main_module))
    wired = [
        kw
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "TrayIcon"
        for kw in node.keywords
        if kw.arg == "notify_sound"
    ]
    assert wired, "상주가 TrayIcon 에 notify_sound 를 넘기지 않는다 — YAML 을 고쳐도 안 바뀐다"
    assert ast.unparse(wired[0].value) == "settings.general.notify_sound"

    # 그리고 그 설정이 YAML 에서 실제로 읽히는가 — 기본값(False)이 아닌 값으로.
    from argus.config.loader import Settings

    assert Settings.model_validate({"general": {"notify_sound": True}}).general.notify_sound is True


def test_suppression_stops_the_balloon_from_reaching_the_screen(monkeypatch) -> None:
    """**알림은 `ARGUS_DATA_DIR` 로 격리되지 않는다 — 화면은 하나뿐이다.**

    2026-08-06 에 `test_shutdown` 이 진짜 상주를 8번 띄우면서 "Argus 감시 시작" 이
    사용자 화면에 8번 떴다. 데이터는 임시 폴더로 갔지만 풍선은 그대로 갔다.
    """
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.setenv("ARGUS_NO_NOTIFY", "1")

    assert tray.notify("제목", "본문") is False
    assert not shell.calls, "억제 중인데 Shell_NotifyIcon 을 불렀다"


def test_suppression_is_off_by_default(monkeypatch) -> None:
    """**꺼짐이 기본이어야 한다.** 억제가 기본이면 제품이 조용히 알림 없는 앱이 된다.

    위 테스트만 있으면 "항상 억제"로 만들어도 통과한다 — 반대쪽을 같이 재야 한다.
    """
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.delenv("ARGUS_NO_NOTIFY", raising=False)

    assert tray.notify("제목", "본문") is True
    assert shell.calls, "억제가 꺼져 있는데 발송하지 않았다"


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_falsy_values_do_not_suppress(monkeypatch, value) -> None:
    """`ARGUS_NO_NOTIFY=0` 은 **끄는 것**이다.

    "설정됨"만 보면 정반대로 동작하고, 그 실패는 조용하다 — 알림이 원래 드물어서
    안 오는 것과 구분되지 않는다.
    """
    tray, shell = _armed_tray(monkeypatch)
    monkeypatch.setenv("ARGUS_NO_NOTIFY", value)

    assert tray.notify("제목", "본문") is True, f"{value!r} 를 억제로 읽었다"
    assert shell.calls


def test_describe_reveals_suppression(monkeypatch) -> None:
    """규칙 4 — 억제 중이면 그 사실이 보여야 한다.

    설정이 켜져 있는데 아무것도 안 뜨는 상태이므로, 안 드러나면 "알림을 켰는데 왜
    안 오지"의 답을 찾을 방법이 없다.
    """
    monkeypatch.setenv("ARGUS_NO_NOTIFY", "1")
    assert TrayIcon().describe()["suppressed"] == "True"

    monkeypatch.delenv("ARGUS_NO_NOTIFY", raising=False)
    assert TrayIcon().describe()["suppressed"] == "False"


def test_notify_is_a_no_op_without_an_icon() -> None:
    """아이콘 등록에 실패했으면 알림도 없다 — 예외 대신 False 를 돌려준다.

    트레이 실패가 융합을 죽이면 안 되기 때문이다(설계 규칙 1).
    """
    assert TrayIcon().notify("제목", "본문") is False


def test_describe_exposes_failure() -> None:
    """규칙 4 — 조용히 실패하지 않는다. 상태를 읽을 수 있어야 한다."""
    state = TrayIcon().describe()
    assert set(state) == {"active", "icon", "notify", "suppressed", "error"}
    assert state["active"] == "False"


# ---------------------------------------------------------------- 알림 → 사건
#
# **알림을 눌러도 갈 곳이 없으면 평가는 일어나지 않는다.** 14일 동안 사건 158건 ·
# 알림 11건에 피드백이 **0건**이었다. 창을 열고 목록에서 그 시각을 손으로 찾아야
# 했기 때문이고, 라벨이 없으니 "이 알림이 맞았나"에 답할 근거도 없었다.


def test_balloon_click_opens_that_incident(monkeypatch) -> None:
    """풍선을 누르면 **그 사건**이 열려야 한다. 목록 첫 줄이 아니라."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    tray = TrayIcon()
    tray._balloon_incident = 156

    from argus.ui import tray as tray_module

    tray._on_message(0, tray_module._WM_TRAY, 0, tray_module._NIN_BALLOONUSERCLICK)

    assert recorder.args is not None, "풍선을 눌렀는데 창이 뜨지 않았다"
    assert recorder.args[-2:] == ["--incident", "156"], (
        f"어느 사건인지 전달되지 않았다: {recorder.args}"
    )


def test_menu_open_does_not_pin_an_incident(monkeypatch) -> None:
    """메뉴로 연 창은 평소처럼 열려야 한다 — 지난 알림에 묶이면 안 된다."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    tray = TrayIcon()
    tray._balloon_incident = 156
    tray._open_dashboard()

    assert recorder.args is not None
    assert "--incident" not in recorder.args, f"메뉴로 열었는데 사건이 붙었다: {recorder.args}"


def test_notify_remembers_which_incident_the_balloon_shows(monkeypatch) -> None:
    """**Windows 는 클릭 알림에 아무것도 실어 주지 않는다.**

    띄우는 시점에 붙잡아 두지 않으면 눌렸을 때 어디로 갈지 알 방법이 없다.

    `_added=True` 로 두는 이유: 아이콘이 없으면 풍선 자체가 안 뜨므로 기억할 것도
    없다. 실제 `Shell_NotifyIcon` 은 hwnd 가 0 이라 실패하고, 그 실패는 이미
    `notify` 안에서 삼켜지지 않고 로그로 남는다(다른 테스트가 그걸 본다).
    """
    from argus.ui import tray as tray_module

    monkeypatch.setattr(tray_module, "notifications_suppressed", lambda: False)
    tray = TrayIcon()
    tray._added = True

    tray.notify("제목", "내용", "warning", incident_id=42)

    assert tray._balloon_incident == 42

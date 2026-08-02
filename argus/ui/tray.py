"""트레이 아이콘과 풍선 알림.

**의존성을 늘리지 않는다.** `pywin32` 는 PDH 때문에 이미 필수라, `Shell_NotifyIcon` 을
직접 쓰면 아이콘과 알림을 **한 API 로** 해결한다. pystray(+Pillow)나 win10toast 는
둘 중 하나만 주면서 의존성을 늘리고, WinRT 토스트는 AUMID 앱 등록이 필요해 배포 절차가
늘어난다.

**세 가지가 이 모듈의 제약이다.**

1. **창 메시지는 창을 만든 스레드에서만 처리된다.** 그래서 `setup`(생성)·`tick`(펌프)·
   `teardown`(제거)이 모두 한 스레드에서 도는 `Component` 규약에 얹는다. 펌프를
   `GetMessage` 로 하면 블로킹되어 `tick` 이 돌아오지 않으므로 `PeekMessage` 를 쓴다.

2. **트레이가 실패해도 수집은 계속돼야 한다** (설계 규칙 1 — 모니터가 병목이 되면 실패다).
   아이콘 생성 실패는 예외로 올리지 않고 비활성화 상태로 내려간다. 대신 **조용히 죽지
   않는다**(규칙 4) — 경고를 남기고 `describe()` 로 상태를 드러낸다.

3. **종료할 때 아이콘을 반드시 지운다.** 안 지우면 죽은 아이콘이 트레이에 남아, 사용자가
   마우스를 올려야 사라진다. 프로그램이 끝났는데 흔적이 남는 것은 상주 프로그램의 기본
   예의 문제다.

**알림은 등급이 warning 이상일 때만 온다.** 그 판단은 여기서 하지 않는다 —
`decide/budget.py` 가 예산·심각도 컷을 이미 정하고, 이 모듈은 전달만 맡는다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from ..logging_setup import get_logger
from ..runtime.supervisor import Component

log = get_logger(__name__)

# 풍선 알림 아이콘. 등급을 시각적으로 구분한다.
_INFO_FLAGS = {"info": 0x01, "warning": 0x02, "critical": 0x03}  # NIIF_INFO/WARNING/ERROR

_WM_TRAY = 0x0400 + 20  # WM_APP + 20. 트레이 아이콘이 보내는 콜백 메시지
_MENU_DASHBOARD = 1001
_MENU_STOP = 1002

# 풍선이 떠 있는 시간(ms). Windows 10+ 는 이 값을 무시하고 시스템 설정을 따르지만,
# 값 자체는 API 가 요구한다.
_BALLOON_TIMEOUT_MS = 10_000


@dataclass
class TrayIcon(Component):
    """트레이 아이콘. 알림 전달 창구이기도 하다."""

    name: str = "tray"
    # 메시지 펌프 주기. 사용자 클릭에 반응해야 하므로 짧지만, 하는 일이 거의 없어
    # 비용은 무시할 수준이다(PeekMessage 는 큐가 비면 즉시 돌아온다).
    interval_s: float = 0.2
    tooltip: str = "Argus — PC 성능 모니터"

    on_stop: object = None
    """종료 메뉴를 눌렀을 때 부를 것. `Supervisor.request_stop` 을 넣는다."""

    _hwnd: int = 0
    _hicon: int = 0
    _added: bool = False
    _failed: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ 생명주기

    def setup(self) -> None:
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError as exc:
            self._failed = f"pywin32 없음: {exc}"
            log.warning("트레이를 켤 수 없다 — 수집은 계속한다", extra={"error": self._failed})
            return

        try:
            klass = win32gui.WNDCLASS()
            klass.hInstance = win32api.GetModuleHandle(None)
            # 클래스 이름이 겹치면 등록이 실패한다. 한 프로세스에 하나만 뜨므로 고정해도
            # 되지만, 재등록 실패를 피하려고 이미 있으면 그대로 쓴다.
            klass.lpszClassName = "ArgusTrayWindow"
            klass.lpfnWndProc = self._on_message
            try:
                win32gui.RegisterClass(klass)
            except Exception:
                pass  # 이미 등록됨 (재시작 경로)

            self._hwnd = win32gui.CreateWindow(
                klass.lpszClassName, "Argus", 0, 0, 0, 0, 0, 0, 0, klass.hInstance, None
            )
            win32gui.UpdateWindow(self._hwnd)

            # 전용 아이콘 파일이 아직 없다. 시스템 정보 아이콘을 쓰면 exe 에 자원을
            # 넣지 않아도 되고, 아이콘이 없다고 트레이를 통째로 포기하지 않아도 된다.
            self._hicon = win32gui.LoadIcon(0, win32con.IDI_INFORMATION)

            flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD,
                (self._hwnd, 0, flags, _WM_TRAY, self._hicon, self.tooltip),
            )
            self._added = True
            log.info("트레이 아이콘 등록")
        except Exception as exc:  # 트레이 실패가 수집을 죽이지 않는다
            self._failed = str(exc)
            log.warning("트레이 아이콘 등록 실패 — 수집은 계속한다", extra={"error": self._failed})

    def tick(self) -> None:
        """메시지 펌프. **블로킹하지 않는다.**"""
        if not self._hwnd:
            return
        import win32gui

        win32gui.PumpWaitingMessages()

    def teardown(self) -> None:
        if not self._added:
            return
        try:
            import win32gui

            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, 0))
            self._added = False
            log.info("트레이 아이콘 제거")
        except Exception as exc:
            # 여기서 실패해도 할 수 있는 일이 없다. 프로세스가 끝나면 어차피 사라진다.
            log.warning("트레이 아이콘 제거 실패", extra={"error": str(exc)})

    # ------------------------------------------------------------------ 알림

    def notify(self, title: str, message: str, severity: str = "warning") -> bool:
        """풍선 알림을 띄운다. 띄웠으면 True.

        **보낼지 말지는 여기서 정하지 않는다** — `decide/budget.py` 가 예산과 심각도 컷을
        이미 판단한다. 이 함수는 전달만 하고, 전달 실패를 삼키지 않는다.
        """
        if not self._added:
            return False

        with self._lock:
            try:
                import win32gui

                win32gui.Shell_NotifyIcon(
                    win32gui.NIM_MODIFY,
                    (
                        self._hwnd,
                        0,
                        win32gui.NIF_INFO,
                        _WM_TRAY,
                        self._hicon,
                        self.tooltip,
                        message,
                        _BALLOON_TIMEOUT_MS,
                        title,
                        _INFO_FLAGS.get(severity, 0x01),
                    ),
                )
                return True
            except Exception as exc:
                log.warning(
                    "알림 표시 실패", extra={"error": str(exc), "title": title}
                )
                return False

    # ------------------------------------------------------------------ 내부

    def _on_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        import win32con
        import win32gui

        if msg == _WM_TRAY and lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONDBLCLK):
            self._show_menu(hwnd)
            return 0
        if msg == win32con.WM_COMMAND:
            self._on_command(win32gui.LOWORD(wparam) if hasattr(win32gui, "LOWORD") else wparam & 0xFFFF)
            return 0
        if msg == win32con.WM_DESTROY:
            self.teardown()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        """우클릭 메뉴. **종료 항목이 없으면 사용자가 끌 방법이 STOP 파일뿐이다.**"""
        import win32con
        import win32gui

        try:
            menu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(menu, win32con.MF_STRING, _MENU_DASHBOARD, "대시보드 열기")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, _MENU_STOP, "Argus 종료")

            x, y = win32gui.GetCursorPos()
            # 메뉴가 뜬 채로 다른 곳을 누르면 닫히도록 포그라운드를 잡아 준다.
            # 이게 없으면 메뉴가 화면에 남는다(Win32 의 알려진 요구사항).
            win32gui.SetForegroundWindow(hwnd)
            win32gui.TrackPopupMenu(
                menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON, x, y, 0, hwnd, None
            )
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)
            win32gui.DestroyMenu(menu)
        except Exception as exc:
            log.warning("트레이 메뉴 실패", extra={"error": str(exc)})

    def _on_command(self, command_id: int) -> None:
        if command_id == _MENU_STOP:
            log.info("트레이에서 종료 요청")
            if callable(self.on_stop):
                self.on_stop()
        elif command_id == _MENU_DASHBOARD:
            self._open_dashboard()

    def _open_dashboard(self) -> None:
        """대시보드를 별도 프로세스로 띄운다.

        **상주 프로세스 안에서 Streamlit 을 돌리지 않는다.** 자체 서버와 이벤트 루프를
        갖고 있어 상주 스레드 모델과 섞이면 종료가 지저분해지고, 무엇보다 대시보드가
        죽을 때 수집까지 끌고 내려간다.
        """
        import subprocess
        import sys

        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [sys.executable, "-m", "argus.dashboard"],
                creationflags=creationflags,
            )
            log.info("대시보드 실행 요청")
        except Exception as exc:
            log.warning("대시보드를 띄우지 못했다", extra={"error": str(exc)})

    def describe(self) -> dict[str, str]:
        """규칙 4 — 조용히 실패하지 않는다. 상태를 드러낸다."""
        return {
            "active": str(self._added),
            "error": self._failed or "-",
        }


if __name__ == "__main__":  # 스모크: python -m argus.ui.tray
    import time

    from ..logging_setup import setup

    setup(level="INFO")
    tray = TrayIcon()
    tray.setup()
    state = tray.describe()
    print(f"  아이콘 등록 = {state['active']}  (오류: {state['error']})")

    if tray._added:
        # 실제로 풍선이 떠야 확인되는 것이라 사람이 봐야 한다. 자동화하지 않는다.
        print("  풍선 알림 표시 =", tray.notify("Argus", "트레이 스모크입니다.", "info"))
        deadline = time.time() + 3
        while time.time() < deadline:
            tray.tick()
            time.sleep(0.05)

    tray.teardown()
    print("[OK] ui.tray" if state["active"] == "True" else "[FAIL] ui.tray")

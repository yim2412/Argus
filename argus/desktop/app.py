"""창 골격. **페이지를 담기만 하고 데이터는 모른다.**

Streamlit 판은 페이지마다 파일 하나였고 사이드바가 그것을 묶었다. 여기서는 왼쪽
탭 목록이 그 역할을 한다 — 페이지가 다섯이 될 예정이라 지금 자리를 잡아 둔다.

**상주 프로세스와 별개다.** 여기에 Qt 이벤트 루프가 있고 상주는 자기 수퍼바이저를
쓴다. 한 프로세스에 넣으면 메인 스레드를 다투고, 창이 죽을 때 수집까지 끌고
내려간다 — 관측자가 병목이 되면 안 된다는 설계 규칙 1 이 여기에도 적용된다.
"""

from __future__ import annotations

import os
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from ..branding import APP_TITLE, set_app_id
from ..dashboard import theme
from ..paths import icon_path
from .pages.incidents import IncidentPage
from .pages.processes import ProcessPage
from .pages.realtime import RealtimePage
from .pages.selfstate import SelfStatePage
from .pages.settings import SettingsPage
from .pages.timeline import TimelinePage

# 개발 중 창을 띄울 모니터(0-기반). 배포 exe 에는 영향이 없다 — 값이 없으면
# Windows 가 정하는 기본 위치를 그대로 쓴다.
ENV_SCREEN = "ARGUS_UI_SCREEN"


def place_on_configured_screen(window: QtWidgets.QWidget) -> str:
    """`ARGUS_UI_SCREEN` 이 가리키는 모니터로 창을 옮긴다.

    **개발 중 창이 작업 중인 화면을 덮으면 안 된다**(CLAUDE.md 검증 절). 게임이나
    작업이 1번 모니터에서 돌고 있을 때 테스트 창이 그 위에 뜨면 그 자체가 방해다.

    지정이 없거나 그런 모니터가 없으면 조용히 기본 위치를 쓴다 — 개발 편의 기능이
    실행을 막으면 안 된다.
    """
    raw = os.environ.get(ENV_SCREEN, "").strip()
    if not raw:
        return "기본 위치"
    try:
        index = int(raw)
    except ValueError:
        return f"기본 위치 ({ENV_SCREEN}={raw!r} 를 못 읽었다)"

    screens = QtWidgets.QApplication.screens()
    if not 0 <= index < len(screens):
        return f"기본 위치 (모니터 {index} 없음, 총 {len(screens)}대)"

    geometry = screens[index].availableGeometry()
    window.move(geometry.center() - window.rect().center())
    return f"모니터 {index} ({screens[index].name()})"


def apply_theme(app: QtWidgets.QApplication) -> None:
    """앱 전체 색과 테두리.

    **함수로 빼 둔 이유가 있다.** 창을 띄우는 경로가 둘이다(`main()` 과
    `tools/ui_snapshot.py`). 스타일이 한쪽에만 붙으면 **캡처가 실제와 다른 화면을
    찍고**, 그 그림을 보고 없는 문제를 고치게 된다 — 2026-08-06 에 실제로 그럴 뻔했다.

    **`QFrame` 에 테두리를 주면 안 된다.** `QLabel` 이 `QFrame` 을 상속하므로 화면의
    모든 글자에 테두리와 둥근 모서리가 붙고, 그만큼 높이를 먹어 큰 수치가 잘린다.
    카드로 쓰는 것만 골라 준다.
    """
    app.setStyleSheet(
        f"QMainWindow, QWidget {{ background: {theme.PAGE}; color: {theme.INK}; }}"
        # 카드는 이 둘뿐이다. QFrame 전체를 잡으면 라벨까지 딸려 온다.
        f"StatTile, QFrame#card {{ background: {theme.SURFACE};"
        f" border: 1px solid {theme.GRID}; border-radius: 8px; }}"
        f"QLabel {{ background: transparent; border: none; }}"
        f"QStatusBar {{ color: {theme.INK_MUTED}; }}"
    )


# 페이지 하나가 스크롤 없이 다 보이는 크기. 차트·표의 최소 높이를 더한 값에서 나왔고,
# 자기 상태 페이지(가장 빽빽하다)가 기준이다.
_WANTED_W, _WANTED_H = 1420, 960


def _initial_size() -> tuple[int, int]:
    """창 기본 크기. **화면보다 크게 열지 않는다.**

    원하는 크기를 고정하면 1366x768 같은 화면에서 창이 화면 밖으로 나가고, 그러면
    스크롤조차 못 한다. 하드웨어를 가정하지 않는다(설계 규칙 2).
    """
    screen = QtWidgets.QApplication.primaryScreen()
    if screen is None:
        return _WANTED_W, _WANTED_H
    available = screen.availableGeometry()
    return (
        min(_WANTED_W, int(available.width() * 0.92)),
        min(_WANTED_H, int(available.height() * 0.92)),
    )


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Argus")
        self.resize(*_initial_size())

        self.realtime = RealtimePage()
        self.processes = ProcessPage()
        self.incidents = IncidentPage()
        self.timeline = TimelinePage()
        self.selfstate = SelfStatePage()
        self.settings = SettingsPage()

        # 왼쪽 탭 + 오른쪽 내용. 페이지가 늘어도 여기만 추가하면 된다.
        self._nav = QtWidgets.QListWidget()
        self._nav.setFixedWidth(150)
        self._nav.setStyleSheet(
            f"QListWidget {{ background: {theme.SURFACE}; border: none;"
            f" color: {theme.INK_SECONDARY}; font-size: 13px; padding: 8px 0; }}"
            f"QListWidget::item {{ padding: 10px 14px; }}"
            f"QListWidget::item:selected {{ background: {theme.GRID}; color: {theme.INK}; }}"
        )
        self._stack = QtWidgets.QStackedWidget()

        self._add_page("실시간", self.realtime)
        self._add_page("프로세스", self.processes)
        self._add_page("타임라인", self.timeline)
        self._add_page("사건", self.incidents)
        self._add_page("자기 상태", self.selfstate)
        self._add_page("설정", self.settings)

        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._nav.setCurrentRow(0)

        body = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._nav)

        content = QtWidgets.QWidget()
        content_box = QtWidgets.QVBoxLayout(content)
        content_box.setContentsMargins(16, 16, 16, 8)
        content_box.addWidget(self._stack)
        row.addWidget(content, stretch=1)

        self.setCentralWidget(body)
        self.statusBar().showMessage("연결 중…")

        # 상태 표시줄은 1초마다 갱신한다. 데이터 조회가 아니라 이미 받은 값을 읽는
        # 것뿐이라 UI 스레드에서 해도 된다.
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._refresh_status)
        self._clock.start(1000)

    def _add_page(self, name: str, widget: QtWidgets.QWidget) -> None:
        self._nav.addItem(name)
        self._stack.addWidget(widget)

    def _refresh_status(self) -> None:
        self.statusBar().showMessage(self.realtime.status_text())

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.realtime.stop()
        self.processes.stop()
        self.incidents.stop()
        self.timeline.stop()
        self.selfstate.stop()
        super().closeEvent(event)


class _Placeholder(QtWidgets.QWidget):
    """아직 이식하지 않은 페이지. **빈 화면 대신 어디서 볼 수 있는지 말한다.**"""

    def __init__(self, name: str) -> None:
        super().__init__()
        box = QtWidgets.QVBoxLayout(self)
        label = QtWidgets.QLabel(
            f"{name} 페이지는 아직 옮기는 중입니다.\n"
            "지금은 `python -m argus.dashboard` 에서 볼 수 있습니다."
        )
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 13px;")
        box.addWidget(label)


def main(seconds: float | None = None) -> int:
    """`seconds` 를 주면 그만큼 돌고 스스로 닫는다.

    **GUI 검증을 마우스로 하지 않기 위한 것이다**(CLAUDE.md). 창이 떴는지·갱신이
    도는지를 사람이 클릭해서 확인하는 대신, 정해진 시간 동안 받은 표본 수를 숫자로
    돌려준다. 0 이면 조회나 시그널 경계가 깨진 것이다.
    """
    # **`QApplication` 보다 먼저다.** Qt 가 첫 창을 만들 때 Windows 가 이 값을 읽어
    # 작업표시줄 그룹을 정하므로, 뒤에 부르면 파이썬 아이콘으로 굳는다.
    set_app_id()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    # 창·작업표시줄·Alt+Tab 이 전부 이 아이콘을 쓴다. 파일이 없으면 Qt 가 빈 아이콘을
    # 받아 기본값으로 돌아갈 뿐이라 창은 그대로 뜬다.
    icon = icon_path()
    if icon.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon)))
    apply_theme(app)

    window = MainWindow()
    placement = place_on_configured_screen(window)
    window.show()
    print(f"  창 위치: {placement}")

    if seconds:
        QtCore.QTimer.singleShot(int(seconds * 1000), app.quit)

    code = app.exec()

    if seconds:
        live = window.realtime.sample_count
        backfill = window.realtime.backfill_count
        # 기대치는 "초당 한 점"이다. 창을 여는 데 드는 시간이 있으므로 여유를 둔다.
        expected = max(1, int(seconds * 0.6))
        loads = window.processes.load_count
        print(f"  백필 {backfill}개 · 실시간 {live}개 ({seconds:.0f}초, 기대 {expected}개 이상)")
        print(f"  프로세스 표 갱신 {loads}회 · 사건 조회 {window.incidents.load_count}회")
        print(f"  타임라인 조회 {window.timeline.load_count}회 · 오버레이 {window.timeline.overlay_count}개")
        print(f"  자기 상태 조회 {window.selfstate.load_count}회")
        if live == 0:
            print("[FAIL] 실시간 표본이 하나도 없다 — 조회나 시그널 경계가 깨졌다")
            return 1
        if live < expected:
            print("[FAIL] 실시간 갱신이 초당 한 점에 못 미친다")
            return 1
        if loads == 0:
            print("[FAIL] 프로세스 표를 한 번도 못 채웠다")
            return 1
        print("[OK] desktop.app")
    return code


def cli() -> int:
    """명령줄 진입점. **exe 진입점도 이걸 부른다.**

    `main()` 을 직접 부르면 인자가 전달되지 않아, exe 에서는 검증용 `--seconds` 를
    쓸 수 없다. 소스 실행과 exe 실행이 다른 경로를 타면 "개발에서는 되는데"가 생긴다.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Argus 데스크톱 창")
    parser.add_argument(
        "--seconds", type=float, default=None, help="이만큼 돌고 자동 종료 (검증용)"
    )
    args, _qt_args = parser.parse_known_args()
    return main(args.seconds)


if __name__ == "__main__":
    raise SystemExit(cli())

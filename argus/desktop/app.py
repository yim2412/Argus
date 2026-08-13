"""창 골격. **페이지를 담기만 하고 데이터는 모른다.**

왼쪽 탭 목록이 페이지를 묶는다. 페이지가 늘어도 `_add_page` 한 줄만 는다.

**상주 프로세스와 별개다.** 여기에 Qt 이벤트 루프가 있고 상주는 자기 수퍼바이저를
쓴다. 한 프로세스에 넣으면 메인 스레드를 다투고, 창이 죽을 때 수집까지 끌고
내려간다 — 관측자가 병목이 되면 안 된다는 설계 규칙 1 이 여기에도 적용된다.
"""

from __future__ import annotations

import json
import os
import sys
import time

from PySide6 import QtCore, QtGui, QtWidgets

from ..branding import APP_TITLE, set_app_id
from ..dashboard import data, theme
from ..paths import icon_path, is_frozen, window_state_path
from .pages.incidents import IncidentPage
from .pages.processes import ProcessPage
from .pages.realtime import RealtimePage
from .pages.report import ReportPage
from .pages.selfstate import SelfStatePage
from .pages.settings import SettingsPage
from .pages.timeline import TimelinePage
from .pages.usage import UsagePage

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


# HD(1280x720). 사용자가 정한 기본 크기다 — **이 크기에 다 들어간다는 뜻이 아니다.**
# 원래 값(1420x960)이 "자기 상태 페이지가 스크롤 없이 다 보이는 크기"였으므로 그 페이지는
# 이제 아래쪽이 잘린다. 창은 자유롭게 키울 수 있으니(고정이 아니라 기본값) 잘리면 늘린다.
_WANTED_W, _WANTED_H = 1280, 720


#: 이보다 작게 저장된 창은 무시한다. 최소화·복원 중에 잡힌 값이거나 파일이 깨진
#: 것이고, 그대로 열면 사용자는 **아무것도 안 보이는 창**을 받는다.
_MIN_SAVED_W, _MIN_SAVED_H = 640, 400


def _initial_size() -> tuple[int, int]:
    """창 기본 크기. **화면보다 크게 열지 않는다.**

    원하는 크기를 고정하면 1366x768 같은 화면에서 창이 화면 밖으로 나가고, 그러면
    스크롤조차 못 한다. 하드웨어를 가정하지 않는다(설계 규칙 2).

    **지난번에 닫은 크기가 있으면 그것이 이긴다.** 사용자가 손으로 맞춘 크기가
    코드 기본값보다 정확한 의도다. 다만 화면 상한은 저장값에도 그대로 걸린다 —
    모니터를 바꾸거나 해상도를 낮추면 저장된 크기가 화면보다 클 수 있다.
    """
    width, height = _WANTED_W, _WANTED_H
    saved = load_window_state()
    if saved:
        width, height = saved["width"], saved["height"]

    screen = QtWidgets.QApplication.primaryScreen()
    if screen is None:
        return width, height
    available = screen.availableGeometry()
    return (min(width, available.width()), min(height, available.height()))


def load_window_state() -> dict | None:
    """지난번에 닫은 창 크기. 없거나 못 믿을 값이면 `None`.

    **읽기가 실패해도 창은 떠야 한다.** 편의 기능이 실행을 막으면 안 된다 —
    `ARGUS_UI_SCREEN` 폴백과 같은 원칙이다.
    """
    path = window_state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        width, height = int(state["width"]), int(state["height"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if width < _MIN_SAVED_W or height < _MIN_SAVED_H:
        return None
    return {"width": width, "height": height}


def save_window_state(width: int, height: int) -> None:
    """창을 닫을 때 크기를 남긴다. **실패는 조용히 넘긴다** — 저장이 안 됐다고
    종료를 막으면, 사용자는 창이 안 닫히는 것으로 겪는다."""
    if width < _MIN_SAVED_W or height < _MIN_SAVED_H:
        return  # 최소화 상태에서 닫힌 것. 그 값을 남기면 다음에 못 쓰는 창이 뜬다
    try:
        window_state_path().write_text(
            json.dumps({"width": int(width), "height": int(height)}), encoding="utf-8"
        )
    except OSError:
        pass


def first_run_notice() -> str | None:
    """DB 가 없을 때 사람이 읽는 안내. 있으면 `None`.

    **"데이터 없음"과 "고장남"을 구분해 준다**(설계 규칙 4). 페이지마다 "…기다리는 중"만
    띄우면 처음 켠 사용자는 무엇이 잘못됐는지 알 수 없다 — 상주를 아직 안 켠 것인지,
    켰는데 안 되는 것인지, 창이 엉뚱한 곳을 보고 있는 것인지가 전부 같은 화면이다.
    그래서 **찾은 경로를 함께 보여 준다** — 셋을 가르는 정보가 그것뿐이다.

    시작 방법은 **실행 형태에 따라 다르다.** 배포판 사용자에게 `python -m argus` 는
    실행할 수 없는 명령이다.
    """
    path = data.db_path()
    if path.exists():
        return None
    how = "argus.exe 를 실행하세요" if is_frozen() else "`python -m argus` 로 시작하세요"
    return f"수집이 아직 시작되지 않았습니다 — {how}.\n찾은 위치: {path}"


class _Banner(QtWidgets.QLabel):
    """페이지 위에 걸리는 한 줄 안내. **어느 페이지를 보든 보여야 한다.**"""

    def __init__(self) -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.INK};"
            f" border: 1px solid {theme.STATUS['warning']}; border-radius: 6px;"
            f" padding: 10px 14px; font-size: 12px;"
        )
        self.hide()

    def update_text(self, text: str | None) -> None:
        """안내가 사라지는 경우도 있다 — 창을 열어 둔 채 상주를 켜면 DB 가 생긴다."""
        if text is None:
            self.hide()
            return
        self.setText(text)
        self.show()


#: 마지막 표본이 이보다 오래됐으면 수집이 멈춘 것으로 본다(초). 수집 주기는 1초지만
#: 스로틀이 걸리면 ×10 까지 늦춰지므로(`runtime/budget`) 그보다 넉넉해야 한다 —
#: 스로틀은 정상 동작이고, 그때마다 "수집 멈춤"이 뜨면 그것이 오탐이다.
STALE_SAMPLE_S = 60.0


class _HealthPoller(QtCore.QThread):
    """맨 윗줄이 쓸 답 하나를 5초마다 물어 온다.

    **UI 스레드에서 DB 를 읽지 않는다**(다른 페이지와 같은 규칙). 조회 자체는
    가볍지만, 성능 모니터가 자기 창에서 버벅이는 것보다 나쁜 인상은 없다.
    """

    loaded = QtCore.Signal(dict)

    def __init__(self, interval_s: float = 5.0) -> None:
        super().__init__()
        self._interval_s = interval_s
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                self.loaded.emit(data.health())
            except Exception:
                # DB 가 아직 없거나 잠깐 잠겼을 뿐이다. 맨 윗줄 하나 때문에 창이
                # 죽으면 안 된다 — 다음 주기에 다시 묻는다.
                pass
            self.msleep(int(self._interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class _StatusLine(QtWidgets.QFrame):
    """**"지금 괜찮은가" 한 줄.** 어느 탭을 보든 맨 위에 있다.

    이 줄이 이 창의 답이다. 그전까지는 사용자가 수치 다섯 개와 차트 다섯 개를
    직접 해석해야 했고, 이미 문장으로 만들어 둔 판정(`incidents.title`)은 사건 탭을
    따로 열어야만 보였다 — **탐지가 아니라 설명이 산출물이다**(설계 규칙).

    **색만으로 뜻을 지지 않는다**(theme 규칙). 색과 함께 항상 말로 쓴다.
    """

    clicked = QtCore.Signal(int)  # 진행 중 사건 id — 누르면 그 사건을 편다
    label_requested = QtCore.Signal()  # "답하기" — 답 안 준 알림으로 데려간다

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self._incident_id: int | None = None

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(10)

        self._dot = QtWidgets.QLabel("●")
        self._text = QtWidgets.QLabel("확인하는 중…")
        self._text.setStyleSheet(f"color: {theme.INK}; font-size: 14px; font-weight: 600;")
        self._detail = QtWidgets.QLabel("")
        self._detail.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")

        row.addWidget(self._dot)
        row.addWidget(self._text)
        row.addWidget(self._detail)
        row.addStretch(1)

        self._open_btn = QtWidgets.QPushButton("사건 보기")
        self._open_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._open_btn.clicked.connect(self._on_clicked)
        self._open_btn.hide()
        row.addWidget(self._open_btn)

        # **정상일 때도 보이는 유일한 버튼이다.** 답할 알림은 대개 이미 끝난 사건이라
        # 진행 중인 것이 없을 때 물어야 하고, 그때가 사용자에게도 여유가 있는 때다.
        self._label_btn = QtWidgets.QPushButton("알림 답하기")
        self._label_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self._label_btn.clicked.connect(self.label_requested.emit)
        self._label_btn.hide()
        row.addWidget(self._label_btn)

        self._paint(theme.INK_MUTED)

    def _paint(self, colour: str) -> None:
        self._dot.setStyleSheet(f"color: {colour}; font-size: 14px;")
        self.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE}; border: 1px solid {colour};"
            f" border-radius: 8px; }}"
        )

    def _on_clicked(self) -> None:
        if self._incident_id is not None:
            self.clicked.emit(self._incident_id)

    @QtCore.Slot(dict)
    def update_health(self, health: dict) -> None:
        now = time.time()
        text, detail, colour, incident_id = _health_line(health, now)
        self._incident_id = incident_id
        self._text.setText(text)
        self._detail.setText(detail)
        self._paint(colour)
        self._open_btn.setVisible(incident_id is not None)

        prompt = _label_prompt(health, now)
        self._label_btn.setText(prompt or "")
        self._label_btn.setVisible(prompt is not None)

    # 테스트가 읽는다 — 창을 띄우지 않고 문구를 확인하기 위한 것이다.
    @property
    def text(self) -> str:
        return self._text.text()

    @property
    def label_text(self) -> str:
        # **`isVisible()` 이 아니라 `isHidden()` 이다.** 창을 띄우지 않는 테스트에서는
        # 보이도록 세운 위젯도 `isVisible()` 이 거짓이라(부모가 안 떠 있다) 늘 빈 문자열이
        # 됐다. `isHidden()` 은 "숨기라고 했는가"만 본다 — 여기서 물어야 할 것이 그것이다.
        return "" if self._label_btn.isHidden() else self._label_btn.text()


def _health_line(health: dict, now: float) -> tuple[str, str, str, int | None]:
    """상태 한 줄의 **문구·색 판정.** 위젯과 떼어 둬 테스트가 직접 부른다.

    순서에 인과가 있다. **수집이 멈췄는지를 먼저 본다** — 멈추면 사건도 생기지
    않으므로 "정상"과 "죽음"이 똑같이 조용해 보이고, 그 상태로 초록불을 켜면
    사용자는 모니터가 죽은 것을 모른 채 안심한다(설계 규칙 4: 조용히 실패하지 않는다).
    """
    sample_ts = health.get("sample_ts")
    if sample_ts is None:
        return ("수집된 데이터가 없습니다", "상주가 아직 켜지지 않았습니다",
                theme.INK_MUTED, None)

    age = now - float(sample_ts)
    if age > STALE_SAMPLE_S:
        return ("수집이 멈췄습니다", f"마지막 표본 {_ago(age)} 전 — 상주를 확인하세요",
                theme.STATUS["critical"], None)

    incident = health.get("open")
    if incident:
        severity = str(incident.get("severity") or "warning")
        colour = theme.STATUS.get(severity, theme.STATUS["warning"])
        started = now - float(incident["ts_start"])
        return (str(incident.get("title") or "이상 감지"), f"{_ago(started)}째 진행 중",
                colour, int(incident["id"]))

    last_end = health.get("last_end_ts")
    detail = f"마지막 사건 {_ago(now - float(last_end))} 전" if last_end else "기록된 사건 없음"
    return ("정상", detail, theme.STATUS["good"], None)


def _label_prompt(health: dict, now: float) -> str | None:
    """**"답하기" 버튼의 문구.** 없으면 `None` — 버튼을 숨긴다.

    `_health_line` 과 같은 이유로 위젯에서 떼어 뒀다. 판정이 셋이다.

    - 답할 알림이 없으면 묻지 않는다. **0건에 버튼을 남겨 두면 그것이 곧 배경이 되어**
      실제로 밀렸을 때도 눈에 걸리지 않는다.
    - **수집이 멈춰 있으면 묻지 않는다.** 그때 화면이 시켜야 할 일은 "상주를 확인하라"
      하나뿐인데, 옆에 라벨 요청을 나란히 두면 어느 쪽이 급한지 흐려진다(규칙 4).
    - 표본이 아예 없을 때(첫 실행)도 같다.
    """
    pending = int(health.get("unlabeled") or 0)
    if pending <= 0:
        return None
    sample_ts = health.get("sample_ts")
    if sample_ts is None or now - float(sample_ts) > STALE_SAMPLE_S:
        return None
    return f"알림 {pending}건 답하기"


def _ago(seconds: float) -> str:
    """사람이 읽는 경과 시간. **초 단위 숫자는 판단에 쓰이지 않는다.**"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}초"
    if seconds < 3600:
        return f"{seconds / 60:.0f}분"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}시간"
    return f"{seconds / 86400:.0f}일"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, incident_id: int | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Argus")
        self.resize(*_initial_size())

        self.realtime = RealtimePage()
        self.processes = ProcessPage()
        self.incidents = IncidentPage()
        self.timeline = TimelinePage()
        self.usage = UsagePage()
        self.report = ReportPage()
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
        self._page_of: dict[QtWidgets.QWidget, int] = {}
        self._nav_row_of: dict[QtWidgets.QWidget, int] = {}
        # **등록된 페이지를 여기 모은다.** 폴러를 멈출 때 이름을 손으로 나열하면
        # 페이지를 추가할 때마다 그 목록이 조용히 뒤처지고, 남은 `QThread` 가
        # 파괴되면서 프로세스가 통째로 죽는다(0xC0000409). 2026-08-13 에 일일 리포트
        # 탭을 붙이자 테스트 전체가 그렇게 됐다 — **461개가 다 통과한 채 종료 코드만
        # 비0 이라** 원인이 보이지 않았고, mutation sweep 이 기준선에서 멈췄다.
        self._pages: list[QtWidgets.QWidget] = []

        # **매일 보는 것과 이상할 때 보는 것을 섞어 두지 않는다.** 일곱 개가 한 줄로
        # 늘어서 있으면 어디부터 봐야 하는지가 목록에 없다. 순서도 사용 빈도순으로
        # 바꿨다 — 사건은 이 제품의 산출물인데 다섯 번째에 있었다.
        self._add_section("보기")
        self._add_page("실시간", self.realtime)
        self._add_page("프로세스", self.processes)
        self._add_page("사건", self.incidents)
        self._add_page("사용시간", self.usage)
        # 사용시간 바로 아래에 둔다 — 둘은 같은 질문의 다른 답이라("켜져 있던" 대
        # "쓰고 있던") 떨어뜨려 놓으면 어느 것을 보고 있는지 헷갈린다.
        self._add_page("일일 리포트", self.report)
        self._add_section("진단")
        self._add_page("타임라인", self.timeline)
        self._add_page("자기 상태", self.selfstate)
        self._add_page("설정", self.settings)

        self._nav.currentRowChanged.connect(self._on_nav_changed)
        self._nav.setCurrentRow(self._nav_row_of[self.realtime])

        body = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(body)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._nav)

        content = QtWidgets.QWidget()
        content_box = QtWidgets.QVBoxLayout(content)
        content_box.setContentsMargins(16, 16, 16, 8)
        self._banner = _Banner()
        content_box.addWidget(self._banner)
        # **답이 먼저, 근거가 뒤.** 페이지(수치·차트)는 이 한 줄의 근거다.
        self.status_line = _StatusLine()
        self.status_line.clicked.connect(self._open_incident)
        self.status_line.label_requested.connect(self._open_unlabeled)
        content_box.addWidget(self.status_line)
        content_box.addWidget(self._stack)
        row.addWidget(content, stretch=1)

        self.setCentralWidget(body)
        self.statusBar().showMessage("연결 중…")

        # 상태 표시줄은 1초마다 갱신한다. 데이터 조회가 아니라 이미 받은 값을 읽는
        # 것뿐이라 UI 스레드에서 해도 된다.
        self._clock = QtCore.QTimer(self)
        self._clock.timeout.connect(self._refresh_status)
        self._clock.start(1000)
        self._refresh_status()

        self._health = _HealthPoller()
        self._health.loaded.connect(self.status_line.update_health)
        # 같은 답을 실시간 타일도 쓴다 — 맨 윗줄이 "디스크 병목"이라 말했으면
        # 어느 수치가 그 말의 근거인지 화면에서 바로 이어져야 한다.
        self._health.loaded.connect(self.realtime.mark_bottleneck)
        self._health.start()

        # 알림을 눌러서 들어온 경우. **평가하러 온 사람을 목록 앞에 세우지 않는다** —
        # 그 사건을 찾는 일이 남아 있으면 거기서 그만둔다(14일간 피드백 0건이 그 결과다).
        if incident_id is not None:
            self._open_incident(incident_id)

    def _add_page(self, name: str, widget: QtWidgets.QWidget) -> None:
        """페이지를 **스크롤 영역에 담아** 등록한다.

        `QStackedWidget` 은 담긴 페이지 **전부의 최소 높이 중 최댓값**을 자기 최소
        높이로 삼는다. 그래서 스크롤이 없으면 가장 빽빽한 페이지 하나가 창의 하한을
        정하고, 나머지 여섯 페이지까지 그 크기를 끌고 다닌다 — 2026-08-12 실측:
        자기 상태 페이지의 1179px 때문에 `resize(1280, 720)` 이 무시되고 창이
        1280x1255 로 떴다. 크기를 요청해도 안 먹는 상태였다.

        **가로 스크롤은 끈다.** 세로로 긴 것은 굴리면 되지만 가로로 잘린 표는
        읽는 방법이 없고, 폭은 어차피 내용을 맞출 수 있다(`setWidgetResizable`).
        """
        area = QtWidgets.QScrollArea()
        area.setWidget(widget)
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # 스크롤 영역이 제 배경을 칠하면 페이지 뒤에 다른 색 판이 깔린다.
        area.setStyleSheet("QScrollArea { background: transparent; }")
        area.viewport().setStyleSheet("background: transparent;")
        # **이것이 없으면 위 설명이 그대로 재현된다.** `QScrollArea` 는 내용이
        # 커지면 자기 최소 높이도 함께 키우려 하는데, 그러면 스택의 하한이 다시
        # 페이지에 끌려간다. 하한을 못 박아 "굴려서 보면 된다"를 강제한다.
        area.setMinimumHeight(200)

        self._nav.addItem(name)
        self._nav_row_of[widget] = self._nav.count() - 1
        self._stack.addWidget(area)
        # 페이지 위젯 → 스택 인덱스. **`indexOf(page)` 는 이제 -1 이다** — 스택에
        # 들어간 것은 페이지가 아니라 그것을 감싼 스크롤 영역이기 때문이다.
        self._page_of[widget] = self._stack.count() - 1
        self._pages.append(widget)

    def _add_section(self, title: str) -> None:
        """탭 목록의 구분 머리. **고를 수 없는 항목이다.**

        `NoItemFlags` 로 두면 마우스로도 키보드로도 선택되지 않고 Qt 가 알아서
        건너뛴다. 이걸 빼먹으면 머리글을 눌러 빈 화면이 뜬다.
        """
        item = QtWidgets.QListWidgetItem(title)
        item.setFlags(QtCore.Qt.NoItemFlags)
        font = item.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        item.setFont(font)
        item.setForeground(QtGui.QColor(theme.INK_MUTED))
        self._nav.addItem(item)

    def _on_nav_changed(self, row: int) -> None:
        """탭 행 → 페이지. **구분 머리 때문에 둘이 어긋난다** — 행 번호를 그대로
        `setCurrentIndex` 에 넘기면 머리글 개수만큼 밀린 페이지가 뜬다."""
        widget = self._nav.item(row)
        if widget is None:
            return
        for page, nav_row in self._nav_row_of.items():
            if nav_row == row:
                self._stack.setCurrentIndex(self._page_of[page])
                return

    def _open_incident(self, incident_id: int) -> None:
        """맨 윗줄의 "사건 보기". 알림을 누른 것과 같은 경로다."""
        self._nav.setCurrentRow(self._nav_row_of[self.incidents])
        self.incidents.focus_incident(incident_id)

    def _open_unlabeled(self) -> None:
        """맨 윗줄의 "알림 N건 답하기". 답 안 준 알림 중 **가장 최근 것**으로 간다."""
        self._nav.setCurrentRow(self._nav_row_of[self.incidents])
        self.incidents.focus_unlabeled()

    def _refresh_status(self) -> None:
        self.statusBar().showMessage(self.realtime.status_text())
        # DB 존재 확인은 `Path.exists()` 한 번이라 1초마다 해도 된다. 조회가 아니다.
        self._banner.update_text(first_run_notice())

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # **최대화 상태의 크기를 저장하지 않는다.** 그대로 남기면 다음에 최대화가
        # 아닌 창이 화면을 꽉 채운 채 뜨고, 사용자는 그걸 되돌릴 방법을 찾아야 한다.
        if not self.isMaximized() and not self.isMinimized():
            save_window_state(self.width(), self.height())
        self.stop_all()
        super().closeEvent(event)

    def stop_all(self) -> None:
        """폴러를 전부 멈춘다. **창을 만든 쪽은 반드시 이것을 불러야 한다.**

        살아 있는 `QThread` 가 파괴되면 프로세스가 죽는다 — 테스트에서는 전부
        통과한 뒤 종료 코드만 비0 이 되어 원인을 찾기 어렵다.

        `_pages` 를 도는 이유는 목록을 손으로 나열하지 않기 위해서다. 페이지가
        늘 때마다 여기와 테스트 양쪽을 고쳐야 한다면 언젠가 한쪽을 빠뜨린다.
        """
        self._health.stop()
        for page in self._pages:
            stop = getattr(page, "stop", None)
            if callable(stop):
                stop()


def main(seconds: float | None = None, incident_id: int | None = None) -> int:
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

    window = MainWindow(incident_id=incident_id)
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
        print(
            f"  사용시간 조회 {window.usage.load_count}회 · "
            f"프로그램 {window.usage.row_count}종"
        )
        # **조회 횟수만 세면 부족하다** — 행이 하나도 없어도 조회는 돈다. 리포트가
        # 실제로 그려졌는지를 함께 남긴다(첫날에는 "없음"이 정상이다).
        print(
            f"  일일 리포트 조회 {window.report.load_count}회 · "
            f"{'그림' if window.report.has_report else '기록 없음'}"
        )
        # **맨 윗줄이 이 창의 답이다.** 실제로 무슨 문장이 떴는지 남긴다 —
        # 폴러·시그널이 끊기면 "확인하는 중…" 이 그대로 찍힌다.
        print(f"  상태 한 줄: {window.status_line.text}")
        # 답하기 버튼은 **실제 DB 에 밀린 알림이 있어야만** 뜬다. 단위 테스트는 가짜
        # health 로 판정을 재므로, 이 기계에서 실제로 켜지는지는 여기서만 보인다.
        print(f"  답 대기: {window.status_line.label_text or '없음'}")
        if live == 0:
            print("[FAIL] 실시간 표본이 하나도 없다 — 조회나 시그널 경계가 깨졌다")
            return 1
        if live < expected:
            print("[FAIL] 실시간 갱신이 초당 한 점에 못 미친다")
            return 1
        if loads == 0:
            print("[FAIL] 프로세스 표를 한 번도 못 채웠다")
            return 1
        # 데이터가 없어도 빈 결과가 한 번은 와야 한다 — 0 이면 워커·시그널이 끊긴 것이다.
        if window.usage.load_count == 0:
            print("[FAIL] 사용시간 조회가 한 번도 돌지 않았다")
            return 1
        if window.status_line.text == "확인하는 중…":
            print("[FAIL] 상태 한 줄이 한 번도 갱신되지 않았다 — 폴러나 시그널이 끊겼다")
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
    parser.add_argument(
        "--incident", type=int, default=None, help="이 사건을 펴고 시작 (알림 클릭)"
    )
    args, _qt_args = parser.parse_known_args()
    return main(args.seconds, incident_id=args.incident)


if __name__ == "__main__":
    raise SystemExit(cli())

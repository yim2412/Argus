"""설정 — 지금은 알림 켬/끔 하나다.

**이 창은 상주와 별도 프로세스다**(창이 죽어도 수집이 계속되게 나눈 결과 — 설계 규칙 1).
그래서 여기서 바꾼 값은 메모리로 전달되지 않고 **파일을 거친다**. 상주 쪽
`LiveConfigWatcher` 가 2초마다 mtime 을 보고 집어 간다.

반대 방향도 같다 — 트레이 메뉴에서 바꾸면 이 페이지가 그것을 따라와야 한다. 그래서
이 페이지도 주기적으로 파일을 다시 읽는다. **한쪽만 하면 두 화면이 서로 다른 값을
보여 주고, 그때 사용자는 무엇이 참인지 알 수 없다.**

여기서 끄는 것은 **발송**뿐이다. 탐지·판정(`notified`)·기록은 그대로 돈다 — 그렇게
나눠 둔 덕에 "켜면 몇 건이 올 것인가"를 꺼진 상태로 계속 잴 수 있다.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...config.loader import ConfigError, load_settings
from ...dashboard import theme
from ...runtime.livecfg import LiveConfig
from ..widgets import section

# 파일을 다시 보는 주기(ms). 트레이에서 바꾼 것이 이 안에 따라온다.
_POLL_MS = 2000


def _default_notify() -> bool:
    """설정 파일의 값. 실행 중 설정 파일이 없을 때의 기준선이다."""
    try:
        return bool(load_settings().detection.notify)
    except ConfigError:
        # 설정이 깨져 있어도 이 페이지는 떠야 한다 — 여기서 죽으면 창 전체가 안 뜬다.
        return True


def _explain(text: str) -> QtWidgets.QLabel:
    """설정 항목에 딸리는 설명. **왼쪽 정렬로 바로 위 항목에 붙인다.**"""
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 12px;")
    return label


class SettingsPage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._live = LiveConfig(defaults={"notify": _default_notify()})
        # 파일 갱신으로 체크박스를 바꿀 때 신호가 다시 돌아와 파일을 또 쓰는 것을 막는다.
        self._syncing = False
        self.reload_count = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        outer.addWidget(section("알림"))

        self.notify_box = QtWidgets.QCheckBox("성능 이상을 알림으로 받기")
        self.notify_box.setStyleSheet(f"color: {theme.INK}; font-size: 14px;")
        self.notify_box.toggled.connect(self._on_toggled)
        outer.addWidget(self.notify_box)

        # **`message()` 를 쓰지 않는다.** 그건 "데이터 없음" 안내용이라 가운데 정렬인데,
        # 설정 설명이 화면 한복판에 뜨면 바로 위 체크박스와 연결이 끊겨 보인다.
        outer.addWidget(
            _explain(
                "끄면 풍선 알림만 멈춥니다. 성능 이상을 찾고 기록하는 일은 계속되므로, "
                "사건 목록과 대시보드에서는 그대로 볼 수 있습니다."
            )
        )

        self.source_label = _explain("")
        outer.addWidget(self.source_label)

        outer.addStretch(1)

        self._sync_from_file()

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._sync_from_file)
        self._timer.start(_POLL_MS)

    # ------------------------------------------------------------------ 동기화

    def _sync_from_file(self) -> None:
        """파일이 바뀌었으면 화면을 맞춘다(트레이에서 바꾼 경우)."""
        changed = self._live.reload()
        if not changed and self.notify_box.isChecked() == self._live.notify_enabled:
            return

        self.reload_count += 1
        self._syncing = True
        try:
            self.notify_box.setChecked(self._live.notify_enabled)
        finally:
            self._syncing = False
        self._refresh_source()

    def _refresh_source(self) -> None:
        """**어느 쪽이 이겼는지 드러낸다**(규칙 4).

        UI 값이 `settings.yaml` 을 이기므로, 파일을 고쳤는데 안 먹는 이유가 여기
        보여야 한다. 조용하면 사용자는 설정이 고장 났다고 생각한다.
        """
        if self._live.overridden("notify"):
            self.source_label.setText(
                f"이 값은 여기서 바꾼 것입니다 (settings.yaml 보다 우선). "
                f"되돌리려면 {self._live.path} 를 지우세요."
            )
        else:
            self.source_label.setText("이 값은 settings.yaml 의 detection.notify 입니다.")

    def _on_toggled(self, checked: bool) -> None:
        if self._syncing:
            return
        self._live.set("notify", checked)
        self._refresh_source()

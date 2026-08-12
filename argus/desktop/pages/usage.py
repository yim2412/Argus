"""사용시간 — 어떤 프로그램을 얼마나 켜 뒀나.

**이 화면의 값은 "존재 여부"가 아니라 시간이다.** 5분 버킷을 세는 방식이면 오래 켜 두는
프로그램이 전부 관측 시간 전체로 붙어 순위가 무의미해진다(실측: chrome·discord·steam 이
나란히 238h). 롤업이 이름 단위 구간 합집합으로 접어 둔 값을 그대로 읽는다.

**비율의 분모는 달력이 아니라 관측 시간이다.** Argus 는 PC 가 켜져 있을 때만 돌므로
"30일 중 40시간"은 뜻이 흐리다 — "관측 242시간 중 40시간(17%)"이라야 읽힌다.

갱신 주기가 길다(5분). 하루에 한 번 접히는 값이라 자주 물을 이유가 없다.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...dashboard import data, theme
from ..widgets import BarRanking, Column, DataTable, StatTile, message

# (일수, 표시 이름). 0 은 "전체" — 롤업이 남아 있는 만큼 전부.
_RANGES = ((7, "최근 7일"), (30, "최근 30일"), (90, "최근 90일"), (0, "전체"))
_RANKING_TOP = 15
_ALL_DAYS = 36500  # '전체'를 조회로 옮길 때 쓰는 값. 롤업 보존이 실질 상한이다.


class UsagePoller(QtCore.QThread):
    """누적 사용시간 조회. 기간이 바뀌면 즉시 다시 읽는다."""

    loaded = QtCore.Signal(list, float)

    def __init__(self, interval_s: float = 300.0) -> None:
        super().__init__()
        self.interval_s = interval_s
        self._days = 30
        self._user_only = True
        self._stop = False
        self._wake = QtCore.QSemaphore(0)

    def set_days(self, days: int) -> None:
        self._days = days
        self._wake.release()

    def set_user_only(self, user_only: bool) -> None:
        self._user_only = user_only
        self._wake.release()

    def run(self) -> None:
        while not self._stop:
            days = self._days or _ALL_DAYS
            try:
                rows = data.program_usage(days=days, limit=200, user_only=self._user_only)
                observed = data.program_usage_observed(days=days)
            except Exception:
                rows, observed = [], 0.0
            self.loaded.emit(rows, observed)
            self._wake.tryAcquire(1, int(self.interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self._wake.release()
        self.wait(3000)


class UsagePage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._loads = 0
        self._rows = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("기간"))
        self._range = QtWidgets.QComboBox()
        for days, label in _RANGES:
            self._range.addItem(label, days)
        self._range.setCurrentIndex(1)  # 최근 30일
        self._range.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range)

        # **기본은 켜짐.** 끄면 상위가 전부 배경 서비스라(svchost 238h · conhost 209h)
        # "내가 무엇을 얼마나 했나"를 읽을 수 없다. 그래도 끌 수 있게 두는 이유는
        # 그 값이 틀린 것이 아니라 다른 질문의 답이기 때문이다.
        self._user_only = QtWidgets.QCheckBox("내가 쓴 프로그램만")
        self._user_only.setChecked(True)
        self._user_only.setToolTip(
            "창을 띄워 앞에 놓인 적이 있는 프로그램만 봅니다.\n"
            "끄면 배경 서비스까지 전부 나옵니다."
        )
        self._user_only.toggled.connect(self._on_filter_changed)
        top.addWidget(self._user_only)

        top.addStretch(1)
        outer.addLayout(top)

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(12)
        self._observed = StatTile("관측 시간")
        self._top = StatTile("가장 오래 켠 프로그램")
        self._counted = StatTile("집계된 프로그램")
        for tile in (self._observed, self._top, self._counted):
            tiles.addWidget(tile)
        tiles.addStretch(1)
        outer.addLayout(tiles)

        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)
        self._ranking = BarRanking("사용시간", unit="h")
        split.addWidget(self._ranking, stretch=2)

        self._table = DataTable(
            [
                Column("name", "프로그램", width=150),
                # **이름만으로는 무엇인지 알 수 없다.** 상위를 차지하는 것 대부분이
                # svchost · dwm · ctfmon 처럼 사용자가 설치한 적 없는 것들이다.
                Column("description", "무슨 프로그램", width=230),
                Column("hours", "사용시간", fmt=",.1f", suffix=" h", align_right=True, width=100),
                Column("share", "관측 대비", fmt=".1f", suffix="%", align_right=True, width=90),
                Column("launches", "실행", fmt=",.0f", suffix="회", align_right=True, width=80),
                Column("days", "기록된 날", fmt=",.0f", suffix="일", align_right=True, width=90),
            ]
        )
        split.addWidget(self._table, stretch=3)
        outer.addLayout(split, stretch=3)

        # **"켜져 있던 시간"과 "쓰고 있던 시간"은 다르다.** 게임을 켜 두고 자리를
        # 비우면 그 시간도 세어진다. 그 사실을 화면에서 말해 주지 않으면 값이 틀린
        # 것처럼 보인다.
        note = QtWidgets.QLabel(
            "프로그램이 켜져 있던 시간입니다(창을 보고 있던 시간이 아닙니다). "
            "체크를 끄면 배경 서비스까지 전부 나옵니다 — 늘 떠 있는 것들이라 "
            "상위를 차지하는 것이 정상입니다."
        )
        note.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        outer.addWidget(note)

        self._notice = message(
            "사용시간은 하루가 끝난 뒤에 집계됩니다 — 첫 결과는 자정 이후에 나옵니다."
        )
        outer.addWidget(self._notice)

        self._poller = UsagePoller()
        self._poller.loaded.connect(self._on_rows)
        self._poller.start()

    def _on_range_changed(self) -> None:
        self._poller.set_days(int(self._range.currentData()))

    def _on_filter_changed(self, user_only: bool) -> None:
        self._poller.set_user_only(user_only)

    @QtCore.Slot(list, float)
    def _on_rows(self, rows: list, observed: float) -> None:
        self._loads += 1
        self._rows = len(rows)
        if not rows:
            self._notice.setVisible(True)
            self._table.set_rows([])
            self._ranking.set_values([], [])
            for tile in (self._observed, self._top, self._counted):
                tile.set("—")
            return
        self._notice.setVisible(False)

        shown = []
        for row in rows:
            seconds = float(row.get("seconds") or 0.0)
            shown.append(
                {
                    **row,
                    "hours": seconds / 3600.0,
                    # 분모가 0 이면 비율을 만들 수 없다. 0% 로 적으면 "안 썼다"로 읽힌다.
                    "share": (seconds / observed * 100.0) if observed > 0 else None,
                }
            )

        self._table.set_rows(shown)
        top = shown[:_RANKING_TOP]
        self._ranking.set_values([r["name"] for r in top], [r["hours"] for r in top])

        self._observed.set(f"{observed / 3600.0:,.0f} h", "PC 가 켜져 있던 시간")
        best = shown[0]
        # **타일에도 설명을 붙인다.** "가장 오래 켠 프로그램: svchost" 는 답이 아니다.
        self._top.set(best["name"], f"{best['hours']:,.1f} h · {best.get('description') or ''}")
        self._counted.set(f"{len(rows)}종", "이 기간에 한 번이라도 뜬 것")

    @property
    def load_count(self) -> int:
        """`--seconds` 검증이 읽는다. 0 이면 조회나 시그널 경계가 깨진 것이다."""
        return self._loads

    @property
    def row_count(self) -> int:
        return self._rows

    def stop(self) -> None:
        self._poller.stop()

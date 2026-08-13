"""일일 리포트 — 어제 무엇을 했나.

**사용시간 화면과 세는 것이 다르다.** 저기는 프로그램이 *켜져 있던* 시간이고, 여기는
창이 *앞에 놓여 있던* 시간이다. 게임을 켜 두고 자리를 비운 두 시간은 저기서는 세지고
여기서는 세지 않는다. 두 화면의 같은 프로그램이 다른 숫자를 보이는 것이 정상이라,
그 사실을 화면에서 말해 준다 — 말하지 않으면 둘 중 하나가 틀린 것처럼 보인다.

**하루가 끝난 뒤에만 만들어진다.** 진행 중인 날을 접으면 부분값이 확정으로 남으므로,
오늘은 목록에 없다. 그것도 화면에서 말해 준다.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...dashboard import theme
from ...report import data
from ..widgets import BarRanking, Column, DataTable, StatTile, message


def _hours(seconds: float) -> str:
    """초 → "3시간 20분". **소수 시간으로 쓰지 않는다** — "3.3h" 는 읽는 사람이
    분으로 환산해야 하는데, 이 화면의 값은 사람이 보낸 시간이라 그럴 이유가 없다."""
    total = int(round(seconds / 60))
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}시간 {m}분"
    if h:
        return f"{h}시간"
    return f"{m}분"


def _delta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if abs(seconds) < 60:
        return "직전 기록일과 비슷"
    sign = "+" if seconds > 0 else "-"
    return f"직전 기록일 대비 {sign}{_hours(abs(seconds))}"


class ReportPoller(QtCore.QThread):
    """리포트 조회. 날짜가 바뀌면 즉시 다시 읽는다.

    조회는 워커에서 한다 — 하루에 한 번 바뀌는 값이지만, UI 스레드에서 DB 를 열면
    잠깐이라도 창이 멎는다.
    """

    loaded = QtCore.Signal(list, object, object)

    def __init__(self, interval_s: float = 300.0) -> None:
        super().__init__()
        self.interval_s = interval_s
        self._day: str | None = None
        self._stop = False
        self._wake = QtCore.QSemaphore(0)

    def set_day(self, day: str | None) -> None:
        self._day = day
        self._wake.release()

    def run(self) -> None:
        while not self._stop:
            try:
                data.clear_cache()
                days = data.available_days()
                # 고른 날이 없거나 사라졌으면 가장 최근 기록일을 본다.
                day = self._day if self._day in days else (days[0] if days else None)
                report = data.report(day) if day else None
                comparison = data.compare(day) if day else None
            except Exception:
                days, report, comparison = [], None, None
            self.loaded.emit(days, report, comparison)
            self._wake.tryAcquire(1, int(self.interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self._wake.release()
        self.wait(3000)


class ReportPage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._loads = 0
        self._has_report = False
        self._filling = False

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("날짜"))
        # **달력이 아니라 목록이다.** 기록이 있는 날만 고를 수 있으면 "왜 비어 있지"
        # 라는 질문 자체가 생기지 않는다.
        self._picker = QtWidgets.QComboBox()
        self._picker.setMinimumWidth(140)
        self._picker.currentIndexChanged.connect(self._on_day_changed)
        top.addWidget(self._picker)
        top.addStretch(1)
        outer.addLayout(top)

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(12)
        self._total = StatTile("쓴 시간")
        self._share = StatTile("관측 대비")
        self._top_app = StatTile("가장 오래 쓴 것")
        for tile in (self._total, self._share, self._top_app):
            tiles.addWidget(tile)
        tiles.addStretch(1)
        outer.addLayout(tiles)

        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)
        self._categories = BarRanking("무엇을 하며 보냈나", unit="분")
        split.addWidget(self._categories, stretch=2)
        self._slots = BarRanking("언제 썼나", unit="분")
        split.addWidget(self._slots, stretch=2)
        outer.addLayout(split, stretch=3)

        self._table = DataTable(
            [
                Column("name", "프로그램", width=180),
                Column("category", "분류", width=110),
                Column("minutes", "쓴 시간", fmt=",.0f", suffix=" 분", align_right=True, width=100),
                Column("share", "그날 대비", fmt=".1f", suffix="%", align_right=True, width=100),
            ]
        )
        outer.addWidget(self._table, stretch=2)

        note = QtWidgets.QLabel(
            "창을 앞에 놓고 있던 시간입니다 — 사용시간 화면(켜져 있던 시간)보다 짧습니다. "
            "분류는 설정 파일의 usage.categories 로 바꿀 수 있고, 없는 이름은 기타로 갑니다."
        )
        note.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        self._notice = message(
            "리포트는 하루가 끝난 뒤에 만들어집니다 — 첫 결과는 자정 이후에 나옵니다."
        )
        outer.addWidget(self._notice)

        self._poller = ReportPoller()
        self._poller.loaded.connect(self._on_loaded)
        self._poller.start()

    def _on_day_changed(self) -> None:
        # 목록을 채우는 중에도 이 신호가 온다. 그때 조회를 걸면 사용자가 고르지 않은
        # 날짜로 화면이 튄다.
        if self._filling:
            return
        self._poller.set_day(self._picker.currentData())

    @QtCore.Slot(list, object, object)
    def _on_loaded(self, days: list, report: object, comparison: object) -> None:
        self._loads += 1
        self._fill_picker(days)

        self._has_report = isinstance(report, dict)
        if not self._has_report:
            self._notice.setVisible(True)
            self._table.set_rows([])
            self._categories.set_values([], [])
            self._slots.set_values([], [])
            for tile in (self._total, self._share, self._top_app):
                tile.set("—")
            return
        self._notice.setVisible(False)
        assert isinstance(report, dict)

        total = report["total_s"]
        observed = report["observed_s"]
        delta = comparison.get("total_delta_s") if isinstance(comparison, dict) else None
        self._total.set(_hours(total), _delta(delta))
        # 분모가 0 이면 비율을 만들 수 없다. 0% 로 적으면 "안 썼다"로 읽힌다.
        self._share.set(
            f"{total / observed * 100:.0f}%" if observed > 0 else "—",
            f"관측 {_hours(observed)}",
        )

        top_apps = report["top_apps"]
        if top_apps:
            first = top_apps[0]
            self._top_app.set(first["name"], _hours(first["seconds"]))
        else:
            self._top_app.set("—")

        by_category = sorted(report["by_category"].items(), key=lambda kv: -kv[1])
        self._categories.set_values(
            [k for k, _ in by_category], [v / 60 for _, v in by_category]
        )

        # **시간대는 값 순서가 아니라 하루 순서로 둔다.** 크기 순으로 정렬하면
        # "새벽·오후·오전·저녁" 처럼 나와서 하루의 모양이 보이지 않는다.
        by_slot = report["by_slot"]
        order = [k for k in _slot_order(by_slot)]
        self._slots.set_values(order, [by_slot[k] / 60 for k in order])

        self._table.set_rows(
            [
                {
                    "name": app["name"],
                    "category": app.get("category", ""),
                    "minutes": app["seconds"] / 60,
                    "share": (app["seconds"] / total * 100) if total > 0 else None,
                }
                for app in top_apps
            ]
        )

    def _fill_picker(self, days: list) -> None:
        existing = [self._picker.itemData(i) for i in range(self._picker.count())]
        if existing == days:
            return
        self._filling = True
        current = self._picker.currentData()
        self._picker.clear()
        for day in days:
            self._picker.addItem(day, day)
        if current in days:
            self._picker.setCurrentIndex(days.index(current))
        self._filling = False

    @property
    def load_count(self) -> int:
        """`--seconds` 검증이 읽는다. 0 이면 조회나 시그널 경계가 깨진 것이다."""
        return self._loads

    @property
    def has_report(self) -> bool:
        """리포트를 실제로 그렸는가. **조회 횟수만으로는 부족하다** — 조회는 도는데
        행이 없어도 `load_count` 는 올라간다."""
        return self._has_report

    def stop(self) -> None:
        self._poller.stop()


def _slot_order(by_slot: dict) -> list:
    """시간대를 하루 순서로. 설정에 없는 이름이 와도 뒤에 붙여 잃지 않는다."""
    known = ["새벽", "오전", "오후", "저녁"]
    ordered = [k for k in known if k in by_slot]
    return ordered + [k for k in by_slot if k not in known]

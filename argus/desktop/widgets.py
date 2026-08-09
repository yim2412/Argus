"""페이지들이 공유하는 위젯.

**차트는 pyqtgraph 로 그린다.** plotly 를 Qt 에서 쓰려면 QtWebEngine 이 필요한데
그것만으로 번들이 300MB 늘고, 웹뷰를 띄우는 순간 "네이티브 앱"이라는 목적도 흐려진다.

여기 있는 것은 표시 규칙이지 데이터가 아니다 — 조회는 `dashboard.data` 가 맡고
이 모듈은 그것을 모른다.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ..dashboard import theme

# 차트가 데이터를 읽히게 하는 최소 높이(px). 이보다 낮으면 선이 겹쳐 뭉개지고,
# 축 눈금이 두세 개만 남아 **그려져 있지만 읽을 수 없는** 상태가 된다.
# 2026-08-06 실측: 55px 로 눌린 보조 차트들이 정확히 그 상태였다.
MIN_PLOT_HEIGHT = 132
# 주 차트(그 페이지에서 가장 중요한 것). 여기까지 줘야 봉우리 모양이 보인다.
MAIN_PLOT_HEIGHT = 190
# 보조 차트의 상한. **상한이 없는 쪽이 남는 공간을 전부 먹는다** — `stretch` 는 여유
# 공간만 나누는데 pyqtgraph 의 sizeHint 가 커서 비율이 사실상 무시된다.
# 2026-08-06 실측: 보조 넷이 460px 씩 가져가 정작 주 차트가 190px 로 가장 작았다.
SUB_PLOT_MAX = 200
# 주 차트 상한은 **페이지마다 다르다** — 한 화면에 들어갈 요소 수가 다르기 때문이다.
# 여기 있는 것은 요소가 적은 페이지의 기본값이고, 빽빽한 페이지는 직접 낮춘다.
MAIN_PLOT_MAX = 330
# 차트 아래 한 줄 설명의 높이. 고정이라야 레이아웃 계산이 흔들리지 않는다.
NOTE_HEIGHT = 16


def legend_chip(name: str, colour: str) -> QtWidgets.QWidget:
    """계열 이름표. **플롯 안이 아니라 제목 줄에 놓는다.**

    pyqtgraph 범례는 플롯 위에 떠서 데이터를 덮는다. 차트가 클 때는 빈 구석에
    앉지만 작아지면 선 위로 올라오고, 하필 그때가 가장 읽기 어려운 순간이다.
    바깥으로 빼면 크기와 무관하게 겹치지 않는다.
    """
    chip = QtWidgets.QWidget()
    row = QtWidgets.QHBoxLayout(chip)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)

    dot = QtWidgets.QLabel("●")
    dot.setStyleSheet(f"color: {colour}; font-size: 11px;")
    label = QtWidgets.QLabel(name)
    label.setStyleSheet(f"color: {theme.INK_SECONDARY}; font-size: 11px;")

    row.addWidget(dot)
    row.addWidget(label)
    return chip


def chart_header(title: str, names: Sequence[str]) -> QtWidgets.QWidget:
    """제목 + 계열 이름표 한 줄."""
    header = QtWidgets.QWidget()
    row = QtWidgets.QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)

    label = QtWidgets.QLabel(title)
    label.setStyleSheet(f"color: {theme.INK}; font-size: 13px; font-weight: 600;")
    row.addWidget(label)

    if len(names) > 1:
        for index, name in enumerate(names):
            row.addWidget(legend_chip(name, theme.SERIES[index % len(theme.SERIES)]))
    row.addStretch(1)
    return header



def chart_note(text: str) -> QtWidgets.QLabel:
    """차트 아래 한 줄 설명.

    **세로로 자라지 않게 못 박는다.** `wordWrap` 을 켠 라벨은 `heightForWidth` 를
    갖는데, 그런 위젯이 `QGridLayout` 의 한 행에만 있으면 Qt 가 행 배분을 다르게
    계산해 `setRowStretch` 가 무시된다 — 2026-08-06 격리 실험: 주석이 있으면
    518 대 154, 없으면 336 대 336 이었다. 같은 `stretch=1` 인데도 그랬다.
    """
    label = QtWidgets.QLabel(text)
    label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 10px;")
    # `wordWrap` 을 끈다 — 켜 두면 높이가 폭에 따라 달라지고, 그 순간 위 문제가 돌아온다.
    # 설명은 한 줄로 쓴다(길면 페이지 아래 별도 문단이 맞다).
    label.setWordWrap(False)
    label.setFixedHeight(NOTE_HEIGHT)
    return label


class StatTile(QtWidgets.QFrame):
    """큰 수치 하나와 그 아래 보조 설명.

    **보조 설명이 있어야 수치가 뜻을 갖는다.** "디스크 12MB/s" 만으로는 읽기인지
    쓰기인지 모르고, "응답 0.2ms" 는 큐 깊이를 같이 봐야 판단이 선다.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setMinimumWidth(150)
        # **세로로는 자라지 않는다.** 타일은 글자 세 줄이 전부라 늘어날 이유가 없는데,
        # 옆의 차트에 상한이 생기는 순간 남는 공간이 전부 이리로 몰린다 —
        # 2026-08-06 실측: 100px 짜리 타일이 460px 로 부풀어 화면 절반을 먹었다.
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed
        )

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(14, 10, 14, 10)
        box.setSpacing(2)

        caption = QtWidgets.QLabel(title)
        caption.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        self._value = QtWidgets.QLabel("—")
        self._value.setStyleSheet(f"color: {theme.INK}; font-size: 26px; font-weight: 600;")
        self._note = QtWidgets.QLabel(" ")
        self._note.setStyleSheet(f"color: {theme.INK_SECONDARY}; font-size: 11px;")

        box.addWidget(caption)
        box.addWidget(self._value)
        box.addWidget(self._note)

    def set(self, value: str, note: str = "") -> None:
        self._value.setText(value)
        self._note.setText(note or " ")  # 빈 문자열이면 높이가 줄어 타일이 흔들린다


class TimeSeriesChart(QtWidgets.QWidget):
    """시간축 꺾은선. **x 는 "몇 초 전"이다** — 절대 시각은 폭을 먹고 읽히지 않는다.

    창을 하나만 유지하고(`maxlen`) 뒤에서 밀어 넣는 구조라, 실시간 갱신에서 매번
    전체를 다시 만들지 않는다.
    """

    def __init__(
        self,
        title: str,
        names: Sequence[str],
        *,
        unit: str = "",
        y_range: tuple[float, float] | None = None,
        maxlen: int = 600,
        note: str = "",
        min_height: int = MIN_PLOT_HEIGHT,
        max_height: int | None = None,
    ) -> None:
        super().__init__()
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        box.addWidget(chart_header(title, names))

        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=False, y=True, alpha=0.15)
        self._plot.setLabel("left", unit)
        self._plot.setLabel("bottom", "초 전")
        self._plot.setMouseEnabled(x=False, y=False)  # 실시간 창에서 축이 밀리면 헷갈린다
        self._plot.hideButtons()
        if y_range:
            self._plot.setYRange(*y_range)
        # **범례는 제목 줄에 있다**(`chart_header`). 플롯 안에 두면 차트가 작아질 때
        # 데이터를 덮는다.
        self._plot.setMinimumHeight(min_height)
        self._hint_height = min_height + 34  # 제목 줄과 여백
        # **남는 공간을 달라고 말해야 받는다.** 기본 정책(Preferred)은 sizeHint 를
        # 선호할 뿐이라 그리드에서 한 행만 늘어나고 다른 행은 최소 크기에 머문다 —
        # 2026-08-06 실측: 같은 `setRowStretch(1)` 인데 518px 대 166px 이었다.
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        if max_height:
            self.setMaximumHeight(max_height)
        box.addWidget(self._plot, stretch=1)

        if note:
            box.addWidget(chart_note(note))

        self._x: deque[float] = deque(maxlen=maxlen)
        self._series: dict[str, deque[float]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        for index, name in enumerate(names):
            colour = theme.SERIES[index % len(theme.SERIES)]
            self._series[name] = deque(maxlen=maxlen)
            self._curves[name] = self._plot.plot(
                [], [], pen=pg.mkPen(colour, width=2), name=name
            )


    def sizeHint(self) -> QtCore.QSize:
        """**최소 높이를 그대로 돌려준다.**

        pyqtgraph 의 기본 `sizeHint` 는 넉넉해서, 레이아웃이 그것부터 채우고 나면
        `stretch` 로 나눌 여유가 거의 남지 않는다. 그 결과 비율이 사실상 무시되고
        먼저 놓인 차트가 공간을 독식한다 — 2026-08-06 실측: `stretch=3` 인 주 차트가
        `stretch=4` 인 보조 넷보다 작았다.

        힌트를 최소치로 낮추면 남는 공간이 전부 `stretch` 몫이 되어 비율이 실제로 먹는다.
        """
        return QtCore.QSize(320, self._hint_height)

    def reset(self, timestamps: Iterable[float], values: dict[str, Iterable[float]]) -> None:
        """과거 구간을 한 번에 채운다(백필). 창을 열자마자 빈 화면을 보여주지 않는다."""
        self._x.clear()
        self._x.extend(timestamps)
        for name, deq in self._series.items():
            deq.clear()
            deq.extend(values.get(name, []))
        self._redraw()

    def append(self, ts: float, values: dict[str, float]) -> None:
        self._x.append(ts)
        for name, deq in self._series.items():
            deq.append(float(values.get(name) or 0.0))
        self._redraw()

    def _redraw(self) -> None:
        if not self._x:
            return
        now = self._x[-1]
        xs = [t - now for t in self._x]  # 오른쪽 끝이 0 = 지금
        for name, curve in self._curves.items():
            values = self._series[name]
            if len(values) == len(xs):
                curve.setData(xs, list(values))


class HistoryChart(QtWidgets.QWidget):
    """긴 구간용 시계열. **x 축이 절대 시각이다.**

    실시간 차트(`TimeSeriesChart`)는 "몇 초 전"으로 그린다 — 10분 창에서는 그게 읽기
    쉽다. 그러나 24시간이나 일주일을 "-86400초"로 적으면 아무도 못 읽는다. 여기서는
    `DateAxisItem` 으로 날짜·시각을 찍는다.

    **오버레이를 그린다.** 결함 주입 구간(세로 밴드)과 탐지 신호(세로선)를 지표 위에
    겹쳐야 "그때 무엇을 넣었고 무엇이 울렸나"가 한눈에 보인다. 색은 계열 팔레트가
    아니라 상태색을 쓴다 — 이건 데이터 계열이 아니라 표시라서, 계열 색으로 그리면
    5번째 지표처럼 읽힌다.
    """

    def __init__(
        self,
        title: str,
        names: Sequence[str],
        *,
        unit: str = "",
        y_range: tuple[float, float] | None = None,
        note: str = "",
        min_height: int = MIN_PLOT_HEIGHT,
        max_height: int | None = None,
    ) -> None:
        super().__init__()
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        box.addWidget(chart_header(title, names))

        self._plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self._plot.showGrid(x=True, y=True, alpha=0.15)
        self._plot.setLabel("left", unit)
        if y_range:
            self._plot.setYRange(*y_range)
        # 범례는 제목 줄로 뺐다 — 플롯 안에 두면 작아질 때 데이터를 덮는다.
        self._plot.setMinimumHeight(min_height)
        self._hint_height = min_height + 34  # 제목 줄과 여백
        # **남는 공간을 달라고 말해야 받는다.** 기본 정책(Preferred)은 sizeHint 를
        # 선호할 뿐이라 그리드에서 한 행만 늘어나고 다른 행은 최소 크기에 머문다 —
        # 2026-08-06 실측: 같은 `setRowStretch(1)` 인데 518px 대 166px 이었다.
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        if max_height:
            self.setMaximumHeight(max_height)
        box.addWidget(self._plot, stretch=1)

        if note:
            box.addWidget(chart_note(note))

        self._curves: dict[str, pg.PlotDataItem] = {}
        for index, name in enumerate(names):
            colour = theme.SERIES[index % len(theme.SERIES)]
            self._curves[name] = self._plot.plot(
                [], [], pen=pg.mkPen(colour, width=2), name=name
            )
        self._overlays: list = []


    def sizeHint(self) -> QtCore.QSize:
        """**최소 높이를 그대로 돌려준다.**

        pyqtgraph 의 기본 `sizeHint` 는 넉넉해서, 레이아웃이 그것부터 채우고 나면
        `stretch` 로 나눌 여유가 거의 남지 않는다. 그 결과 비율이 사실상 무시되고
        먼저 놓인 차트가 공간을 독식한다 — 2026-08-06 실측: `stretch=3` 인 주 차트가
        `stretch=4` 인 보조 넷보다 작았다.

        힌트를 최소치로 낮추면 남는 공간이 전부 `stretch` 몫이 되어 비율이 실제로 먹는다.
        """
        return QtCore.QSize(320, self._hint_height)

    def set_data(self, timestamps: Sequence[float], values: dict[str, Sequence[float]]) -> None:
        for name, curve in self._curves.items():
            series = values.get(name) or []
            if len(series) == len(timestamps):
                curve.setData(list(timestamps), list(series))

    def set_overlays(self, bands: Sequence[dict], marks: Sequence[float]) -> None:
        """`bands` 는 `{lo, hi, strong}`, `marks` 는 시각 목록.

        **효과가 관측되지 않은 주입은 흐리게 그린다**(`strong=False`). 라벨은 있으나
        증상이 없던 구간이라 채점에서 빠지는데, 같은 진하기로 그리면 "탐지 실패"처럼
        보인다.
        """
        for item in self._overlays:
            self._plot.removeItem(item)
        self._overlays.clear()

        for band in bands:
            colour = QtGui.QColor(theme.STATUS["warning"])
            colour.setAlphaF(0.22 if band.get("strong") else 0.07)
            region = pg.LinearRegionItem(
                values=(band["lo"], band["hi"]), brush=colour, movable=False
            )
            region.setZValue(-10)  # 지표 선 아래로
            self._plot.addItem(region)
            self._overlays.append(region)

        pen = pg.mkPen(theme.STATUS["critical"], width=1, style=QtCore.Qt.DotLine)
        for ts in marks:
            line = pg.InfiniteLine(pos=ts, angle=90, pen=pen)
            self._plot.addItem(line)
            self._overlays.append(line)

    @property
    def overlay_count(self) -> int:
        """검증용 — 오버레이가 실제로 그려졌는지 숫자로 확인한다."""
        return len(self._overlays)


class Column:
    """표의 열 하나. **표시 형식을 데이터와 분리한다.**

    포맷을 모델 안에 박으면 같은 데이터를 다른 화면에서 다르게 보여줄 수 없고,
    정렬이 문자열 기준이 되어 "9 MB" 가 "10 MB" 보다 뒤로 간다.
    """

    def __init__(
        self,
        key: str,
        title: str,
        *,
        fmt: str = "",
        suffix: str = "",
        align_right: bool = False,
        width: int | None = None,
    ) -> None:
        self.key = key
        self.title = title
        self.fmt = fmt
        self.suffix = suffix
        self.align_right = align_right
        self.width = width

    def display(self, row: dict) -> str:
        value = row.get(self.key)
        if value is None or value == "":
            return "—"
        if self.fmt:
            try:
                return format(value, self.fmt) + self.suffix
            except (TypeError, ValueError):
                return str(value)
        return str(value) + self.suffix


class TableModel(QtCore.QAbstractTableModel):
    """행이 `dict` 인 표. 프로세스·사건이 함께 쓴다.

    **정렬은 원본 값으로 한다.** 표시 문자열로 정렬하면 숫자가 사전순이 되어
    9 MB 가 10 MB 뒤에 온다 — 사용량 표에서 그건 치명적이다.
    """

    def __init__(self, columns: Sequence[Column], rows: Sequence[dict] | None = None) -> None:
        super().__init__()
        self._columns = list(columns)
        self._rows: list[dict] = list(rows or [])

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:  # noqa: N802 - Qt 규약
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QtCore.QModelIndex, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        column = self._columns[index.column()]
        row = self._rows[index.row()]
        if role == QtCore.Qt.DisplayRole:
            return column.display(row)
        if role == QtCore.Qt.TextAlignmentRole and column.align_right:
            return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if role == QtCore.Qt.UserRole:
            return row.get(column.key)  # 정렬용 원본 값
        return None

    def headerData(self, section: int, orientation, role=QtCore.Qt.DisplayRole):  # noqa: N802
        if role != QtCore.Qt.DisplayRole or orientation != QtCore.Qt.Horizontal:
            return None
        return self._columns[section].title

    def set_rows(self, rows: Sequence[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, index: int) -> dict | None:
        return self._rows[index] if 0 <= index < len(self._rows) else None

    @property
    def columns(self) -> list[Column]:
        return self._columns


class _SortProxy(QtCore.QSortFilterProxyModel):
    """`UserRole`(원본 값)로 정렬한다."""

    def lessThan(self, left, right) -> bool:  # noqa: N802
        a = self.sourceModel().data(left, QtCore.Qt.UserRole)
        b = self.sourceModel().data(right, QtCore.Qt.UserRole)
        if a is None:
            return True
        if b is None:
            return False
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class DataTable(QtWidgets.QTableView):
    """정렬·선택이 되는 표. 행을 고르면 `row_selected` 가 그 dict 를 넘긴다."""

    row_selected = QtCore.Signal(dict)

    def __init__(
        self,
        columns: Sequence[Column],
        *,
        sort_column: int | None = None,
        min_rows: int = 5,
        max_rows: int | None = None,
    ) -> None:
        super().__init__()
        self._min_rows = min_rows
        self._model = TableModel(columns)
        self._proxy = _SortProxy()
        self._proxy.setSourceModel(self._model)
        self.setModel(self._proxy)

        # **`setSortingEnabled(True)` 는 즉시 정렬을 걸어 버린다.** 기본 인디케이터가
        # 0번 열 내림차순이라, 아무것도 지정하지 않으면 표가 열리자마자 첫 열 역순으로
        # 뒤집힌다 — 프로세스 표가 CPU 순이 아니라 **이름 역순**으로 뜨고 있었다.
        # 조회가 이미 원하는 순서로 정렬해 오므로, 지정이 없으면 그 순서를 지킨다.
        self.setSortingEnabled(True)
        if sort_column is None:
            self.horizontalHeader().setSortIndicator(-1, QtCore.Qt.AscendingOrder)
            self._proxy.sort(-1)
        else:
            self.sortByColumn(sort_column, QtCore.Qt.DescendingOrder)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(False)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setStyleSheet(
            f"QTableView {{ background: {theme.SURFACE}; border: 1px solid {theme.GRID};"
            f" border-radius: 8px; gridline-color: {theme.GRID};"
            f" color: {theme.INK_SECONDARY}; }}"
            f"QTableView::item:selected {{ background: {theme.GRID}; color: {theme.INK}; }}"
            f"QHeaderView::section {{ background: {theme.PAGE}; color: {theme.INK_MUTED};"
            f" border: none; border-bottom: 1px solid {theme.GRID}; padding: 6px; }}"
        )
        for index, column in enumerate(columns):
            if column.width:
                self.setColumnWidth(index, column.width)

        # **표에 최소 높이가 없으면 헤더와 한 줄만 남는다.** 레이아웃이 남는 공간을
        # 다른 위젯에 주고 표를 끝까지 누르기 때문인데, 그 상태에서는 표가 있다는
        # 사실만 보이고 내용은 스크롤해야 한다 — 2026-08-06 에 세 페이지가 그랬다.
        #
        # **크기는 픽셀이 아니라 행 수로 다룬다.** 픽셀로 상한을 걸면 폰트·DPI 가
        # 달라질 때 마지막 행이 반쯤 잘리고, 그 값이 최소 높이보다 작아지면 둘이
        # 싸운다 — 2026-08-06 에 `setMaximumHeight(170)` 이 5행(≈187px)을 잘랐다.
        header_h = self.horizontalHeader().sizeHint().height()
        row_h = max(24, self.verticalHeader().defaultSectionSize())

        def height_for(rows: int) -> int:
            return header_h + row_h * rows + 4

        self.setMinimumHeight(height_for(min_rows))
        if max_rows:
            self.setMaximumHeight(height_for(max(max_rows, min_rows)))

        self.selectionModel().selectionChanged.connect(self._emit_selection)

    def set_rows(self, rows: Sequence[dict]) -> None:
        """**선택을 잃지 않는다.** 5초마다 갱신되는 표에서 선택이 풀리면
        사용자가 보고 있던 프로그램의 상세가 사라진다."""
        keep = self.selected_row()
        self._model.set_rows(rows)
        if keep is None:
            return
        first_key = self._model.columns[0].key
        for row_index in range(self._proxy.rowCount()):
            source = self._proxy.mapToSource(self._proxy.index(row_index, 0))
            row = self._model.row_at(source.row())
            if row and row.get(first_key) == keep.get(first_key):
                self.selectRow(row_index)
                return

    def selected_row(self) -> dict | None:
        indexes = self.selectionModel().selectedRows() if self.selectionModel() else []
        if not indexes:
            return None
        source = self._proxy.mapToSource(indexes[0])
        return self._model.row_at(source.row())

    def _emit_selection(self) -> None:
        row = self.selected_row()
        if row is not None:
            self.row_selected.emit(row)


class BarRanking(QtWidgets.QWidget):
    """가로 막대 랭킹.

    **세로가 아니라 가로다.** 프로그램 이름이 길고 개수가 많아 세로 막대는 라벨이
    겹친다 — Streamlit 판에서 같은 이유로 가로를 썼다.
    """

    def __init__(self, title: str, *, unit: str = "") -> None:
        super().__init__()
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        header = QtWidgets.QLabel(title)
        header.setStyleSheet(f"color: {theme.INK}; font-size: 13px; font-weight: 600;")
        box.addWidget(header)

        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=False, alpha=0.15)
        self._plot.setLabel("bottom", unit)
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.hideButtons()
        self._axis = self._plot.getAxis("left")
        box.addWidget(self._plot, stretch=1)
        self._item: pg.BarGraphItem | None = None

    def set_values(self, labels: Sequence[str], values: Sequence[float]) -> None:
        if self._item is not None:
            self._plot.removeItem(self._item)
            self._item = None
        if not labels:
            self._axis.setTicks([])
            return

        # 위에서 아래로 큰 값이 오도록 뒤집는다 (y 가 위로 증가하므로).
        ys = list(range(len(labels)))
        self._item = pg.BarGraphItem(
            x0=0, y=ys, height=0.6, width=list(reversed(values)), brush=theme.SERIES[0]
        )
        self._plot.addItem(self._item)
        self._axis.setTicks([[(y, name) for y, name in zip(ys, reversed(labels))]])
        self._plot.setYRange(-0.5, len(labels) - 0.5)


def section(title: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(title)
    label.setStyleSheet(f"color: {theme.INK_SECONDARY}; font-size: 12px; font-weight: 600;")
    return label


def message(text: str) -> QtWidgets.QLabel:
    """데이터가 없을 때. **빈 화면 대신 왜 비었는지 말한다.**"""
    label = QtWidgets.QLabel(text)
    label.setAlignment(QtCore.Qt.AlignCenter)
    label.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 13px; padding: 24px;")
    return label

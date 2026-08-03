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
from PySide6 import QtCore, QtWidgets

from ..dashboard import theme


class StatTile(QtWidgets.QFrame):
    """큰 수치 하나와 그 아래 보조 설명.

    **보조 설명이 있어야 수치가 뜻을 갖는다.** "디스크 12MB/s" 만으로는 읽기인지
    쓰기인지 모르고, "응답 0.2ms" 는 큐 깊이를 같이 봐야 판단이 선다.
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setMinimumWidth(150)

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
    ) -> None:
        super().__init__()
        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        header = QtWidgets.QLabel(title)
        header.setStyleSheet(f"color: {theme.INK}; font-size: 13px; font-weight: 600;")
        box.addWidget(header)

        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=False, y=True, alpha=0.15)
        self._plot.setLabel("left", unit)
        self._plot.setLabel("bottom", "초 전")
        self._plot.setMouseEnabled(x=False, y=False)  # 실시간 창에서 축이 밀리면 헷갈린다
        self._plot.hideButtons()
        if y_range:
            self._plot.setYRange(*y_range)
        if len(names) > 1:
            self._plot.addLegend(offset=(-10, 10), labelTextColor=theme.INK_SECONDARY)
        box.addWidget(self._plot, stretch=1)

        if note:
            hint = QtWidgets.QLabel(note)
            hint.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 10px;")
            hint.setWordWrap(True)
            box.addWidget(hint)

        self._x: deque[float] = deque(maxlen=maxlen)
        self._series: dict[str, deque[float]] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        for index, name in enumerate(names):
            colour = theme.SERIES[index % len(theme.SERIES)]
            self._series[name] = deque(maxlen=maxlen)
            self._curves[name] = self._plot.plot(
                [], [], pen=pg.mkPen(colour, width=2), name=name
            )

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

    def __init__(self, columns: Sequence[Column], *, sort_column: int | None = None) -> None:
        super().__init__()
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

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

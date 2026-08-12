"""프로세스 — 지금 무엇이 쓰고 있나.

**PID 가 아니라 프로그램 단위로 본다.** 크롬 탭이 30개면 PID 도 30개인데, 사용자가
"크롬이 얼마나 쓰나"를 물을 때 기대하는 답은 30개 중 하나가 아니라 합계다.

**갱신은 5초다.** 실시간 페이지(1초)와 다른 이유: 집계 창이 최소 30초라 1초마다
다시 물어도 값이 거의 그대로다. 관측자가 자기 부하를 만들 이유가 없다(설계 규칙 1).
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ...dashboard import data, theme
from ..widgets import BarRanking, Column, DataTable, TimeSeriesChart, message

# (초, 표시 이름) — Streamlit 판의 집계 창 선택지를 그대로 옮겼다.
_WINDOWS = ((30, "최근 30초"), (60, "최근 1분"), (300, "최근 5분"), (900, "최근 15분"),
            (1800, "최근 30분"))
_RANKING_TOP = 12
_SERIES_S = 1800


class ProcessPoller(QtCore.QThread):
    """집계 조회. 창 길이가 바뀌면 즉시 다시 읽는다."""

    loaded = QtCore.Signal(list)

    def __init__(self, interval_s: float = 5.0) -> None:
        super().__init__()
        self.interval_s = interval_s
        self._window_s = 60
        self._stop = False
        self._wake = QtCore.QSemaphore(0)

    def set_window(self, seconds: int) -> None:
        self._window_s = seconds
        self._wake.release()  # 다음 주기를 기다리지 않고 곧바로 다시 읽는다

    def run(self) -> None:
        while not self._stop:
            try:
                rows = data.top_processes(seconds=self._window_s, limit=20)
                # **설명은 워커에서 붙인다.** 조회 자체는 5분 캐시라 거의 공짜지만,
                # UI 스레드에서 DB 를 만지는 경로를 하나라도 만들지 않는다.
                described = data.program_descriptions()
                rows = [{**row, "description": described.get(row["name"])} for row in rows]
            except Exception:
                rows = []
            self.loaded.emit(rows)
            # 주기 대기 중에도 창 변경에 반응해야 한다.
            self._wake.tryAcquire(1, int(self.interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self._wake.release()
        self.wait(3000)


class SeriesLoader(QtCore.QThread):
    """선택한 프로그램의 시계열. 표 선택마다 새로 뜬다."""

    loaded = QtCore.Signal(str, list)

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def run(self) -> None:
        try:
            rows = data.process_series(self._name, seconds=_SERIES_S)
        except Exception:
            rows = []
        self.loaded.emit(self._name, rows)


class ProcessPage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._selected: str | None = None
        self._loader: SeriesLoader | None = None
        self._loads = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # --- 집계 창 + 포어그라운드
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("집계 창"))
        self._window = QtWidgets.QComboBox()
        for seconds, label in _WINDOWS:
            self._window.addItem(label, seconds)
        self._window.setCurrentIndex(1)  # 최근 1분
        self._window.currentIndexChanged.connect(self._on_window_changed)
        top.addWidget(self._window)
        self._foreground = QtWidgets.QLabel("")
        self._foreground.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        top.addWidget(self._foreground)
        top.addStretch(1)
        outer.addLayout(top)

        # --- 랭킹 + 표
        split = QtWidgets.QHBoxLayout()
        split.setSpacing(12)
        self._ranking = BarRanking("CPU 사용량", unit="%")
        split.addWidget(self._ranking, stretch=2)

        self._table = DataTable(
            [
                Column("name", "프로그램", width=140),
                Column("description", "무슨 프로그램", width=200),
                Column("pids", "PID", align_right=True, width=60),
                Column("cpu", "CPU 평균", fmt=".2f", suffix="%", align_right=True, width=90),
                Column("cpu_max", "CPU 최대", fmt=".1f", suffix="%", align_right=True, width=90),
                Column("rss", "메모리", fmt=",.0f", suffix=" MB", align_right=True, width=100),
                Column("handles", "핸들", fmt=",.0f", align_right=True, width=80),
            ]
        )
        self._table.row_selected.connect(self._on_row_selected)
        split.addWidget(self._table, stretch=3)
        outer.addLayout(split, stretch=3)

        # --- 개별 추이
        self._detail_title = QtWidgets.QLabel("개별 추이 — 표에서 프로그램을 고르세요")
        self._detail_title.setStyleSheet(
            f"color: {theme.INK_SECONDARY}; font-size: 12px; font-weight: 600;"
        )
        outer.addWidget(self._detail_title)

        detail = QtWidgets.QHBoxLayout()
        detail.setSpacing(12)
        self._cpu_chart = TimeSeriesChart("CPU", ["CPU"], unit="%", maxlen=_SERIES_S)
        self._mem_chart = TimeSeriesChart(
            "메모리 · 핸들",
            ["메모리 MB", "핸들"],
            maxlen=_SERIES_S,
            note="누수는 둘이 함께 오른다.",
        )
        detail.addWidget(self._cpu_chart)
        detail.addWidget(self._mem_chart)
        outer.addLayout(detail, stretch=2)

        self._growth = QtWidgets.QLabel("")
        self._growth.setStyleSheet(f"color: {theme.INK_SECONDARY}; font-size: 12px;")
        outer.addWidget(self._growth)

        self._notice = message("프로세스 기록을 기다리는 중…")
        outer.addWidget(self._notice)

        self._poller = ProcessPoller()
        self._poller.loaded.connect(self._on_rows)
        self._poller.start()

    # ------------------------------------------------------------------ 슬롯

    def _on_window_changed(self) -> None:
        self._poller.set_window(int(self._window.currentData()))

    @QtCore.Slot(list)
    def _on_rows(self, rows: list) -> None:
        self._loads += 1
        if not rows:
            self._notice.setVisible(True)
            return
        self._notice.setVisible(False)

        self._table.set_rows(rows)

        top = [r for r in rows if (r.get("cpu") or 0) > 0][:_RANKING_TOP]
        self._ranking.set_values(
            [r["name"] for r in top], [float(r.get("cpu") or 0) for r in top]
        )

        foreground = [r["name"] for r in rows if r.get("foreground")]
        self._foreground.setText(
            "포어그라운드: " + ", ".join(foreground[:3]) if foreground else ""
        )

        # 아직 아무것도 안 골랐으면 1위를 보여 준다 — 빈 차트로 시작하지 않는다.
        if self._selected is None and rows:
            self._table.selectRow(0)

    @QtCore.Slot(dict)
    def _on_row_selected(self, row: dict) -> None:
        name = row.get("name")
        if not name or name == self._selected:
            return
        self._selected = name
        self._detail_title.setText(f"개별 추이 — {name}")
        # 조회는 워커에서. 표를 클릭할 때마다 UI 가 멈추면 안 된다.
        self._loader = SeriesLoader(name)
        self._loader.loaded.connect(self._on_series)
        self._loader.start()

    @QtCore.Slot(str, list)
    def _on_series(self, name: str, rows: list) -> None:
        if name != self._selected:
            return  # 늦게 도착한 이전 선택의 결과는 버린다
        if not rows:
            self._cpu_chart.reset([], {})
            self._mem_chart.reset([], {})
            self._growth.setText("이 프로그램의 시계열이 없습니다.")
            return

        ts = [float(r["ts"]) for r in rows]
        self._cpu_chart.reset(ts, {"CPU": [r.get("cpu") or 0 for r in rows]})
        self._mem_chart.reset(
            ts,
            {
                "메모리 MB": [r.get("rss") or 0 for r in rows],
                "핸들": [r.get("handles") or 0 for r in rows],
            },
        )
        self._growth.setText(_growth_text(rows))

    # ------------------------------------------------------------------ 상태

    @property
    def load_count(self) -> int:
        """검증용 — 표를 몇 번 채웠는가."""
        return self._loads

    def stop(self) -> None:
        self._poller.stop()
        if self._loader is not None:
            self._loader.wait(2000)


def _growth_text(rows: list[dict]) -> str:
    """메모리 증가율. **누수 판정이 아니라 관측이다** — 판정은 `procleak` 이 한다.

    관측 구간이 너무 짧으면 증가율이 잡음이라 아예 말하지 않는다.
    """
    first, last = rows[0], rows[-1]
    span_h = (float(last["ts"]) - float(first["ts"])) / 3600
    if span_h <= 0.05 or not first.get("rss"):
        return ""
    rate = (float(last["rss"]) - float(first["rss"])) / span_h
    return f"메모리 증가율 {rate:+.1f} MB/시간 ({span_h * 60:.0f}분 관측)"

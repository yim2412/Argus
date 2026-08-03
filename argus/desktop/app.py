"""예광탄 — 창 하나, 실시간 차트 하나.

이 파일의 목적은 기능이 아니라 **판정**이다. 다음 넷을 확인하면 본격 이식을 시작할지
정할 수 있고, 아니면 여기서 방향을 바꾼다.

    1. 창이 뜨는가 (고DPI·한글 폰트 포함)
    2. 1초 주기 실시간 갱신이 부드러운가 (Streamlit 이 못 하던 것)
    3. 조회가 UI 를 막지 않는가 (워커 스레드 경계)
    4. exe 로 묶이는가, 그리고 얼마나 커지는가

**조회를 메인 스레드에서 하지 않는다.** SQLite 읽기가 수십 ms 걸리면 그동안 창이
멈춘다. 1초마다 그러면 사용자는 "무거운 프로그램"으로 느끼고, 그건 성능 모니터가
줄 수 있는 최악의 인상이다.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque

from PySide6 import QtCore, QtGui, QtWidgets

from ..dashboard import data, theme

# 실시간 창에 유지할 표본 수. 1초 주기이므로 곧 초 단위 길이다.
_WINDOW_S = 600

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


class _Poller(QtCore.QThread):
    """DB 조회 전담 스레드. **UI 를 막지 않는 것이 유일한 존재 이유다.**"""

    sampled = QtCore.Signal(dict)

    def __init__(self, interval_s: float = 1.0) -> None:
        super().__init__()
        self.interval_s = interval_s
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                latest = data.latest_metrics()
                gpus = data.latest_gpu()
            except Exception:
                latest, gpus = None, []
            if latest:
                self.sampled.emit({"metrics": latest, "gpus": gpus})
            self.msleep(int(self.interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Argus")
        self.resize(1100, 700)

        self._ts: deque[float] = deque(maxlen=_WINDOW_S)
        self._cpu: deque[float] = deque(maxlen=_WINDOW_S)
        self._mem: deque[float] = deque(maxlen=_WINDOW_S)

        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._tiles = _StatRow()
        layout.addWidget(self._tiles)

        # pyqtgraph 는 여기서 import 한다 — Qt 애플리케이션이 만들어진 뒤에 설정을
        # 건드려야 배경색 지정이 먹는다.
        import pyqtgraph as pg

        pg.setConfigOptions(antialias=True, background=theme.SURFACE, foreground=theme.INK_MUTED)
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=False, y=True, alpha=0.15)
        self._plot.setYRange(0, 100)
        self._plot.setLabel("left", "사용률 %")
        self._plot.addLegend(offset=(-10, 10))
        self._cpu_curve = self._plot.plot([], [], pen=pg.mkPen(theme.SERIES[0], width=2), name="CPU")
        self._mem_curve = self._plot.plot(
            [], [], pen=pg.mkPen(theme.SERIES[2], width=2), name="메모리"
        )
        layout.addWidget(self._plot, stretch=1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("연결 중…")

        self._poller = _Poller()
        self._poller.sampled.connect(self._on_sample)
        self._poller.start()

    @QtCore.Slot(dict)
    def _on_sample(self, sample: dict) -> None:
        """워커가 보낸 표본을 그린다. **이 함수는 메인 스레드에서 돈다**(Qt 시그널 규약)."""
        metrics = sample["metrics"]
        ts = float(metrics.get("ts") or time.time())
        if self._ts and ts <= self._ts[-1]:
            return  # 같은 행을 다시 그리지 않는다 (수집이 잠깐 멈춘 경우)

        self._ts.append(ts)
        self._cpu.append(float(metrics.get("cpu_total") or 0.0))
        self._mem.append(float(metrics.get("mem_percent") or 0.0))

        base = self._ts[-1]
        xs = [t - base for t in self._ts]  # 오른쪽 끝이 0(지금)
        self._cpu_curve.setData(xs, list(self._cpu))
        self._mem_curve.setData(xs, list(self._mem))

        gpu = (sample.get("gpus") or [{}])[0]
        self._tiles.update_values(
            cpu=self._cpu[-1],
            mem=self._mem[-1],
            gpu=gpu.get("util_percent"),
            temp=gpu.get("temp_c"),
        )
        age = time.time() - ts
        self.statusBar().showMessage(
            f"표본 {len(self._ts)}개 · 최신 {age:.0f}초 전"
            + ("  — 수집이 멈춘 것 같습니다" if age > 30 else "")
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._poller.stop()
        super().closeEvent(event)


class _StatRow(QtWidgets.QWidget):
    """상단 수치 타일. 지금 값이 한눈에 보여야 한다."""

    def __init__(self) -> None:
        super().__init__()
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self._labels: dict[str, QtWidgets.QLabel] = {}
        for key, title in (("cpu", "CPU"), ("mem", "메모리"), ("gpu", "GPU"), ("temp", "GPU 온도")):
            tile = QtWidgets.QFrame()
            tile.setFrameShape(QtWidgets.QFrame.StyledPanel)
            box = QtWidgets.QVBoxLayout(tile)
            caption = QtWidgets.QLabel(title)
            caption.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
            value = QtWidgets.QLabel("—")
            value.setStyleSheet(f"color: {theme.INK}; font-size: 24px; font-weight: 600;")
            box.addWidget(caption)
            box.addWidget(value)
            self._labels[key] = value
            row.addWidget(tile)

    def update_values(self, *, cpu: float, mem: float, gpu, temp) -> None:
        self._labels["cpu"].setText(f"{cpu:.0f}%")
        self._labels["mem"].setText(f"{mem:.0f}%")
        self._labels["gpu"].setText("—" if gpu is None else f"{gpu:.0f}%")
        self._labels["temp"].setText("—" if temp is None else f"{temp:.0f}°C")


def main(seconds: float | None = None) -> int:
    """`seconds` 를 주면 그만큼 돌고 스스로 닫는다.

    **GUI 검증을 마우스로 하지 않기 위한 것이다**(CLAUDE.md). 창이 떴는지·갱신이
    도는지를 사람이 클릭해서 확인하는 대신, 정해진 시간 동안 받은 표본 수를 숫자로
    돌려준다. 0 이면 조회나 시그널 경계가 깨진 것이다.
    """
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Argus")
    # 어두운 배경을 기본으로. 대시보드 테마와 같은 색을 쓴다 — 두 UI 가 공존하는
    # 동안 색이 갈리면 같은 프로그램으로 보이지 않는다.
    app.setStyleSheet(
        f"QMainWindow, QWidget {{ background: {theme.PAGE}; color: {theme.INK}; }}"
        f"QFrame {{ background: {theme.SURFACE}; border: 1px solid {theme.GRID};"
        f" border-radius: 8px; }}"
        f"QStatusBar {{ color: {theme.INK_MUTED}; }}"
    )

    window = MainWindow()
    placement = place_on_configured_screen(window)
    window.show()
    print(f"  창 위치: {placement}")

    if seconds:
        QtCore.QTimer.singleShot(int(seconds * 1000), app.quit)

    code = app.exec()

    if seconds:
        drawn = len(window._ts)
        print(f"  {seconds:.0f}초 동안 그린 표본 {drawn}개")
        if drawn == 0:
            print("[FAIL] 한 점도 그리지 못했다 — 조회나 시그널 경계가 깨졌다")
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

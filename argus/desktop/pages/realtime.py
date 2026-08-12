"""실시간 — 지금 이 순간과 최근 10분.

**축이 다른 지표를 한 차트에 겹치지 않는다.** CPU %(0~100)와 디스크 MB/s 를 한 그림에
넣으면 두 y 축이 생기고, 그 순간 "어느 선이 어느 축인가"를 읽는 비용이 데이터를 읽는
비용보다 커진다. 단위가 같은 것끼리만 묶고 나머지는 나눈다. (Streamlit 판에서 그대로
가져온 원칙이다.)

**조회는 워커 스레드가 한다.** SQLite 읽기가 수십 ms 걸리는데 그걸 UI 스레드에서 하면
1초마다 창이 멈춘다. 성능 모니터가 스스로 버벅이는 것보다 나쁜 인상은 없다.

**과거는 한 번만 읽는다.** 창을 열 때 최근 10분을 백필하고, 그 뒤로는 새 표본만
덧붙인다. 매 초 600행을 다시 읽으면 관측자가 관측 대상을 오염시킨다(설계 규칙 1).
"""

from __future__ import annotations

import time

from PySide6 import QtCore, QtWidgets

from ...dashboard import data
from ..widgets import (
    MAIN_PLOT_HEIGHT,
    StatTile,
    TimeSeriesChart,
    message,
)

_MB = 1048576.0
_WINDOW_S = 600

# 병목 종류 → 그것이 나타나는 타일. `explain/bottleneck` 이 쓰는 이름 그대로다.
#
# **GPU 와 THERMAL 이 같은 타일인 이유:** 이 창에는 클럭·전력 타일이 없고, 발열
# 스로틀은 GPU 수치(온도)로 보인다. 둘을 나눌 자리가 생기면 그때 나눈다.
# **CONTENTION 은 CPU 다** — 자원 포화 없이 지연되는 것이라 딱 맞는 타일이 없는데,
# 경합이 관측되는 곳은 결국 CPU 대기다. **NONE 은 여기 없다**(강조하지 않는다).
_BOTTLENECK_TILE = {
    "CPU": "cpu",
    "MEMORY": "mem",
    "IO": "disk",
    "GPU": "gpu",
    "THERMAL": "gpu",
    "CONTENTION": "cpu",
}


class RealtimePoller(QtCore.QThread):
    """DB 조회 전담. 첫 회차는 백필, 이후는 최신 한 점."""

    backfilled = QtCore.Signal(dict)
    sampled = QtCore.Signal(dict)

    def __init__(self, interval_s: float = 1.0) -> None:
        super().__init__()
        self.interval_s = interval_s
        self._stop = False

    def run(self) -> None:
        try:
            self.backfilled.emit(
                {
                    "metrics": data.recent_metrics(seconds=_WINDOW_S),
                    "gpu": data.recent_gpu(seconds=_WINDOW_S),
                }
            )
        except Exception:
            # 백필 실패가 실시간 갱신까지 막으면 안 된다. 창이 비어 시작할 뿐이다.
            self.backfilled.emit({"metrics": [], "gpu": []})

        while not self._stop:
            try:
                payload = {"metrics": data.latest_metrics(), "gpu": data.latest_gpu()}
            except Exception:
                payload = {"metrics": None, "gpu": []}
            if payload["metrics"]:
                self.sampled.emit(payload)
            self.msleep(int(self.interval_s * 1000))

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class RealtimePage(QtWidgets.QWidget):
    """수치 타일 5개 + 차트 5개."""

    def __init__(self) -> None:
        super().__init__()
        self._last_ts: float | None = None
        # **백필과 실시간을 따로 센다.** 합쳐 세면 "창을 열었다"와 "갱신이 돈다"가
        # 구분되지 않는다 — 첫 측정에서 608개가 나왔는데 그중 600개가 백필이었고,
        # 실시간이 8개뿐이라는 사실이 그 숫자에 가려져 있었다.
        self._backfilled = 0
        self._live = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # --- 수치 타일
        self._tiles = {
            key: StatTile(title)
            for key, title in (
                ("cpu", "CPU"),
                ("mem", "메모리"),
                ("disk", "디스크"),
                ("resp", "디스크 응답"),
                ("gpu", "GPU"),
            )
        }
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        for tile in self._tiles.values():
            row.addWidget(tile)
        outer.addLayout(row)

        # --- 차트
        # 이 페이지의 주 차트. 나머지 넷보다 크게 잡는다 — 여기서 "지금 무슨 일이
        # 일어나는가"를 먼저 읽는다.
        self._cpu_chart = TimeSeriesChart(
            "CPU · 메모리",
            ["CPU 전체", "최다 코어", "메모리"],
            unit="%",
            y_range=(0, 100),
            min_height=MAIN_PLOT_HEIGHT
        )
        outer.addWidget(self._cpu_chart, stretch=2)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(12)
        self._disk_chart = TimeSeriesChart(
            "디스크 처리량", ["읽기", "쓰기"], unit="MB/s"
        )
        self._resp_chart = TimeSeriesChart(
            "디스크 응답시간",
            ["응답"],
            unit="ms",
            note="사용률은 원인이고 응답시간이 증상이다. 증상 없는 원인은 알릴 가치가 없다."
        )
        self._net_chart = TimeSeriesChart(
            "네트워크", ["수신", "송신"], unit="MB/s"
        )
        self._gpu_chart = TimeSeriesChart(
            "GPU", ["사용률 %", "온도 °C"], unit=""
        )
        grid.addWidget(self._disk_chart, 0, 0)
        grid.addWidget(self._resp_chart, 0, 1)
        grid.addWidget(self._net_chart, 1, 0)
        grid.addWidget(self._gpu_chart, 1, 1)
        # **행 비율을 명시한다.** 안 하면 `QGridLayout` 이 sizeHint 대로 나눠,
        # 주석이 붙은 행 하나가 다른 행의 세 배가 된다(2026-08-06 실측 480 vs 150).
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        outer.addLayout(grid, stretch=3)

        self._notice = message("수집된 메트릭을 기다리는 중…")
        outer.addWidget(self._notice)

        self._poller = RealtimePoller()
        self._poller.backfilled.connect(self._on_backfill)
        self._poller.sampled.connect(self._on_sample)
        self._poller.start()

    # ------------------------------------------------------------------ 슬롯
    #
    # 아래 둘은 **메인 스레드에서 돈다**(Qt 시그널 규약). 위젯을 워커에서 직접
    # 건드리면 조용히 깨지거나 죽는다.

    @QtCore.Slot(dict)
    def _on_backfill(self, payload: dict) -> None:
        rows = payload.get("metrics") or []
        if rows:
            ts = [float(r["ts"]) for r in rows]
            self._cpu_chart.reset(
                ts,
                {
                    "CPU 전체": [r.get("cpu_total") or 0 for r in rows],
                    "최다 코어": [r.get("cpu_max_core") or 0 for r in rows],
                    "메모리": [r.get("mem_percent") or 0 for r in rows],
                },
            )
            self._disk_chart.reset(
                ts,
                {
                    "읽기": [(r.get("disk_read_bps") or 0) / _MB for r in rows],
                    "쓰기": [(r.get("disk_write_bps") or 0) / _MB for r in rows],
                },
            )
            self._resp_chart.reset(ts, {"응답": [r.get("disk_resp_ms") or 0 for r in rows]})
            self._net_chart.reset(
                ts,
                {
                    "수신": [(r.get("net_rx_bps") or 0) / _MB for r in rows],
                    "송신": [(r.get("net_tx_bps") or 0) / _MB for r in rows],
                },
            )
            self._last_ts = ts[-1]
            self._backfilled = len(ts)

        gpu_rows = payload.get("gpu") or []
        if gpu_rows:
            self._gpu_chart.reset(
                [float(r["ts"]) for r in gpu_rows],
                {
                    "사용률 %": [r.get("util_percent") or 0 for r in gpu_rows],
                    "온도 °C": [r.get("temp_c") or 0 for r in gpu_rows],
                },
            )

    @QtCore.Slot(dict)
    def _on_sample(self, payload: dict) -> None:
        metrics = payload["metrics"]
        ts = float(metrics.get("ts") or time.time())
        if self._last_ts is not None and ts <= self._last_ts:
            return  # 수집이 잠시 멈추면 같은 행이 계속 온다. 다시 그리지 않는다.
        self._last_ts = ts
        self._live += 1
        self._notice.setVisible(False)

        self._cpu_chart.append(
            ts,
            {
                "CPU 전체": metrics.get("cpu_total"),
                "최다 코어": metrics.get("cpu_max_core"),
                "메모리": metrics.get("mem_percent"),
            },
        )
        read = (metrics.get("disk_read_bps") or 0) / _MB
        write = (metrics.get("disk_write_bps") or 0) / _MB
        self._disk_chart.append(ts, {"읽기": read, "쓰기": write})
        self._resp_chart.append(ts, {"응답": metrics.get("disk_resp_ms")})
        self._net_chart.append(
            ts,
            {
                "수신": (metrics.get("net_rx_bps") or 0) / _MB,
                "송신": (metrics.get("net_tx_bps") or 0) / _MB,
            },
        )

        gpus = payload.get("gpu") or []
        gpu = gpus[0] if gpus else {}
        if gpus:
            self._gpu_chart.append(
                ts, {"사용률 %": gpu.get("util_percent"), "온도 °C": gpu.get("temp_c")}
            )

        self._update_tiles(metrics, gpu, read, write)

    def _update_tiles(self, metrics: dict, gpu: dict, read: float, write: float) -> None:
        def number(value, fmt: str, fallback: str = "—") -> str:
            return fallback if value is None else format(value, fmt)

        self._tiles["cpu"].set(
            number(metrics.get("cpu_total"), ".1f") + "%",
            f"최다 코어 {metrics['cpu_max_core']:.0f}%" if metrics.get("cpu_max_core") else "",
        )
        avail = metrics.get("mem_avail_mb")
        self._tiles["mem"].set(
            number(metrics.get("mem_percent"), ".1f") + "%",
            f"여유 {avail / 1024:.1f} GB" if avail else "",
        )
        self._tiles["disk"].set(f"{read + write:.1f} MB/s", f"읽기 {read:.1f} · 쓰기 {write:.1f}")
        resp = metrics.get("disk_resp_ms")
        self._tiles["resp"].set(
            "—" if resp is None else f"{resp:.2f} ms",
            f"큐 {metrics['disk_queue']:.1f}" if metrics.get("disk_queue") is not None else "",
        )
        if gpu:
            vram = gpu.get("vram_used_mb")
            temp = gpu.get("temp_c")
            note = []
            if temp is not None:
                note.append(f"{temp:.0f}°C")
            if vram:
                note.append(f"VRAM {vram / 1024:.1f}GB")
            self._tiles["gpu"].set(number(gpu.get("util_percent"), ".0f") + "%", " · ".join(note))
        else:
            self._tiles["gpu"].set("없음", "NVML 미탑재")

    @QtCore.Slot(dict)
    def mark_bottleneck(self, health: dict) -> None:
        """진행 중 사건의 병목에 해당하는 타일만 강조한다.

        **판정을 여기서 다시 하지 않는다.** 창은 "무엇이 병목인가"를 이미 답으로
        받았고(`explain/bottleneck` → `incidents.bottleneck`), 여기서 하는 일은
        그 답을 어느 타일 위에 놓을지 고르는 것뿐이다. 값을 보고 색을 정하면
        하드웨어마다 틀리고(설계 규칙 2) 맨 윗줄과 갈린다.
        """
        incident = health.get("open") or {}
        key = _BOTTLENECK_TILE.get(str(incident.get("bottleneck") or ""))
        severity = str(incident.get("severity") or "warning") if key else None
        for name, tile in self._tiles.items():
            tile.mark(severity if name == key else None)

    # ------------------------------------------------------------------ 상태

    @property
    def sample_count(self) -> int:
        """**실시간으로 받은 표본만.** 백필은 세지 않는다 — 이 값이 판정 근거다."""
        return self._live

    @property
    def backfill_count(self) -> int:
        return self._backfilled

    def status_text(self) -> str:
        if self._last_ts is None:
            return "수집된 메트릭 없음"
        age = time.time() - self._last_ts
        suffix = "  — 수집이 멈춘 것 같습니다" if age > 30 else ""
        total = self._backfilled + self._live
        return f"표본 {total}개 · 최신 {age:.0f}초 전{suffix}"

    def stop(self) -> None:
        self._poller.stop()

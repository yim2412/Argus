"""타임라인 — 1분 집계로 본 긴 구간.

**실시간 페이지와 데이터 성격이 다르다.** 저기는 10분/초 단위라 "몇 초 전"으로 그리고
새 표본을 덧붙이지만, 여기는 최대 일주일/분 단위다. 구간을 바꾸면 통째로 다시 읽는
편이 맞고(증분이 의미 없다), x 축도 절대 시각이어야 읽힌다.

**웜(Parquet)까지 읽는다.** `metrics_1m` 만 보면 이틀 지난 날짜가 통째로 빈다 —
내보낸 뒤 SQLite 에서 지워지기 때문이다. `data.rollup()` 이 `history` 를 거쳐 합쳐 준다.

**오버레이가 이 페이지의 값이다.** 결함 주입 구간과 탐지 신호를 지표 위에 겹쳐야
"그때 무엇을 넣었고 무엇이 울렸나"가 한눈에 보인다.
"""

from __future__ import annotations

from datetime import datetime

from PySide6 import QtCore, QtWidgets

from ...dashboard import data, theme
from ..widgets import (
    MAIN_PLOT_HEIGHT,
    Column,
    DataTable,
    HistoryChart,
    message,
)

_MB = 1048576.0
_HOUR_CHOICES = ((1, "1시간"), (3, "3시간"), (6, "6시간"), (12, "12시간"),
                 (24, "24시간"), (72, "3일"), (168, "7일"))


class TimelineLoader(QtCore.QThread):
    """구간 하나를 통째로 읽는다. **웜까지 훑으므로 UI 스레드에서 하면 안 된다.**"""

    loaded = QtCore.Signal(dict)

    def __init__(self, hours: int) -> None:
        super().__init__()
        self._hours = hours

    def run(self) -> None:
        payload = {"rows": [], "faults": [], "signals": [], "hours": self._hours}
        try:
            payload["rows"] = data.rollup(hours=self._hours)
            payload["faults"] = data.fault_injections(hours=self._hours)
            payload["signals"] = data.anomaly_signals(hours=self._hours)
        except Exception:
            pass  # 빈 화면과 안내로 떨어진다 — 창이 죽는 것보다 낫다
        self.loaded.emit(payload)


class TimelinePage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._loader: TimelineLoader | None = None
        self._loads = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("구간"))
        self._hours = QtWidgets.QComboBox()
        for hours, label in _HOUR_CHOICES:
            self._hours.addItem(label, hours)
        self._hours.setCurrentIndex(2)  # 6시간
        self._hours.currentIndexChanged.connect(self._reload)
        top.addWidget(self._hours)
        self._legend = QtWidgets.QLabel("")
        self._legend.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        top.addWidget(self._legend)
        top.addStretch(1)
        outer.addLayout(top)

        self._cpu = HistoryChart(
            "CPU — 평균과 흔들림",
            ["평균", "최대", "표준편차"],
            unit="%",
            note="표준편차가 크면 같은 평균이라도 다른 상황이다 — "
            "고르게 눌린 부하와 튀는 부하는 다르다.",
            min_height=MAIN_PLOT_HEIGHT,
        )
        outer.addWidget(self._cpu, stretch=3)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(12)
        self._mem = HistoryChart("메모리", ["평균", "최대"], unit="%")
        self._resp = HistoryChart(
            "디스크 응답", ["평균", "p95"], unit="ms"
        )
        self._disk = HistoryChart(
            "디스크 처리량", ["읽기", "쓰기"], unit="MB/s"
        )
        self._gpu = HistoryChart("GPU", ["평균", "최대"], unit="%")
        grid.addWidget(self._mem, 0, 0)
        grid.addWidget(self._resp, 0, 1)
        grid.addWidget(self._disk, 1, 0)
        grid.addWidget(self._gpu, 1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        outer.addLayout(grid, stretch=4)

        # --- 무엇을 하고 있었나 + 주입 이력
        tables = QtWidgets.QHBoxLayout()
        tables.setSpacing(12)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("무엇을 하고 있었나"))
        self._foreground = DataTable(
            [
                Column("name", "프로그램", width=170),
                Column("minutes", "분", align_right=True, width=60),
                Column("share", "비중", fmt=".0f", suffix="%", align_right=True, width=70),
            ],
            max_rows=6,
        )
        left.addWidget(self._foreground)
        tables.addLayout(left)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("결함 주입 이력"))
        self._faults = DataTable(
            [
                Column("scenario", "시나리오", width=120),
                Column("started", "시작", width=120),
                Column("length", "길이", align_right=True, width=70),
                Column("observed", "증상 관측", width=110),
            ],
            max_rows=6,
        )
        right.addWidget(self._faults)
        tables.addLayout(right)
        outer.addLayout(tables, stretch=2)

        self._notice = message("1분 집계를 불러오는 중…")
        outer.addWidget(self._notice)

        self._reload()

    # ------------------------------------------------------------------ 조회

    def _reload(self) -> None:
        self._loader = TimelineLoader(int(self._hours.currentData()))
        self._loader.loaded.connect(self._on_loaded)
        self._loader.start()

    @QtCore.Slot(dict)
    def _on_loaded(self, payload: dict) -> None:
        self._loads += 1
        rows = payload.get("rows") or []
        if not rows:
            self._notice.setText(
                "1분 집계가 없습니다. 롤업은 수집 시작 후 약 2분 뒤부터 채워집니다."
            )
            self._notice.setVisible(True)
            return
        self._notice.setVisible(False)

        ts = [float(r["ts_min"]) for r in rows]

        def column(key: str, scale: float = 1.0) -> list[float]:
            return [(r.get(key) or 0) * scale for r in rows]

        self._cpu.set_data(
            ts,
            {"평균": column("cpu_mean"), "최대": column("cpu_max"), "표준편차": column("cpu_std")},
        )
        self._mem.set_data(
            ts,
            {"평균": column("mem_percent_mean"), "최대": column("mem_percent_max")},
        )
        self._resp.set_data(
            ts,
            {"평균": column("disk_resp_ms_mean"), "p95": column("disk_resp_ms_p95")},
        )
        self._disk.set_data(
            ts,
            {
                "읽기": column("disk_read_bps_mean", 1 / _MB),
                "쓰기": column("disk_write_bps_mean", 1 / _MB),
            },
        )
        self._gpu.set_data(
            ts, {"평균": column("gpu_util_mean"), "최대": column("gpu_util_max")}
        )

        self._apply_overlays(payload.get("faults") or [], payload.get("signals") or [])
        self._fill_foreground(rows)
        self._fill_faults(payload.get("faults") or [])

    def _apply_overlays(self, faults: list[dict], signals: list[dict]) -> None:
        bands = [
            {
                "lo": float(f["ts_start"]),
                "hi": float(f["ts_end"]),
                "strong": bool(f.get("completed")),
            }
            for f in faults
            if f.get("ts_end")
        ]
        marks = [float(s["ts"]) for s in signals]
        for chart in (self._cpu, self._mem, self._resp, self._disk, self._gpu):
            chart.set_overlays(bands, marks if chart is self._cpu else [])

        bits = []
        if faults:
            bits.append(f"▮ 결함 주입 구간 (정답 라벨) {len(faults)}건")
        if signals:
            bits.append(f"┆ 탐지 신호 {len(signals)}건")
        self._legend.setText(
            " · ".join(bits) if bits else "이 구간에는 결함 주입도 탐지 신호도 없습니다."
        )

    def _fill_foreground(self, rows: list[dict]) -> None:
        """**리소스 수치만으로는 '무엇을 하는 중인가'를 알 수 없다.**
        이 표가 Phase 4-B 레짐 추론의 입력이다."""
        counts: dict[str, int] = {}
        for row in rows:
            name = row.get("foreground_proc")
            if name:
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            self._foreground.set_rows([])
            return
        total = sum(counts.values())
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
        self._foreground.set_rows(
            [
                {"name": name, "minutes": count, "share": count / total * 100}
                for name, count in top
            ]
        )

    def _fill_faults(self, faults: list[dict]) -> None:
        self._faults.set_rows(
            [
                {
                    "scenario": f.get("scenario") or "?",
                    "started": datetime.fromtimestamp(float(f["ts_start"])).strftime(
                        "%m-%d %H:%M:%S"
                    ),
                    "length": (
                        f"{(float(f['ts_end']) - float(f['ts_start'])) / 60:.1f}분"
                        if f.get("ts_end")
                        else "미완"
                    ),
                    # 증상이 관측되지 않은 주입은 채점에서 빠진다 — 그 사실을 표에도 적는다.
                    "observed": "관측됨" if f.get("completed") else "없음 (채점 제외)",
                }
                for f in reversed(faults)
            ]
        )

    # ------------------------------------------------------------------ 상태

    @property
    def load_count(self) -> int:
        return self._loads

    @property
    def overlay_count(self) -> int:
        return self._cpu.overlay_count

    def stop(self) -> None:
        if self._loader is not None:
            self._loader.wait(5000)

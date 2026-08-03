"""자기 상태 — 관측자가 병목이 되고 있지 않은가.

**모니터가 병목이 되는 순간 제품은 실패다**(설계 규칙 1). 이 페이지는 그 규칙이
지켜지고 있는지를 보여 준다 — 예산(CPU 2% / RSS 300MB) 대비 지금 어디인지.

**누수 판정의 정본은 `private` 이다.** RSS 는 Windows 의 워킹셋 트림에 따라 내려가서,
실제로 메모리를 쥐고 있어도 줄어든 것처럼 보인다(실측: 강제 트림으로 RSS 95.8 → 1.0MB,
private 은 85.4MB 유지). 그래서 둘을 같이 그리고 증가율은 private 으로 잰다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from PySide6 import QtCore, QtWidgets

from ...dashboard import data, theme
from ..widgets import Column, DataTable, HistoryChart, StatTile, message

_HOUR_CHOICES = ((1, "1시간"), (2, "2시간"), (4, "4시간"), (8, "8시간"),
                 (24, "24시간"), (72, "3일"))
_CPU_BUDGET = 2.0


class SelfStateLoader(QtCore.QThread):
    loaded = QtCore.Signal(dict)

    def __init__(self, hours: int) -> None:
        super().__init__()
        self._hours = hours

    def run(self) -> None:
        payload: dict = {"rows": [], "storage": {}, "events": [], "runs": []}
        try:
            payload["rows"] = data.self_telemetry(hours=self._hours)
            payload["events"] = data.system_events(hours=self._hours)
            payload["runs"] = data.eval_runs(limit=12)
            payload["storage"] = {
                "db_bytes": data.db_size_bytes(),
                "warm_bytes": data.warm_size_bytes(),
                "warm_span": data.warm_span(),
                "rollup_state": data.rollup_state(),
                "rollup_span": data.rollup_span(),
                "tables": data.table_counts(),
            }
        except Exception:
            pass
        self.loaded.emit(payload)


class SelfStatePage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._loader: SelfStateLoader | None = None
        self._loads = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("구간"))
        self._hours = QtWidgets.QComboBox()
        for hours, label in _HOUR_CHOICES:
            self._hours.addItem(label, hours)
        self._hours.setCurrentIndex(3)  # 8시간
        self._hours.currentIndexChanged.connect(self._reload)
        top.addWidget(self._hours)
        top.addStretch(1)
        outer.addLayout(top)

        self._tiles = {
            key: StatTile(title)
            for key, title in (
                ("cpu", "CPU"),
                ("private", "private"),
                ("rss", "RSS"),
                ("drop", "유실"),
                ("handles", "핸들"),
            )
        }
        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(10)
        for tile in self._tiles.values():
            tiles.addWidget(tile)
        outer.addLayout(tiles)

        # **경고는 눈에 띄어야 한다.** 유실과 스로틀은 규칙 1 이 깨지고 있다는 신호다.
        self._alert = QtWidgets.QLabel("")
        self._alert.setWordWrap(True)
        self._alert.setStyleSheet(
            f"color: {theme.STATUS['warning']}; font-size: 12px;"
            f" border: 1px solid {theme.STATUS['warning']}; border-radius: 6px; padding: 8px;"
        )
        self._alert.setVisible(False)
        outer.addWidget(self._alert)

        self._memory = HistoryChart(
            "메모리 — RSS 와 private",
            ["private (커밋)", "RSS (물리)", "peak working set"],
            unit="MB",
            note="누수는 여기서 보인다. RSS 가 내려가도 private 이 오르면 실제로는 쓰고 있는 것이다.",
        )
        outer.addWidget(self._memory, stretch=3)

        self._growth = QtWidgets.QLabel("")
        self._growth.setStyleSheet(f"color: {theme.INK_SECONDARY}; font-size: 12px;")
        outer.addWidget(self._growth)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        self._cpu = HistoryChart("CPU (예산 2%)", ["CPU"], unit="%")
        self._io = HistoryChart("쓰기 지연 · 큐 깊이", ["쓰기 지연 ms", "큐 깊이"])
        row.addWidget(self._cpu)
        row.addWidget(self._io)
        outer.addLayout(row, stretch=2)

        # --- 저장소
        outer.addWidget(QtWidgets.QLabel("저장소"))
        self._storage_tiles = {
            key: StatTile(title)
            for key, title in (
                ("db", "DB"),
                ("warm", "웜 스토어"),
                ("lag", "롤업 지연"),
                ("buckets", "1분 집계"),
            )
        }
        storage = QtWidgets.QHBoxLayout()
        storage.setSpacing(10)
        for tile in self._storage_tiles.values():
            storage.addWidget(tile)
        outer.addLayout(storage)

        self._tables = DataTable(
            [
                Column("table", "테이블", width=150),
                Column("rows", "행", fmt=",.0f", align_right=True, width=90),
                Column("from", "시작", width=110),
                Column("to", "끝", width=110),
                Column("held", "보유", fmt=".1f", suffix="h", align_right=True, width=70),
            ]
        )
        self._tables.setMaximumHeight(200)
        outer.addWidget(self._tables, stretch=2)

        hint = QtWidgets.QLabel(
            "이 표는 핫 저장소(SQLite) 보유분입니다. 롤업은 이틀이 지나면 웜 스토어"
            "(Parquet)로 옮겨가므로 여기서는 보유 시간이 줄어듭니다 — 사라지는 것이 "
            "아니라 위 '웜 스토어'로 이동합니다."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 10px;")
        outer.addWidget(hint)

        # --- 시스템 사건 + 스코어보드
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("시스템 사건"))
        self._events = DataTable(
            [
                Column("when", "시각", width=120),
                Column("event", "사건", width=150),
                Column("gap", "공백(초)", fmt=".0f", align_right=True, width=80),
                Column("cause", "추정 원인", width=130),
            ]
        )
        self._events.setMaximumHeight(180)
        left.addWidget(self._events)
        bottom.addLayout(left)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("탐지기 스코어보드"))
        self._runs = DataTable(
            [
                Column("detector", "탐지기", width=110),
                Column("f1", "F1", fmt=".3f", align_right=True, width=60),
                Column("precision", "정밀도", fmt=".0f", suffix="%", align_right=True, width=70),
                Column("recall", "재현율", fmt=".0f", suffix="%", align_right=True, width=70),
                Column("fp_per_hour", "오탐/h", fmt=".2f", align_right=True, width=70),
                Column("when", "실행", width=100),
            ]
        )
        self._runs.setMaximumHeight(180)
        right.addWidget(self._runs)
        bottom.addLayout(right)
        outer.addLayout(bottom, stretch=2)

        self._notice = message("자기 계측 기록을 불러오는 중…")
        outer.addWidget(self._notice)

        self._reload()

    # ------------------------------------------------------------------ 조회

    def _reload(self) -> None:
        self._loader = SelfStateLoader(int(self._hours.currentData()))
        self._loader.loaded.connect(self._on_loaded)
        self._loader.start()

    @QtCore.Slot(dict)
    def _on_loaded(self, payload: dict) -> None:
        self._loads += 1
        rows = payload.get("rows") or []
        self._fill_storage(payload.get("storage") or {})
        self._fill_events(payload.get("events") or [])
        self._fill_runs(payload.get("runs") or [])
        if not rows:
            self._notice.setText(
                "자기 계측 기록이 없습니다. `python -m argus` 로 수집을 시작하세요."
            )
            self._notice.setVisible(True)
            return
        self._notice.setVisible(False)

        latest = rows[-1]
        ts = [float(r["ts"]) for r in rows]

        def column(key: str) -> list[float]:
            return [float(r.get(key) or 0) for r in rows]

        self._memory.set_data(
            ts,
            {
                "private (커밋)": column("private_mb"),
                "RSS (물리)": column("rss_mb"),
                "peak working set": column("peak_wset_mb"),
            },
        )
        self._cpu.set_data(ts, {"CPU": column("cpu_percent")})
        self._io.set_data(
            ts,
            {"쓰기 지연 ms": column("write_latency_ms"), "큐 깊이": column("queue_depth")},
        )

        self._fill_tiles(latest)
        self._growth.setText(_private_growth(rows))
        self._fill_alert(rows, latest)

    def _fill_tiles(self, latest: dict) -> None:
        cpu = float(latest.get("cpu_percent") or 0.0)
        self._tiles["cpu"].set(f"{cpu:.2f}%", f"예산의 {cpu / _CPU_BUDGET * 100:.0f}%")
        private = latest.get("private_mb")
        self._tiles["private"].set(
            f"{private:.0f} MB" if private else "—", "누수 판정의 정본"
        )
        self._tiles["rss"].set(f"{float(latest.get('rss_mb') or 0):.0f} MB", "예산 300MB")
        drops = int(latest.get("drop_count") or 0)
        self._tiles["drop"].set(f"{drops:,}", "0 이어야 한다")
        handles = latest.get("handles")
        self._tiles["handles"].set(f"{handles:,}" if handles else "—")

    def _fill_alert(self, rows: list[dict], latest: dict) -> None:
        """**규칙 1 이 깨지고 있다는 신호만 띄운다.** 평소에는 조용해야 한다."""
        messages = []
        drops = int(latest.get("drop_count") or 0)
        if drops:
            messages.append(
                f"수집 유실 {drops:,}행 — 큐가 가득 차 오래된 표본을 버렸습니다. "
                "저장이 수집을 못 따라가고 있습니다."
            )
        throttled = [r for r in rows if r.get("throttle_level")]
        if throttled:
            worst = max(int(r["throttle_level"]) for r in throttled)
            messages.append(
                f"스로틀이 걸린 표본 {len(throttled)}개 (최대 레벨 {worst}) — "
                "예산 초과로 수집 주기를 늦췄습니다."
            )
        self._alert.setText("\n".join(messages))
        self._alert.setVisible(bool(messages))

    def _fill_storage(self, storage: dict) -> None:
        db_bytes = storage.get("db_bytes") or 0
        self._storage_tiles["db"].set(f"{db_bytes / 1048576:.1f} MB")

        span = storage.get("warm_span")
        warm_bytes = storage.get("warm_bytes") or 0
        if span:
            size = (
                f"{warm_bytes / 1024:.0f} KB"
                if warm_bytes < 1048576
                else f"{warm_bytes / 1048576:.1f} MB"
            )
            self._storage_tiles["warm"].set(
                f"{span['days']}일치", f"{span['lo'][5:]}~{span['hi'][5:]} · {size}"
            )
        else:
            self._storage_tiles["warm"].set("—", "아직 내보낸 날짜가 없습니다")

        state = storage.get("rollup_state")
        if state:
            lag_min = (time.time() - float(state["watermark_ts"])) / 60
            self._storage_tiles["lag"].set(f"{lag_min:.0f}분", "정상 범위 2~3분")
        else:
            # **롤업이 멈추면 원본 정리도 함께 멈춘다** — 접히기 전에 지우면 그 구간은
            # 어디에도 남지 않기 때문이다. 그래서 이 칸이 비면 그 자체가 경고다.
            self._storage_tiles["lag"].set("—", "아직 실행되지 않음")

        rollup_span = storage.get("rollup_span")
        self._storage_tiles["buckets"].set(
            f"{rollup_span['n']:,}분" if rollup_span else "—"
        )

        self._tables.set_rows(
            [
                {
                    "table": row["table"],
                    "rows": row["n"],
                    "from": datetime.fromtimestamp(row["lo"]).strftime("%m-%d %H:%M"),
                    "to": datetime.fromtimestamp(row["hi"]).strftime("%m-%d %H:%M"),
                    "held": (row["hi"] - row["lo"]) / 3600,
                }
                for row in (storage.get("tables") or [])
            ]
        )

    def _fill_events(self, events: list[dict]) -> None:
        """절전·재부팅·크래시 이력.

        **사건 이름만으로는 "왜"가 안 보인다.** 절전인지 재부팅인지 강제 종료인지가
        사후 진단의 전부라, `detail` 안의 추정 원인을 끌어올린다.
        """
        self._events.set_rows(
            [
                {
                    "when": datetime.fromtimestamp(float(e["ts"])).strftime("%m-%d %H:%M:%S"),
                    "event": e.get("event") or "",
                    "gap": e.get("gap_seconds"),
                    "cause": _cause_ko(e.get("detail")),
                }
                for e in events[:15]
            ]
        )

    def _fill_runs(self, runs: list[dict]) -> None:
        """탐지기 채점 이력. `python -m argus.eval --detector all --save` 로 갱신된다."""
        self._runs.set_rows(
            [
                {
                    "detector": r.get("detector") or "",
                    "f1": r.get("f1"),
                    "precision": r.get("precision_pct"),
                    "recall": r.get("recall_pct"),
                    "fp_per_hour": r.get("fp_per_hour"),
                    "when": datetime.fromtimestamp(float(r["ts"])).strftime("%m-%d %H:%M"),
                }
                for r in runs
            ]
        )

    # ------------------------------------------------------------------ 상태

    @property
    def load_count(self) -> int:
        return self._loads

    def stop(self) -> None:
        if self._loader is not None:
            self._loader.wait(5000)


_CAUSE_KO = {
    "suspend_or_stall": "절전·정지",
    "clock_change": "시각 변경",
    "clock_backwards": "시각 역행",
    "reboot_or_power_loss": "재부팅·전원 차단",
    "process_killed_or_crash": "강제 종료·크래시",
    "unknown": "불명",
}


def _cause_ko(detail: str | None) -> str:
    """`detail` JSON 에서 추정 원인을 사람 말로. 못 읽으면 조용히 빈 칸이다 —
    진단 보조 정보 하나 때문에 표 전체가 비면 안 된다."""
    if not detail:
        return ""
    try:
        cause = json.loads(detail).get("likely_cause")
    except (ValueError, AttributeError, TypeError):
        return ""
    return _CAUSE_KO.get(cause, cause or "")


def _private_growth(rows: list[dict]) -> str:
    """private 증가율. **RSS 가 아니라 private 으로 잰다.**

    RSS 는 워킹셋 트림에 따라 내려가서 누수를 가린다 — 2026-07-27 에 RSS 가 63 → 18MB
    로 내려간 것이 반납이 아니라 트림이었다.
    """
    with_private = [r for r in rows if r.get("private_mb") is not None]
    if len(with_private) < 2:
        return ""
    first, last = with_private[0], with_private[-1]
    span_h = (float(last["ts"]) - float(first["ts"])) / 3600
    if span_h <= 0.2:
        return ""
    delta = float(last["private_mb"]) - float(first["private_mb"])
    return (
        f"private 증가율 {delta / span_h:+.2f} MB/시간 "
        f"({span_h:.1f}시간 동안 {delta:+.1f}MB)"
    )

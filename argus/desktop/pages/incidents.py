"""사건 — 어제 왜 느렸는지.

**이 페이지가 Argus 의 산출물이다.** 수집도 탐지도 여기서 문장이 되지 않으면 사용자에게
닿지 않는다("탐지가 아니라 설명이 제품이다").

Streamlit 판은 사건마다 `expander` 를 폈다 접었다 했다. 네이티브에서는 **좌측 목록 +
우측 상세**로 간다 — 목록을 훑으며 상세를 갈아 끼우는 편이 "어느 사건이 문제였나"를
찾는 동작에 맞고, 접힌 것을 하나씩 여는 수고가 없다.

**마크다운은 `QTextBrowser` 가 그린다.** 리포트가 이미 마크다운이라 다시 쓸 필요가
없었다 — 이식 전 이것이 유일한 미지수였다.
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6 import QtCore, QtWidgets

from ...dashboard import data, theme
from ..widgets import Column, DataTable, StatTile, message

_DAY_CHOICES = ((1, "1일"), (3, "3일"), (7, "7일"), (30, "30일"))

_SEVERITY_LABEL = {"critical": "심각", "warning": "경고", "info": "정보"}


class IncidentLoader(QtCore.QThread):
    """사건 목록 조회. 구간이 바뀌면 다시 뜬다."""

    loaded = QtCore.Signal(list)

    def __init__(self, days: int) -> None:
        super().__init__()
        self._days = days

    def run(self) -> None:
        try:
            rows = data.incidents(days=self._days)
        except Exception:
            rows = []
        self.loaded.emit(rows)


class IncidentPage(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._selected_id: int | None = None
        # 알림에서 넘어온 사건. 목록이 도착하기 전에는 고를 수 없으므로 들고 있는다.
        self._pending_id: int | None = None
        self._widened = False
        self._loader: IncidentLoader | None = None
        self._loads = 0

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # --- 구간 + 요약
        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("구간"))
        self._days = QtWidgets.QComboBox()
        for days, label in _DAY_CHOICES:
            self._days.addItem(label, days)
        self._days.setCurrentIndex(2)  # 7일
        self._days.currentIndexChanged.connect(self._reload)
        top.addWidget(self._days)
        top.addStretch(1)
        outer.addLayout(top)

        self._tiles = {
            key: StatTile(title)
            for key, title in (
                ("total", "사건"),
                ("open", "진행 중"),
                ("notified", "알림 대상"),
                ("fp", "오탐 비율"),
            )
        }
        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(10)
        for tile in self._tiles.values():
            tiles.addWidget(tile)
        outer.addLayout(tiles)

        # --- 목록 + 상세
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._table = DataTable(
            [
                Column("when", "시각", width=130),
                Column("severity_ko", "등급", width=60),
                Column("span", "지속", align_right=True, width=70),
                Column("title", "제목", width=280),
            ]
        )
        self._table.row_selected.connect(self._on_row_selected)
        split.addWidget(self._table)
        split.addWidget(self._build_detail())
        split.setSizes([560, 640])
        outer.addWidget(split, stretch=1)

        self._notice = message("사건을 불러오는 중…")
        outer.addWidget(self._notice)

        self._reload()

    def _build_detail(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(panel)
        box.setContentsMargins(12, 0, 0, 0)
        box.setSpacing(8)

        self._detail_head = QtWidgets.QLabel("왼쪽에서 사건을 고르세요")
        self._detail_head.setStyleSheet(f"color: {theme.INK}; font-size: 14px; font-weight: 600;")
        self._detail_head.setWordWrap(True)
        box.addWidget(self._detail_head)

        self._detail_meta = QtWidgets.QLabel("")
        self._detail_meta.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 11px;")
        self._detail_meta.setWordWrap(True)
        box.addWidget(self._detail_meta)

        # 리포트가 이미 마크다운이라 그대로 그린다.
        self._report = QtWidgets.QTextBrowser()
        self._report.setOpenExternalLinks(False)
        self._report.setStyleSheet(
            f"QTextBrowser {{ background: {theme.SURFACE}; border: 1px solid {theme.GRID};"
            f" border-radius: 8px; color: {theme.INK_SECONDARY}; padding: 10px; }}"
        )
        box.addWidget(self._report, stretch=3)

        box.addWidget(QtWidgets.QLabel("원인 후보"))
        self._contributors = DataTable(
            [
                Column("name", "프로그램", width=150),
                Column("share_pct", "기여도", fmt=".0f", suffix="%", align_right=True, width=70),
                Column("delta", "증가", fmt="+.1f", align_right=True, width=80),
                Column("pid_count", "프로세스", align_right=True, width=70),
                Column("lead", "선행", width=70),
            ],
            max_rows=6,
        )
        box.addWidget(self._contributors, stretch=2)

        # --- 피드백. **대시보드에서 유일하게 DB 를 쓰는 곳이다.**
        feedback = QtWidgets.QHBoxLayout()
        feedback.addWidget(QtWidgets.QLabel("이 판단이 맞았나요?"))
        self._normal_btn = QtWidgets.QPushButton("정상이야")
        self._real_btn = QtWidgets.QPushButton("맞아 문제야")
        self._clear_btn = QtWidgets.QPushButton("취소")
        self._normal_btn.clicked.connect(lambda: self._label("normal"))
        self._real_btn.clicked.connect(lambda: self._label("real"))
        self._clear_btn.clicked.connect(lambda: self._label(None))
        for button in (self._normal_btn, self._real_btn, self._clear_btn):
            button.setEnabled(False)
            feedback.addWidget(button)
        feedback.addStretch(1)
        box.addLayout(feedback)

        hint = QtWidgets.QLabel(
            "이 피드백은 Phase 11 의 학습 입력이 됩니다 — "
            "'정상'으로 표시한 구간은 정상 데이터로 편입됩니다."
        )
        hint.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 10px;")
        hint.setWordWrap(True)
        box.addWidget(hint)
        return panel

    # ------------------------------------------------------------------ 조회

    def _reload(self) -> None:
        days = int(self._days.currentData())
        self._loader = IncidentLoader(days)
        self._loader.loaded.connect(self._on_rows)
        self._loader.start()

    @QtCore.Slot(list)
    def _on_rows(self, rows: list) -> None:
        self._loads += 1
        self._rows = rows
        if not rows:
            self._notice.setText(
                "기록된 사건이 없습니다.\n"
                "사건은 실시간 탐지가 신호를 내고 그것이 하나로 묶일 때 만들어집니다 — "
                "탐지가 조용했다면 정상입니다."
            )
            self._notice.setVisible(True)
            self._table.set_rows([])
            return

        self._notice.setVisible(False)
        self._table.set_rows([_list_row(r) for r in rows])
        self._update_summary(rows)

        if self._pending_id is not None:
            # 골랐든 · 구간을 넓혔든 · 못 찾았다고 말했든 **여기서 끝난다.**
            # 아래 기본 동작(첫 줄 선택)으로 흘러가면 방금 띄운 "못 찾았습니다"를
            # 엉뚱한 사건이 덮어쓴다 — 사용자는 그것을 방금 누른 알림으로 읽는다.
            self._select_pending()
            return
        if self._selected_id is None:
            self._table.selectRow(0)

    def focus_incident(self, incident_id: int) -> None:
        """그 사건을 골라 놓는다. **알림을 누르고 들어온 사람이 도착하는 자리다.**

        목록은 비동기로 온다. 여기서 할 수 있는 것은 "오면 이걸 고른다"까지이고,
        실제 선택은 `_on_rows` 가 한다.
        """
        self._pending_id = incident_id
        self._widened = False
        if self._rows:
            self._select_pending()

    def _select_pending(self) -> None:
        """대기 중인 사건을 고르거나, 구간을 넓히거나, 못 찾았다고 말한다.

        **구간 밖이면 한 번 넓힌다.** 기본 구간은 7일인데 알림은 그보다 오래된 것을
        가리킬 수 있다(창을 며칠 만에 열면 그렇다). 그때 빈손으로 두면 사용자는
        무엇을 눌렀는지도 모른 채 목록 앞에 서게 된다. **한 번만** 넓히는 이유는
        30일에도 없으면 보존 정리로 사라진 것이고, 그때는 다시 넓혀도 소용없다.
        """
        target = self._pending_id
        if target is None:
            return

        for index, row in enumerate(self._rows):
            if row.get("id") == target:
                self._pending_id = None
                self._table.selectRow(index)
                return

        widest = max(days for days, _ in _DAY_CHOICES)
        if not self._widened and int(self._days.currentData()) != widest:
            self._widened = True
            self._days.setCurrentIndex(
                next(i for i, (days, _) in enumerate(_DAY_CHOICES) if days == widest)
            )
            return  # 구간 변경이 재조회를 부른다. 그때 다시 찾는다.

        # 30일 안에도 없다. **조용히 목록 앞으로 보내지 않는다**(규칙 4).
        self._pending_id = None
        self._detail_head.setText(f"사건 #{target} 을 찾지 못했습니다")
        self._detail_meta.setText(
            "보존 기간이 지나 정리됐거나, 다른 데이터 폴더의 사건입니다."
        )

    def _update_summary(self, rows: list[dict]) -> None:
        open_now = sum(1 for r in rows if r.get("ts_end") is None)
        notified = sum(1 for r in rows if r.get("notified"))
        labeled = [r for r in rows if r.get("user_label")]
        false_positives = [r for r in rows if r.get("user_label") == "normal"]

        self._tiles["total"].set(str(len(rows)))
        self._tiles["open"].set(str(open_now))
        self._tiles["notified"].set(str(notified), "발송은 설정에서 켠다")
        if labeled:
            rate = len(false_positives) / len(labeled) * 100
            self._tiles["fp"].set(f"{rate:.0f}%", f"피드백 {len(labeled)}건 기준")
        else:
            self._tiles["fp"].set("—", "피드백 없음")

    # ------------------------------------------------------------------ 상세

    @QtCore.Slot(dict)
    def _on_row_selected(self, row: dict) -> None:
        incident = self._incident_by_id(row.get("id"))
        if incident is None:
            return
        self._selected_id = incident["id"]
        self._render_detail(incident)

    def _incident_by_id(self, incident_id) -> dict | None:
        for row in self._rows:
            if row.get("id") == incident_id:
                return row
        return None

    def _render_detail(self, incident: dict) -> None:
        severity = incident.get("severity") or "info"
        self._detail_head.setText(incident.get("title") or "(제목 없음)")

        detectors = ", ".join(json.loads(incident.get("detectors") or "[]")) or "—"
        marks = []
        if incident.get("suppressed_by"):
            marks.append(f"상위 사건 #{incident['suppressed_by']}에 묻힘")
        if incident.get("user_label") == "normal":
            marks.append("사용자: 정상")
        elif incident.get("user_label") == "real":
            marks.append("사용자: 실제 문제")
        if incident.get("notify_skipped"):
            marks.append(f"알림 안 함: {incident['notify_skipped']}")

        colour = theme.STATUS.get(severity, theme.INK_MUTED)
        meta = (
            f"<span style='color:{colour}'>●</span> {_SEVERITY_LABEL.get(severity, severity)}"
            f" · 신호 {incident.get('signal_count') or 0}건 · 탐지기 {detectors}"
        )
        if marks:
            meta += "<br>" + " · ".join(marks)
        self._detail_meta.setText(meta)

        report = incident.get("explanation_md")
        if report:
            self._report.setMarkdown(report)
        else:
            self._report.setPlainText(
                "설명이 없습니다 — 그 구간의 원본이 이미 정리됐거나 수집이 멈춰 있었습니다."
            )

        self._contributors.set_rows(
            [_contributor_row(c) for c in json.loads(incident.get("contributors") or "[]")]
        )

        self._normal_btn.setEnabled(True)
        self._real_btn.setEnabled(True)
        self._clear_btn.setEnabled(bool(incident.get("user_label")))

    def _label(self, label: str | None) -> None:
        """**대시보드에서 유일하게 DB 를 쓰는 곳.** 피드백은 사용자가 화면에서만 준다."""
        if self._selected_id is None:
            return
        try:
            data.set_user_label(self._selected_id, label)
        except Exception as exc:
            self._detail_meta.setText(f"피드백을 저장하지 못했습니다: {exc}")
            return
        # 방금 쓴 값이 목록에도 반영돼야 한다(오탐 비율이 여기서 바뀐다).
        self._reload()

    # ------------------------------------------------------------------ 상태

    @property
    def load_count(self) -> int:
        return self._loads

    def stop(self) -> None:
        if self._loader is not None:
            self._loader.wait(2000)


def _list_row(incident: dict) -> dict:
    """목록 한 줄. **정렬용 원본 값을 함께 남긴다** — 표시 문자열로 정렬하면 안 된다."""
    start = float(incident["ts_start"])
    end = incident.get("ts_end")
    if end:
        duration = float(end) - start
        span = f"{duration / 60:.0f}분" if duration >= 60 else f"{duration:.0f}초"
    else:
        span = "진행 중"
    return {
        "id": incident.get("id"),
        "when": datetime.fromtimestamp(start).strftime("%m-%d %H:%M:%S"),
        "severity_ko": _SEVERITY_LABEL.get(incident.get("severity"), incident.get("severity")),
        "span": span,
        "title": incident.get("title") or "",
    }


def _contributor_row(contributor: dict) -> dict:
    """원인 후보 한 줄.

    **선행 시간은 기여도가 충분할 때만 보여 준다.** 기여가 작으면 "오르기 시작한 시점"이
    잡음에 좌우되어 엉뚱한 값이 나온다(실측: 기여도 5% 짜리가 "255초 선행").
    """
    share = float(contributor.get("share") or 0.0)
    lead = contributor.get("lead_s")
    return {
        "name": contributor.get("name") or "?",
        "share_pct": share * 100,
        "delta": float(contributor.get("delta") or 0.0),
        "pid_count": len(contributor.get("pids") or []),
        "lead": f"{lead:.0f}초" if lead is not None and share >= 0.1 else "",
    }

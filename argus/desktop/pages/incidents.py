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
                # **초를 빼서 폭을 줄였다.** 사건을 초 단위로 구분할 일은 없고,
                # 그렇게 아낀 자리를 "알림" 열에 준다 — 목록이 좁으면 오른쪽 열부터
                # 잘리는데, 잘려서 안 보이는 열은 없는 것과 같다.
                Column("when", "시각", width=96),
                Column("severity_ko", "등급", width=54),
                # **등급과 알림 발송은 다르다.** `경고` 인데 안 나간 것이 있고
                # (억제·상위 사건에 물림), `정보` 라 안 나간 것도 있다. 목록에 등급만
                # 있으면 "어느 것이 나를 방해했나"를 알 수 없어 상세를 하나씩 열어
                # 봐야 했다. 2026-08-15 에 사용자가 지적한 자리다.
                Column("notified_mark", "알림", width=44),
                Column("span", "지속", align_right=True, width=64),
                # **답을 안 준 알림이 목록에서 보여야 한다.** 상세를 열기 전에는
                # 어느 것이 남았는지 알 수 없었고, 그래서 하나씩 눌러 보는 수밖에
                # 없었다 — 그 수고가 라벨 0건의 이유 중 하나다.
                Column("answer", "답", width=54),
                Column("title", "제목", width=240),
            ]
        )
        self._table.row_selected.connect(self._on_row_selected)
        # **제목 앞의 다섯 열은 항상 보여야 한다.** 96+54+44+64+54 = 312 이고,
        # 여기에 여백을 더한 값이다. 이 바닥이 없으면 상세 쪽 문구가 길어질 때마다
        # 목록이 밀려 `시각` 만 남는다 — 그러면 등급도 알림 여부도 답 대기도 상세를
        # 열어야만 보이고, 목록의 쓸모가 사라진다(2026-08-15 실측).
        self._table.setMinimumWidth(330)
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

        box.addWidget(self._build_feedback())

        # 리포트가 이미 마크다운이라 그대로 그린다.
        self._report = QtWidgets.QTextBrowser()
        self._report.setOpenExternalLinks(False)
        self._report.setStyleSheet(
            f"QTextBrowser {{ background: {theme.SURFACE}; border: 1px solid {theme.GRID};"
            f" border-radius: 8px; color: {theme.INK_SECONDARY}; padding: 10px; }}"
        )
        box.addWidget(self._report, stretch=3)

        # 제목은 사건마다 바뀐다 — `_render_detail` 이 `attributable` 을 보고 고른다.
        self._contributors_head = QtWidgets.QLabel(_CONTRIBUTORS_HEAD)
        self._contributors_head.setWordWrap(True)
        self._contributors_head.setMinimumWidth(1)  # 위 `_question` 과 같은 이유
        box.addWidget(self._contributors_head)
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
        return panel

    def _build_feedback(self) -> QtWidgets.QWidget:
        """피드백 줄. **대시보드에서 유일하게 DB 를 쓰는 곳이다.**

        **제목 바로 아래에 둔다.** 처음에는 상세 맨 아래였는데, 리포트와 원인 후보
        표가 사이에 있어 창이 작으면 시야 밖으로 밀렸다 — 사용자는 답할 자리가
        있다는 것조차 보지 못한다. 설명을 읽고 답하는 순서가 자연스러워 보였지만,
        **읽히지 않는 자리에 있는 버튼은 순서가 없다.**

        **선택지는 상자로 보인다.** 누름 버튼이던 때는 눌러도 버튼 자체가 그대로여서
        "내가 이 사건에 답을 했던가"가 화면에 남지 않았다 — 답한 것과 안 한 것이
        같아 보이면 사용자는 같은 사건을 다시 열어 다시 누른다.

        **둘은 서로를 끈다.** 한 사건이 정상이면서 비정상일 수는 없다. 켜진 것을 다시
        누르면 꺼지고, 그것이 곧 취소다 — 상자를 쓰기로 한 이상 그 동작이 자연스럽고,
        옆의 "취소" 버튼은 같은 일을 하는 두 번째 길로 남는다.
        """
        panel = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        # **"맞았나요"가 아니라 "쓸모 있었나요"다.** 앞의 물음은 사실 판정으로 읽힌다 —
        # "롤이 CPU 26% 를 썼나"는 매번 참이라 그렇게 모은 라벨은 전부 비정상이 되고
        # 아무것도 못 거른다. 이 라벨의 유일한 쓰임이 "알림을 줄일지"를 정하는 것이므로
        # 재는 축도 그것이어야 한다. 08-06 에 점수 하한으로 가르려다 기각된 이유가
        # 여기 있다 — 하한을 걸면 op.gg 가 남고 GPU 발열이 걸러졌다.
        # **묻는 문장은 사건마다 바뀐다** — `_render_detail` 이 `notified` 로 고른다.
        # 알림이 안 나간 사건에까지 "이 알림이 쓸모 있었나요"라고 물으면 사용자는
        # 오지도 않은 알림을 떠올리려 한다. 답은 같은 축(정상/비정상)이고 방향만 반대다.
        self._question = QtWidgets.QLabel(_QUESTION_NOTIFIED)
        # **긴 문장이 상세 패널의 최소 폭을 정하게 두지 않는다.** 줄바꿈을 막아 두면
        # QLabel 이 문장 전체 폭을 최소 크기로 요구하고, 스플리터가 그만큼을 상세에
        # 떼어 주느라 **왼쪽 목록이 시각 열만 남는다.** 2026-08-15 에 이 문구를
        # 길게 바꾸자 실제로 그렇게 됐다 — 문구 하나가 목록을 지운 것이다.
        self._question.setWordWrap(True)
        # **1 로 두면 라벨이 0폭으로 찌그러져 질문이 통째로 사라진다**(2026-08-15
        # 실측 — 상자만 남았다). 무엇에 답하는지 안 보이는 것이 목록이 좁은 것보다
        # 나쁘다. 두 줄로 접힐 수 있는 폭을 바닥으로 준다.
        self._question.setMinimumWidth(190)
        row.addWidget(self._question, stretch=1)
        # 화면 문구는 "정상/비정상", 저장값은 normal/real 이다. **저장값을 바꾸지
        # 않는다** — 이미 쌓인 라벨과 평가 경로(`--incident`)가 그 값을 읽는다.
        self._normal_box = QtWidgets.QCheckBox("정상")
        self._normal_box.setToolTip("안 알려도 됐다 — 내가 돌린 작업이고 불편하지 않았다")
        self._real_box = QtWidgets.QCheckBox("비정상")
        self._real_box.setToolTip("알려줄 만했다 — 실제로 느려졌거나 조치할 것이 있었다")
        # **넘기는 것도 답이다.** 아무것도 안 누르고 지나가면 답 대기에 그대로 남아,
        # "답하기" 를 누를 때마다 같은 사건으로 되돌아온다 — 애매한 것이 가장 최근이면
        # 거기서 막힌다. 그리고 이 수가 쌓이면 그것대로 발견이다: 무슨 일이었는지
        # 모르겠는 알림은 **문턱이 아니라 설명이 부족한** 알림이다.
        self._unknown_box = QtWidgets.QCheckBox("모르겠음")
        self._unknown_box.setToolTip("판단할 수 없다 — 다시 묻지 않는다")
        self._clear_btn = QtWidgets.QPushButton("취소")
        # **`clicked` 다** — `toggled`/`stateChanged` 는 코드가 상태를 세울 때도
        # 울려, 사건을 고르기만 해도 방금 그린 라벨을 DB 에 다시 쓰게 된다.
        self._normal_box.clicked.connect(lambda on: self._label("normal" if on else None))
        self._real_box.clicked.connect(lambda on: self._label("real" if on else None))
        self._unknown_box.clicked.connect(lambda on: self._label("unknown" if on else None))
        self._clear_btn.clicked.connect(lambda: self._label(None))
        for widget in (self._normal_box, self._real_box, self._unknown_box, self._clear_btn):
            widget.setEnabled(False)
            widget.setCursor(QtCore.Qt.PointingHandCursor)
            row.addWidget(widget)

        # **아직 하지 않는 일을 적지 않는다.** 전에는 "정상으로 표시한 구간은 정상
        # 데이터로 편입됩니다"였는데, `user_label` 을 읽는 곳은 이 화면의 오탐 비율
        # 타일뿐이다 — 탐지기·평가·학습 어디에도 소비처가 없다(2026-08-14 확인).
        # 하지도 않는 일을 적으면 사용자는 자기 답이 이미 쓰이고 있다고 믿는다.
        hint = QtWidgets.QLabel("알림 문턱을 고칠 때 근거로 씁니다 · 애매하면 넘기세요")
        hint.setStyleSheet(f"color: {theme.INK_MUTED}; font-size: 10px;")
        row.addWidget(hint)
        row.addStretch(1)
        return panel

    def _show_label(self, label: str | None) -> None:
        """지금 사건의 답을 상자에 비춘다. **사용자 클릭과 구분된다** — 여기서 세운
        상태는 `clicked` 를 울리지 않으므로 DB 를 다시 건드리지 않는다."""
        self._normal_box.setChecked(label == "normal")
        self._real_box.setChecked(label == "real")
        self._unknown_box.setChecked(label == "unknown")

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

    def focus_unlabeled(self) -> None:
        """답 안 준 알림 중 **가장 최근 것**으로 간다. 맨 윗줄 "답하기"가 여기로 온다.

        최근 것부터인 이유는 기억이다 — 오늘 아침 알림이 맞았는지는 답할 수 있어도
        열흘 전 것은 짐작이 된다. 짐작으로 붙인 라벨은 문턱을 고칠 근거가 못 된다.

        **여기서 목록을 다시 묻는 이유**는 화면의 구간(기본 7일)이 답 대기 창(14일)보다
        좁을 수 있어서다. 화면에 있는 것만 보면 8일 전 알림은 영영 안 물어본다.
        """
        try:
            pending = data.unlabeled_notified()
        except Exception:
            pending = []
        if not pending:
            self._detail_head.setText("답할 알림이 없습니다")
            self._detail_meta.setText(
                f"최근 {int(data.label_window_days())}일 안에 발송된 알림에는 모두 답이 달려 있습니다."
            )
            return
        self.focus_incident(int(pending[0]["id"]))

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
        # **"모르겠음"은 분모에 넣지 않는다.** 판단이 아니라 판단할 수 없었다는 표시라,
        # 세면 오탐 비율이 실제보다 낮게 나온다 — 답을 모을수록 문제가 작아 보인다.
        labeled = [r for r in rows if r.get("user_label") in ("normal", "real")]
        false_positives = [r for r in rows if r.get("user_label") == "normal"]
        # **기계 답은 따로 센다.** 자동 라벨은 사람 답이 없을 때만 쓰고, 몇 건이
        # 기계 것인지를 타일 문구에 그대로 적는다 — 섞어 놓고 한 숫자만 보이면
        # "오탐 12%"가 사람의 판단인지 내 규칙의 메아리인지 구분할 수 없다.
        auto = [
            r
            for r in rows
            if r.get("notified")
            and not r.get("user_label")
            and r.get("auto_label") in ("normal", "real")
            and not r.get("during_injection")
        ]
        auto_fp = [r for r in auto if r.get("auto_label") == "normal"]

        self._tiles["total"].set(str(len(rows)))
        self._tiles["open"].set(str(open_now))
        self._tiles["notified"].set(str(notified), "발송은 설정에서 켠다")

        # **라벨이 없을 때 "피드백 없음"으로 두지 않는다.** 그 문구는 결과처럼 읽혀
        # 사용자가 할 일이 있다는 것을 전하지 못했다(08-09 ~ 08-14, 알림 50건에 라벨 0건).
        # 밀린 수를 세어 요구로 바꾼다.
        pending = sum(
            1
            for r in rows
            if r.get("notified")
            and not r.get("user_label")
            and not r.get("auto_label")
            and not r.get("during_injection")
        )
        if labeled or auto:
            total = len(labeled) + len(auto)
            rate = (len(false_positives) + len(auto_fp)) / total * 100
            parts = []
            if labeled:
                parts.append(f"사람 {len(labeled)}건")
            if auto:
                parts.append(f"자동 {len(auto)}건")
            detail = " · ".join(parts) + " 기준"
            if pending:
                detail += f" · {pending}건 답 대기"
            self._tiles["fp"].set(f"{rate:.0f}%", detail)
        elif pending:
            self._tiles["fp"].set("—", f"알림 {pending}건이 맞았는지 알려주세요")
        else:
            self._tiles["fp"].set("—", "답할 알림 없음")

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
            marks.append("사용자: 비정상")
        elif incident.get("user_label") == "unknown":
            marks.append("사용자: 모르겠음")
        elif incident.get("auto_label") in ("normal", "real"):
            # **근거를 함께 적는다.** 판정만 보이면 사용자는 그것을 뒤집을지 말지를
            # 정할 수 없고, 뒤집을 수 없는 판정은 사람 답을 모으는 길을 막는다.
            auto_ko = "정상" if incident["auto_label"] == "normal" else "비정상"
            reason = incident.get("auto_label_reason") or ""
            marks.append(f"자동: {auto_ko}" + (f" ({reason})" if reason else ""))
        if incident.get("notify_skipped"):
            marks.append(f"알림 안 함: {incident['notify_skipped']}")
        # **왜 안 물어보는지가 보여야 한다.** 답 대기에서 빼 놓고 이유를 안 적으면
        # 사용자는 목록에서 사라진 것을 버그로 읽는다.
        if incident.get("during_injection"):
            marks.append("결함 주입 중 — 답하지 않아도 됩니다")

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

        # **표 제목이 리포트와 같은 말을 해야 한다.** 발열·GPU 는 프로세스별 사용량을
        # 얻을 수 없어 CPU 상위로 대신 분해하는데, 리포트 본문은 그것을 "참고"라고
        # 적는 반면 표는 "원인 후보"라고 적고 있었다. **둘이 붙어 있으면 표가 이긴다** —
        # 사용자는 순위가 매겨진 표를 답으로 읽는다. 실측 `#59` 에서 GPU 90°C 사건의
        # 1위가 `pythonw`(관측자 자신) 22%, 실제로 GPU 를 태운 롤 클라이언트가 2위였다.
        self._contributors_head.setText(
            _CONTRIBUTORS_HEAD if incident.get("attributable") else _CONTRIBUTORS_HEAD_REFERENCE
        )
        self._question.setText(
            _QUESTION_NOTIFIED if incident.get("notified") else _QUESTION_UNNOTIFIED
        )
        self._contributors.set_rows(
            [_contributor_row(c) for c in json.loads(incident.get("contributors") or "[]")]
        )

        self._show_label(incident.get("user_label"))
        self._normal_box.setEnabled(True)
        self._real_box.setEnabled(True)
        self._unknown_box.setEnabled(True)
        self._clear_btn.setEnabled(bool(incident.get("user_label")))

    def _label(self, label: str | None) -> None:
        """**대시보드에서 유일하게 DB 를 쓰는 곳.** 피드백은 사용자가 화면에서만 준다."""
        if self._selected_id is None:
            return
        try:
            data.set_user_label(self._selected_id, label)
        except Exception as exc:
            self._detail_meta.setText(f"피드백을 저장하지 못했습니다: {exc}")
            # **화면을 저장된 값으로 되돌린다.** 저장에 실패했는데 상자가 켜진 채면
            # 사용자는 답을 남겼다고 믿고 떠난다 — 그러면 라벨은 없는데 다시 물어볼
            # 기회도 사라진다.
            incident = self._incident_by_id(self._selected_id) or {}
            self._show_label(incident.get("user_label"))
            return
        # **한 사건이 정상이면서 비정상일 수는 없다.** 방금 켠 것만 남긴다.
        self._show_label(label)
        self._clear_btn.setEnabled(label is not None)
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
        "when": datetime.fromtimestamp(start).strftime("%m-%d %H:%M"),
        "severity_ko": _SEVERITY_LABEL.get(incident.get("severity"), incident.get("severity")),
        # 발송된 것만 표시한다. 안 나간 쪽이 훨씬 많아(7일간 20건 중 5건) 그쪽에
        # 기호를 달면 목록이 기호로 덮인다 — 드문 쪽을 표시해야 눈에 띈다.
        "notified_mark": "●" if incident.get("notified") else "",
        "span": span,
        "answer": _answer_mark(incident),
        "title": incident.get("title") or "",
    }


def _answer_mark(incident: dict) -> str:
    """목록의 "답" 칸.

    **묻지 않는 사건은 빈칸으로 둔다.** 답을 안 준 것과 물은 적이 없는 것은 다르고,
    173건 전부에 물음표를 세우면 밀린 것이 그 안에 묻힌다.

    **"묻는가"는 `pending_answer` 가 답한다** — 조회 계층이 답 대기와 같은 규칙으로
    채워 준다. 예전에는 여기서 `notified` 만 봤는데, 2026-08-15 에 답 대기가 미탐
    일부까지 묻게 되자 **목록은 "안 물어봄"이라 적고 답하기 버튼은 그 사건으로
    데려가는** 상태가 됐다. 규칙을 화면이 따로 갖고 있으면 반드시 갈린다.

    **결함 주입 구간은 "주입"으로 따로 세운다.** 답 대기에서 뺐으므로 물음표를
    달면 거짓말이 되고, 빈칸으로 두면 왜 안 물어보는지가 보이지 않는다.

    **기계가 매긴 답에는 `·` 를 붙인다** (`·정상`). 사람 답과 같은 글자로 적으면
    목록만 보고는 누가 답한 것인지 알 수 없고, 그러면 "내가 언제 이걸 정상이라고
    했지"가 된다.
    """
    label = incident.get("user_label")
    if label == "normal":
        return "정상"
    if label == "real":
        return "비정상"
    if label == "unknown":
        return "모름"
    auto = incident.get("auto_label")
    if auto == "normal":
        return "·정상"
    if auto == "real":
        return "·비정상"
    if incident.get("during_injection"):
        return "주입"
    return "?" if incident.get("pending_answer") else ""


# **"맞았나요"가 아니라 "쓸모 있었나요"다.** 앞의 물음은 사실 판정으로 읽힌다 —
# "롤이 CPU 26% 를 썼나"는 매번 참이라 그렇게 모은 라벨은 전부 비정상이 되고 아무것도
# 못 거른다. 이 라벨의 유일한 쓰임이 "알림을 줄일지"이므로 재는 축도 그것이어야 한다.
_QUESTION_NOTIFIED = "이 알림이 쓸모 있었나요?"

# 알림이 안 나간 사건. **같은 축을 반대 방향에서 묻는다** — 여기서 "비정상"은
# "알려줬어야 했다"(미탐)가 된다. 저장값은 그대로 normal/real 이다.
_QUESTION_UNNOTIFIED = "이 사건을 알려줬어야 했나요? (알림은 나가지 않았습니다)"

_CONTRIBUTORS_HEAD = "원인 후보"

# **"참고"만으로는 약하다.** 순위가 매겨진 표 위에 붙은 말은 잘 안 읽히므로, 왜 원인이
# 아닌지를 같은 줄에서 끝낸다 — 읽는 사람이 표를 보기 전에 알아야 하는 것이 그것이다.
_CONTRIBUTORS_HEAD_REFERENCE = (
    "참고 — CPU 사용 상위 (이 사건의 원인은 특정할 수 없습니다. 아래는 범인이 아닙니다)"
)


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

"""Incidents — "어제 몇 시에 왜 느렸는지".

**이 페이지가 Phase 10 DoD 다.** 나머지 화면은 무슨 일이 있었는지 보여 주지만
*왜* 에는 답하지 못한다. 사건 하나를 열면 병목·원인 후보·근거가 한 화면에 있어야 한다.

피드백 버튼이 여기 있는 이유: 오탐을 사용자가 알려 줄 수 있는 유일한 자리다.
그 라벨이 Phase 11 의 학습 입력이 된다.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

# Streamlit 은 페이지를 `exec` 으로 돌려 패키지 컨텍스트가 없다 → 상대 import 불가.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from argus.dashboard import data, theme  # noqa: E402
from argus.dashboard.common import empty, page_header, sidebar_status  # noqa: E402

st.set_page_config(page_title="Argus · Incidents", page_icon="👁", layout="wide")
page_header("Incidents", "무슨 일이 있었고, 왜 그랬는가")
sidebar_status()

days = st.select_slider("구간", options=[1, 3, 7, 30], value=7, format_func=lambda d: f"{d}일")
rows = data.incidents(days=days)

if not rows:
    empty(
        "기록된 사건이 없습니다.\n\n"
        "사건은 실시간 탐지가 신호를 내고 그것이 하나로 묶일 때 만들어집니다. "
        "탐지가 조용했다면 정상입니다 — 현재 오탐률 0.00/h."
    )
    st.stop()

# ---------------------------------------------------------------- 요약

open_now = [r for r in rows if r["ts_end"] is None]
labeled = [r for r in rows if r["user_label"]]
false_positives = [r for r in rows if r["user_label"] == "normal"]

cols = st.columns(4)
with cols[0]:
    st.metric("사건", len(rows))
with cols[1]:
    st.metric("진행 중", len(open_now))
with cols[2]:
    st.metric("알림 대상", sum(1 for r in rows if r["notified"]))
    st.caption("발송은 Phase 9 후반")
with cols[3]:
    if labeled:
        rate = len(false_positives) / len(labeled) * 100
        st.metric("오탐 비율", f"{rate:.0f}%")
        st.caption(f"피드백 {len(labeled)}건 기준")
    else:
        st.metric("오탐 비율", "—")
        st.caption("피드백 없음")

# ---------------------------------------------------------------- 목록

SEVERITY_COLOR = {
    "critical": theme.STATUS["critical"],
    "warning": theme.STATUS["warning"],
    "info": theme.INK_MUTED,
}

st.markdown("#### 사건 목록")

for row in rows:
    start = datetime.fromtimestamp(row["ts_start"])
    if row["ts_end"]:
        duration = row["ts_end"] - row["ts_start"]
        span = f"{duration / 60:.0f}분" if duration >= 60 else f"{duration:.0f}초"
        when = f"{start:%m-%d %H:%M:%S} · {span}"
    else:
        when = f"{start:%m-%d %H:%M:%S} · 진행 중"

    color = SEVERITY_COLOR.get(row["severity"], theme.INK_MUTED)
    marks = []
    if row["suppressed_by"]:
        marks.append(f"상위 사건 #{row['suppressed_by']}에 묻힘")
    if row["user_label"] == "normal":
        marks.append("사용자: 정상")
    elif row["user_label"] == "real":
        marks.append("사용자: 실제 문제")
    suffix = f"  ({', '.join(marks)})" if marks else ""

    header = f"[{row['severity'].upper()}] {when} — {row['title']}{suffix}"
    with st.expander(header, expanded=False):
        st.markdown(
            f"<span style='color:{color}'>●</span> **{row['severity']}**"
            f" · 신호 {row['signal_count']}건"
            f" · 탐지기 {', '.join(json.loads(row['detectors'] or '[]')) or '—'}",
            unsafe_allow_html=True,
        )

        if row["explanation_md"]:
            st.markdown(row["explanation_md"])
        else:
            st.caption("설명이 없습니다 — 그 구간의 원본이 이미 정리됐거나 수집이 멈춰 있었습니다.")

        contributors = json.loads(row["contributors"] or "[]")
        if contributors:
            st.markdown("**원인 후보**")
            st.dataframe(
                [
                    {
                        "프로그램": c["name"],
                        "기여도": f"{c['share'] * 100:.0f}%",
                        "증가": f"+{c['delta']:.1f}",
                        "프로세스": len(c["pids"]),
                        "선행": (
                            f"{c['lead_s']:.0f}초"
                            if c.get("lead_s") is not None and c["share"] >= 0.1
                            else ""
                        ),
                        "신규": "●" if c.get("is_new") else "",
                    }
                    for c in contributors
                ],
                width="stretch",
                hide_index=True,
            )

        if row["notify_skipped"]:
            st.caption(f"알림 안 함: {row['notify_skipped']}")

        # ------------------------------------------------------ 피드백
        st.markdown("**이 판단이 맞았나요?**")
        fb = st.columns([1, 1, 1, 5])
        with fb[0]:
            if st.button("정상이야", key=f"normal_{row['id']}"):
                data.set_user_label(row["id"], "normal")
                st.rerun()
        with fb[1]:
            if st.button("맞아 문제야", key=f"real_{row['id']}"):
                data.set_user_label(row["id"], "real")
                st.rerun()
        with fb[2]:
            if row["user_label"] and st.button("취소", key=f"clear_{row['id']}"):
                data.set_user_label(row["id"], None)
                st.rerun()

        st.caption(
            "이 피드백은 Phase 11 의 학습 입력이 됩니다 — "
            "'정상'으로 표시한 구간은 정상 데이터로 편입됩니다."
        )

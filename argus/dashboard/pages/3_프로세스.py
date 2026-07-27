"""프로세스 — 무엇이 리소스를 쓰고 있나.

PID 가 아니라 **프로그램 이름으로 합친다.** chrome 은 탭마다 프로세스를 만들어
PID 단위로 보면 30개가 각각 3% 인데, 사용자가 알고 싶은 것은 "크롬이 90%"다.

여기는 아직 지문(Phase 6)이 없다. 그래서 "평소보다 많이 쓰는가"는 답하지 못하고
"지금 많이 쓰는가"만 답한다. 그 차이를 화면에서 숨기지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit 은 페이지를 `exec` 으로 돌려 패키지 컨텍스트가 없다 → 상대 import 불가.
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from argus.dashboard import data, theme  # noqa: E402
from argus.dashboard.common import (  # noqa: E402
    chart,
    empty,
    page_header,
    series,
    sidebar_status,
    timestamps,
)

st.set_page_config(page_title="Argus · 프로세스", page_icon="👁", layout="wide")
page_header("프로세스", "프로세스별 사용량 — 지금 무엇이 쓰고 있나")
sidebar_status()

window = st.select_slider(
    "집계 창", options=[30, 60, 300, 900, 1800], value=60, format_func=lambda s: f"최근 {s // 60}분" if s >= 60 else f"최근 {s}초"
)
rows = data.top_processes(seconds=window, limit=20)

if not rows:
    empty("프로세스 기록이 없습니다.")
    st.stop()

foreground = [r for r in rows if r["foreground"]]
if foreground:
    st.caption(
        "포어그라운드: " + ", ".join(f"**{r['name']}**" for r in foreground[:3])
    )

# ---------------------------------------------------------------- 랭킹

top = [r for r in rows if (r["cpu"] or 0) > 0][:12]
if top:
    st.markdown("#### CPU 사용량 (%)")
    # 가로 막대 — 이름이 길고 개수가 많을 때 세로 막대는 라벨이 겹친다.
    chart(
        [
            {
                "type": "bar",
                "orientation": "h",
                "x": [r["cpu"] for r in reversed(top)],
                "y": [r["name"] for r in reversed(top)],
                "marker": {"color": theme.SERIES[0], "cornerradius": 4},
                "text": [f"{r['cpu']:.1f}%" for r in reversed(top)],
                "textposition": "outside",
                "textfont": {"color": theme.INK_SECONDARY},
                "hovertemplate": "%{y}: %{x:.2f}%<extra></extra>",
            }
        ],
        height=max(240, 26 * len(top)),
        hovermode="closest",
        margin={"l": 160, "r": 60, "t": 8, "b": 32},
        xaxis={"gridcolor": theme.GRID, "linecolor": theme.AXIS, "zeroline": False,
               "tickfont": {"color": theme.INK_MUTED}, "ticksuffix": "%"},
        yaxis={"gridcolor": "rgba(0,0,0,0)", "linecolor": theme.AXIS, "zeroline": False,
               "tickfont": {"color": theme.INK_SECONDARY}},
    )

st.markdown("#### 전체")
st.dataframe(
    [
        {
            "프로그램": r["name"],
            "PID 수": r["pids"],
            "CPU 평균": f"{r['cpu']:.2f}%" if r["cpu"] is not None else "—",
            "CPU 최대": f"{r['cpu_max']:.1f}%" if r["cpu_max"] is not None else "—",
            "메모리": f"{r['rss']:.0f} MB" if r["rss"] is not None else "—",
            "핸들": f"{r['handles']:,}" if r["handles"] else "—",
            "포어그라운드": "●" if r["foreground"] else "",
        }
        for r in rows
    ],
    width='stretch',
    hide_index=True,
)

# ---------------------------------------------------------------- 개별 추이

st.markdown("#### 개별 추이")
names = [r["name"] for r in rows]
chosen = st.selectbox("프로그램", names, index=0)
history = data.process_series(chosen, seconds=1800)

if not history:
    st.caption("이 프로그램의 시계열이 없습니다.")
else:
    hts = timestamps(history)
    left, right = st.columns(2)
    with left:
        chart([theme.line("CPU (%)", hts, series(history, "cpu"), slot=0)], height=200)
        st.caption("CPU")
    with right:
        chart(
            [
                theme.line("메모리 (MB)", hts, series(history, "rss"), slot=1),
                theme.line("핸들", hts, series(history, "handles"), slot=2),
            ],
            legend=True,
            height=200,
        )
        st.caption("메모리와 핸들 — 누수는 둘이 함께 오른다")

    first, last = history[0], history[-1]
    span_h = (last["ts"] - first["ts"]) / 3600
    if span_h > 0.05 and first["rss"]:
        rate = (last["rss"] - first["rss"]) / span_h
        color = theme.severity_color(abs(rate), warn=20.0, crit=100.0)
        st.markdown(
            f"메모리 증가율 **<span style='color:{color}'>{rate:+.1f} MB/시간</span>** "
            f"({span_h * 60:.0f}분 관측)",
            unsafe_allow_html=True,
        )

st.caption(
    "지문(Phase 6)이 붙기 전이라 '평소 대비'는 아직 답하지 못합니다 — "
    "지금은 '현재 많이 쓰는가'만 보여 줍니다."
)

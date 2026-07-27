"""Timeline — 1분 집계 시간축에 결함 주입 구간과 탐지 신호를 겹친다.

이 화면의 목적은 하나다. **탐지기가 잡아야 할 곳을 잡았는지 눈으로 확인하는 것.**
주입 구간(정답)과 탐지 신호(판정)가 같은 축 위에 있어야 정탐·미탐·오탐이 보인다.

`metrics_1m` 을 쓰는 이유는 원본이 24시간만 남기 때문이다. 어제 일을 보려면 접힌
데이터를 봐야 한다. 접을 때 표준편차를 함께 남긴 것이 여기서 값을 한다 — 평균이
같아도 흔들림이 다르면 다른 상황이다.
"""

from __future__ import annotations

import sys
from datetime import datetime
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
)

st.set_page_config(page_title="Argus · Timeline", page_icon="👁", layout="wide")
page_header("Timeline", "1분 집계 · 결함 주입 구간과 탐지 신호")
sidebar_status()

hours = st.select_slider(
    "구간", options=[1, 3, 6, 12, 24, 72, 168], value=6, format_func=lambda h: f"{h}시간"
)
rows = data.rollup(hours=hours)

if not rows:
    empty(
        "1분 집계가 없습니다. 롤업은 수집 시작 후 약 2분 뒤부터 채워집니다 "
        "(`python -m argus.storage.rollup` 으로 즉시 따라잡을 수도 있습니다)."
    )
    st.stop()

ts = [datetime.fromtimestamp(r["ts_min"]) for r in rows]
faults = data.fault_injections(hours=hours)
signals = data.anomaly_signals(hours=hours)

# ---------------------------------------------------------------- 오버레이

def overlays() -> tuple[list[dict], list[dict]]:
    """주입 구간은 세로 밴드, 탐지 신호는 세로 선.

    색은 계열 팔레트가 아니라 상태색을 쓴다 — 이건 데이터 계열이 아니라 "표시"다.
    계열 색으로 그리면 5번째 지표처럼 읽힌다.
    """
    shapes, annotations = [], []
    for fault in faults:
        if not fault["ts_end"]:
            continue
        # 효과가 관측되지 않은 주입(completed=0)은 채점에서 빠진다. 흐리게 그려
        # "라벨은 있으나 증상이 없던 구간"임을 드러낸다.
        strong = bool(fault["completed"])
        shapes.append(
            {
                "type": "rect",
                "xref": "x",
                "yref": "paper",
                "x0": datetime.fromtimestamp(fault["ts_start"]),
                "x1": datetime.fromtimestamp(fault["ts_end"]),
                "y0": 0,
                "y1": 1,
                "fillcolor": theme.STATUS["warning"],
                "opacity": 0.22 if strong else 0.07,
                "line": {"width": 0},
                "layer": "below",
            }
        )
        annotations.append(
            {
                "x": datetime.fromtimestamp(fault["ts_start"]),
                "y": 1.0,
                "xref": "x",
                "yref": "paper",
                "text": fault["scenario"] + ("" if strong else " (증상 없음)"),
                "showarrow": False,
                "font": {"size": 10, "color": theme.INK_MUTED},
                "xanchor": "left",
                "yanchor": "bottom",
            }
        )
    for signal in signals:
        shapes.append(
            {
                "type": "line",
                "xref": "x",
                "yref": "paper",
                "x0": datetime.fromtimestamp(signal["ts"]),
                "x1": datetime.fromtimestamp(signal["ts"]),
                "y0": 0,
                "y1": 1,
                "line": {"color": theme.STATUS["critical"], "width": 1.5, "dash": "dot"},
                "layer": "above",
            }
        )
    return shapes, annotations


shapes, annotations = overlays()

legend_bits = []
if faults:
    legend_bits.append(
        f"<span style='color:{theme.STATUS['warning']}'>▮</span> 결함 주입 구간 (정답 라벨) {len(faults)}건"
    )
if signals:
    legend_bits.append(
        f"<span style='color:{theme.STATUS['critical']}'>┆</span> 탐지 신호 {len(signals)}건"
    )
if legend_bits:
    st.markdown(" · ".join(legend_bits), unsafe_allow_html=True)
else:
    st.caption("이 구간에는 결함 주입도 탐지 신호도 없습니다.")

# ---------------------------------------------------------------- 차트

st.markdown("#### CPU (%) — 평균과 흔들림")
chart(
    [
        theme.line("평균", ts, series(rows, "cpu_mean"), slot=0),
        theme.line("최대", ts, series(rows, "cpu_max"), slot=1),
        theme.line("표준편차", ts, series(rows, "cpu_std"), slot=2),
    ],
    legend=True,
    height=260,
    shapes=shapes,
    annotations=annotations,
)
st.caption(
    "표준편차가 크면 같은 평균이라도 다른 상황이다 — 고르게 눌린 부하와 튀는 부하는 다르다."
)

left, right = st.columns(2)
with left:
    st.markdown("#### 메모리 (%)")
    chart(
        [
            theme.line("평균", ts, series(rows, "mem_percent_mean"), slot=0),
            theme.line("최대", ts, series(rows, "mem_percent_max"), slot=1),
        ],
        legend=True,
        height=200,
        shapes=shapes,
    )
with right:
    st.markdown("#### 디스크 응답 (ms)")
    chart(
        [
            theme.line("평균", ts, series(rows, "disk_resp_ms_mean"), slot=0),
            theme.line("p95", ts, series(rows, "disk_resp_ms_p95"), slot=1),
        ],
        legend=True,
        height=200,
        shapes=shapes,
    )

left2, right2 = st.columns(2)
with left2:
    st.markdown("#### 디스크 처리량 (MB/s)")
    chart(
        [
            theme.line("읽기", ts, [(r["disk_read_bps_mean"] or 0) / 1048576 for r in rows], slot=0),
            theme.line("쓰기", ts, [(r["disk_write_bps_mean"] or 0) / 1048576 for r in rows], slot=1),
        ],
        legend=True,
        height=200,
        shapes=shapes,
    )
with right2:
    if any(r["gpu_util_mean"] is not None for r in rows):
        st.markdown("#### GPU (%)")
        chart(
            [
                theme.line("평균", ts, series(rows, "gpu_util_mean"), slot=0),
                theme.line("최대", ts, series(rows, "gpu_util_max"), slot=1),
            ],
            legend=True,
            height=200,
            shapes=shapes,
        )
    else:
        st.markdown("#### 코어 불균형")
        chart([theme.line("불균형", ts, series(rows, "cpu_imbalance_mean"), slot=0)], height=200)

# ---------------------------------------------------------------- 포어그라운드

st.markdown("#### 무엇을 하고 있었나")
foreground = {}
for row in rows:
    name = row["foreground_proc"]
    if name:
        foreground[name] = foreground.get(name, 0) + 1

if foreground:
    total = sum(foreground.values())
    top = sorted(foreground.items(), key=lambda kv: kv[1], reverse=True)[:10]
    st.dataframe(
        [
            {"프로그램": name, "분": count, "비중": f"{count / total * 100:.0f}%"}
            for name, count in top
        ],
        width='stretch',
        hide_index=True,
    )
    st.caption(
        "이 표가 Phase 4-B 레짐 추론의 입력이다 — 리소스 수치만으로는 "
        "'무엇을 하는 중인가'를 알 수 없다."
    )
else:
    st.caption("포어그라운드 기록이 없습니다.")

if faults:
    st.markdown("#### 결함 주입 이력")
    st.dataframe(
        [
            {
                "시나리오": f["scenario"],
                "시작": f"{datetime.fromtimestamp(f['ts_start']):%m-%d %H:%M:%S}",
                "길이": f"{(f['ts_end'] - f['ts_start']) / 60:.1f}분" if f["ts_end"] else "미완",
                "증상 관측": "✅" if f["completed"] else "❌ (채점 제외)",
                "램프": "○" if f["ramp"] else "",
            }
            for f in reversed(faults)
        ],
        width='stretch',
        hide_index=True,
    )

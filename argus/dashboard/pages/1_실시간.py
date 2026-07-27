"""실시간 — 지금 이 순간과 최근 10분.

**축이 다른 지표를 한 차트에 겹치지 않는다.** CPU %(0~100)와 디스크 MB/s 를 한 그림에
넣으면 두 y 축이 생기고, 그 순간 "어느 선이 어느 축인가"를 읽는 비용이 데이터를 읽는
비용보다 커진다. 단위가 같은 것끼리만 묶고 나머지는 나눈다.
"""

from __future__ import annotations

import sys
import time
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
    timestamps,
)

st.set_page_config(page_title="Argus · 실시간", page_icon="👁", layout="wide")
page_header("실시간", "지금 이 순간의 상태와 최근 10분")
sidebar_status()

latest = data.latest_metrics()
if not latest:
    empty("아직 수집된 메트릭이 없습니다.")
    st.stop()

age = time.time() - latest["ts"]
if age > 120:
    st.warning(f"마지막 표본이 {age / 60:.0f}분 전입니다 — 수집이 멈춰 있을 수 있습니다.")

rows = data.recent_metrics(seconds=600)
gpu_rows = data.recent_gpu(seconds=600)
gpus = data.latest_gpu()
ts = timestamps(rows)

# ---------------------------------------------------------------- 현재 값

cols = st.columns(5)
with cols[0]:
    st.metric("CPU", f"{latest['cpu_total']:.1f}%" if latest["cpu_total"] is not None else "—")
    if latest["cpu_max_core"] is not None:
        st.caption(f"최다 코어 {latest['cpu_max_core']:.0f}%")
with cols[1]:
    st.metric("메모리", f"{latest['mem_percent']:.1f}%" if latest["mem_percent"] is not None else "—")
    if latest["mem_avail_mb"]:
        st.caption(f"여유 {latest['mem_avail_mb'] / 1024:.1f} GB")
with cols[2]:
    read = (latest["disk_read_bps"] or 0) / 1048576
    write = (latest["disk_write_bps"] or 0) / 1048576
    st.metric("디스크", f"{read + write:.1f} MB/s")
    st.caption(f"읽기 {read:.1f} · 쓰기 {write:.1f}")
with cols[3]:
    # 응답시간이 증상이다. 처리량이 아무리 높아도 여기가 낮으면 사용자는 못 느낀다.
    resp = latest["disk_resp_ms"]
    st.metric("디스크 응답", f"{resp:.2f} ms" if resp is not None else "—")
    if latest["disk_queue"] is not None:
        st.caption(f"큐 {latest['disk_queue']:.1f}")
with cols[4]:
    if gpus:
        st.metric("GPU", f"{gpus[0]['util_percent']:.0f}%" if gpus[0]["util_percent"] is not None else "—")
        temp = gpus[0].get("temp_c")
        st.caption(f"{temp:.0f}°C · VRAM {gpus[0]['vram_used_mb'] / 1024:.1f}GB" if temp else "")
    else:
        st.metric("GPU", "없음")
        st.caption("NVML 미탑재")

if not rows:
    empty("최근 10분 데이터가 없습니다.")
    st.stop()

# ---------------------------------------------------------------- 최근 10분

st.markdown("#### CPU · 메모리 (%)")
chart(
    [
        theme.line("CPU 전체", ts, series(rows, "cpu_total"), slot=0),
        theme.line("최다 코어", ts, series(rows, "cpu_max_core"), slot=1),
        theme.line("메모리", ts, series(rows, "mem_percent"), slot=2),
    ],
    legend=True,
    height=240,
    yaxis={"gridcolor": theme.GRID, "linecolor": theme.AXIS, "zeroline": False,
           "tickfont": {"color": theme.INK_MUTED}, "range": [0, 100], "ticksuffix": "%"},
)

left, right = st.columns(2)
with left:
    st.markdown("#### 디스크 처리량 (MB/s)")
    chart(
        [
            theme.line("읽기", ts, [(r["disk_read_bps"] or 0) / 1048576 for r in rows], slot=0),
            theme.line("쓰기", ts, [(r["disk_write_bps"] or 0) / 1048576 for r in rows], slot=1),
        ],
        legend=True,
        height=200,
    )
with right:
    st.markdown("#### 디스크 응답시간 (ms) — 증상")
    chart([theme.line("응답", ts, series(rows, "disk_resp_ms"), slot=3)], height=200)
    st.caption("사용률은 원인이고 응답시간이 증상이다. 증상 없는 원인은 알릴 가치가 없다.")

left2, right2 = st.columns(2)
with left2:
    st.markdown("#### 네트워크 (MB/s)")
    chart(
        [
            theme.line("수신", ts, [(r["net_rx_bps"] or 0) / 1048576 for r in rows], slot=0),
            theme.line("송신", ts, [(r["net_tx_bps"] or 0) / 1048576 for r in rows], slot=1),
        ],
        legend=True,
        height=200,
    )
with right2:
    if gpu_rows:
        st.markdown("#### GPU 사용률 (%)")
        gts = timestamps(gpu_rows)
        chart(
            [
                theme.line("사용률", gts, series(gpu_rows, "util_percent"), slot=0),
                theme.line("온도 (°C)", gts, series(gpu_rows, "temp_c"), slot=1),
            ],
            legend=True,
            height=200,
        )
    else:
        st.markdown("#### GPU")
        st.caption("이 시스템에는 NVML 로 읽을 수 있는 GPU 가 없습니다.")

st.caption(f"마지막 갱신 {datetime.fromtimestamp(latest['ts']):%H:%M:%S} · 새로고침하면 갱신됩니다")

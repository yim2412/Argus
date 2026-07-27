"""페이지 공통 조각 — 헤더, 사이드바, 차트 렌더.

차트는 여기 `chart()` 한 곳을 통해서만 그린다. 페이지마다 plotly 레이아웃을 따로
쓰면 축·격자·hover 가 조금씩 어긋나고, 그 어긋남이 데이터 차이처럼 읽힌다.
"""

from __future__ import annotations

import time
from datetime import datetime

import streamlit as st

from . import data, theme


def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def sidebar_status() -> None:
    """수집이 살아 있는지. 대시보드를 여는 이유의 절반이 이 확인이다."""
    with st.sidebar:
        st.markdown("### 수집 상태")
        latest = data.latest_metrics()
        if not latest:
            st.warning("메트릭이 없습니다")
            return

        age = time.time() - latest["ts"]
        if age < 10:
            st.success(f"수집 중 · {age:.0f}초 전")
        elif age < 120:
            st.warning(f"지연 · {age:.0f}초 전")
        else:
            st.error(f"멈춤 · {age / 60:.0f}분 전")

        st.caption(f"마지막 표본 {datetime.fromtimestamp(latest['ts']):%m-%d %H:%M:%S}")

        state = data.rollup_state()
        if state:
            lag = time.time() - state["watermark_ts"]
            st.caption(f"롤업 워터마크 {lag / 60:.0f}분 뒤")
        else:
            st.caption("롤업 미실행")


def chart(traces: list[dict], *, height: int = 260, legend: bool = False, **layout_kwargs):
    """공통 레이아웃으로 plotly 차트를 그린다.

    `legend` 는 계열이 둘 이상일 때 켠다 — 색만으로 정체를 알게 두지 않는다.
    """
    import plotly.graph_objects as go

    figure = go.Figure(
        data=traces,
        layout=theme.layout(height=height, showlegend=legend, **layout_kwargs),
    )
    st.plotly_chart(figure, width='stretch', config={"displayModeBar": False})


def stat(label: str, value: str, caption: str = "", color: str | None = None) -> None:
    """한 줄 지표. 색을 쓰더라도 값과 라벨이 항상 함께 있어야 한다."""
    if color:
        st.markdown(
            f"<div style='font-size:0.8rem;color:{theme.INK_MUTED}'>{label}</div>"
            f"<div style='font-size:1.6rem;font-weight:600;color:{color}'>{value}</div>",
            unsafe_allow_html=True,
        )
    else:
        st.metric(label, value)
    if caption:
        st.caption(caption)


def timestamps(rows: list[dict], key: str = "ts") -> list[datetime]:
    return [datetime.fromtimestamp(row[key]) for row in rows]


def series(rows: list[dict], key: str) -> list[float | None]:
    return [row.get(key) for row in rows]


def empty(message: str) -> None:
    st.info(message)

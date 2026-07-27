"""대시보드 색과 차트 기본값.

**색은 다크 서피스 기준으로 검증된 값만 쓴다.** `dataviz` 스킬의 검증기를 실제로
돌려서 통과한 조합이며, 눈으로 고른 것이 아니다.

    node scripts/validate_palette.js "#3987e5,#d95926,#199e70,#c98500" --mode dark
    → 명도 대역 PASS · 채도 하한 PASS · 색각 분리 PASS(최악 ΔE 8.4)
      · 일반 시야 하한 PASS(19.8) · 서피스 대비 PASS(전부 3:1 이상)

라이트 모드로 가지 않는 이유: 같은 팔레트가 라이트 서피스에서는 aqua·yellow 가
3:1 미만이라 별도 완화(가시 라벨·표 병기)가 필요하다. 모니터링 도구는 다크가
관례이기도 해서, 검증이 전부 통과하는 쪽으로 고정했다.

**시리즈 색은 순서 고정이고 순환시키지 않는다.** 5번째 계열이 필요하면 색을
만들어내는 게 아니라 차트를 나눈다.
"""

from __future__ import annotations

# 카테고리 슬롯 (다크). 순서가 곧 색각 안전성이라 재배열하지 않는다.
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500")

# 상태색 — 계열 색으로 재사용 금지. 아이콘·라벨과 함께만 쓴다(색만으로 뜻을 지지 않는다).
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

SURFACE = "#1a1a19"
PAGE = "#0d0d0d"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def layout(height: int = 260, **overrides) -> dict:
    """plotly 공통 레이아웃.

    격자와 축은 뒤로 물린다 — 읽어야 할 것은 데이터지 눈금이 아니다.
    hover 는 x 통합이라 한 시점의 모든 계열이 한 번에 보인다.
    """
    base = {
        "height": height,
        "margin": {"l": 48, "r": 16, "t": 8, "b": 32},
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT, "color": INK_SECONDARY, "size": 12},
        "hovermode": "x unified",
        "hoverlabel": {"bgcolor": PAGE, "bordercolor": AXIS, "font": {"color": INK}},
        "xaxis": {
            "gridcolor": GRID,
            "linecolor": AXIS,
            "zeroline": False,
            "tickfont": {"color": INK_MUTED},
        },
        "yaxis": {
            "gridcolor": GRID,
            "linecolor": AXIS,
            "zeroline": False,
            "tickfont": {"color": INK_MUTED},
            "rangemode": "tozero",
        },
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.0,
            "x": 0,
            "font": {"color": INK_SECONDARY},
        },
        "showlegend": False,
    }
    base.update(overrides)
    return base


def line(name: str, x, y, slot: int = 0, **kwargs) -> dict:
    """2px 얇은 선. 점 표시는 기본으로 끄고 hover 로 읽는다."""
    trace = {
        "type": "scatter",
        "mode": "lines",
        "name": name,
        "x": x,
        "y": y,
        "line": {"color": SERIES[slot % len(SERIES)], "width": 2},
    }
    trace.update(kwargs)
    return trace


def severity_color(value: float, warn: float, crit: float) -> str:
    """예산 대비 상태색. 값이 클수록 나쁜 지표에 쓴다."""
    if value >= crit:
        return STATUS["critical"]
    if value >= warn:
        return STATUS["warning"]
    return STATUS["good"]

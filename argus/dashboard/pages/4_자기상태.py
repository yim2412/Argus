"""자기 상태 — Argus 자신의 상태.

모니터링 도구의 1순위 실패 모드는 관측 행위가 관측 대상을 오염시키는 것이다.
그래서 이 페이지가 다른 어떤 화면보다 먼저 필요하다.

**RSS 와 private 을 나란히 그리는 이유**가 이 페이지의 핵심이다. 백그라운드 프로세스는
Windows 가 워킹셋을 트림하므로 RSS 가 실제 사용량과 무관하게 내려간다(실측: 95.8MB →
1.0MB, 같은 순간 private 은 85.4MB 그대로). RSS 만 그리면 "메모리가 줄고 있다"는
착시가 생기고, 그 착시 위에서 누수 판정을 하면 틀린다.
"""

from __future__ import annotations

import json
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

st.set_page_config(page_title="Argus · 자기 상태", page_icon="👁", layout="wide")
page_header("자기 상태", "관측자가 병목이 되고 있지 않은가")
sidebar_status()

hours = st.select_slider("구간", options=[1, 2, 4, 8, 24, 72], value=8, format_func=lambda h: f"{h}시간")
rows = data.self_telemetry(hours=hours)

if not rows:
    empty("자기 계측 기록이 없습니다. `python -m argus` 로 수집을 시작하세요.")
    st.stop()

latest = rows[-1]
ts = timestamps(rows)

# ---------------------------------------------------------------- 요약

cols = st.columns(5)
with cols[0]:
    cpu = latest["cpu_percent"] or 0.0
    st.metric("CPU", f"{cpu:.2f}%", help="예산 2.0% (머신 전체 기준으로 정규화)")
    st.caption(f"예산의 {cpu / 2.0 * 100:.0f}%")
with cols[1]:
    private = latest.get("private_mb")
    st.metric("private", f"{private:.0f} MB" if private else "—", help="누수 판정의 정본")
    st.caption("트림에 영향받지 않음")
with cols[2]:
    st.metric("RSS", f"{latest['rss_mb']:.0f} MB", help="예산 300MB. 워킹셋 트림에 따라 내려간다")
with cols[3]:
    st.metric("유실", f"{latest['drop_count']:,}", help="큐가 가득 차 버린 누적 행 수. 0 이어야 한다")
with cols[4]:
    st.metric("핸들", f"{latest['handles']:,}" if latest["handles"] else "—")

if latest["drop_count"]:
    st.error(
        f"수집 유실 {latest['drop_count']:,}행 — 큐가 가득 차 오래된 표본을 버렸습니다. "
        "저장이 수집을 못 따라가고 있습니다."
    )

# ---------------------------------------------------------------- 메모리

st.markdown("#### 메모리 — RSS 와 private")
has_private = any(row.get("private_mb") is not None for row in rows)
if has_private:
    chart(
        [
            theme.line("private (커밋)", ts, series(rows, "private_mb"), slot=0),
            theme.line("RSS (물리)", ts, series(rows, "rss_mb"), slot=1),
            theme.line("peak working set", ts, series(rows, "peak_wset_mb"), slot=2, line={"color": theme.SERIES[2], "width": 2, "dash": "dot"}),
        ],
        legend=True,
        height=280,
    )
    first = next((r for r in rows if r.get("private_mb") is not None), None)
    if first and first is not latest and first.get("private_mb"):
        delta = (latest["private_mb"] or 0) - first["private_mb"]
        span_h = (latest["ts"] - first["ts"]) / 3600
        if span_h > 0.2:
            rate = delta / span_h
            color = theme.severity_color(rate, warn=2.0, crit=10.0)
            st.markdown(
                f"private 증가율 **<span style='color:{color}'>{rate:+.2f} MB/시간</span>** "
                f"({span_h:.1f}시간 동안 {delta:+.1f}MB)",
                unsafe_allow_html=True,
            )
            st.caption(
                "누수는 여기서 보인다. RSS 가 내려가도 private 이 오르면 실제로는 쓰고 있는 것이다."
            )
else:
    chart([theme.line("RSS", ts, series(rows, "rss_mb"), slot=1)], height=280)
    st.warning(
        "이 구간에는 `private_mb` 가 없습니다(스키마 v5 이전 기록). "
        "RSS 만으로는 워킹셋 트림과 실제 감소를 구분할 수 없습니다."
    )

# ---------------------------------------------------------------- CPU·지연

left, right = st.columns(2)
with left:
    st.markdown("#### CPU (예산 2%)")
    chart([theme.line("CPU", ts, series(rows, "cpu_percent"), slot=0)], height=200)
with right:
    st.markdown("#### 쓰기 지연 · 큐 깊이")
    chart(
        [
            theme.line("쓰기 지연 (ms)", ts, series(rows, "write_latency_ms"), slot=3),
            theme.line("큐 깊이", ts, series(rows, "queue_depth"), slot=1),
        ],
        legend=True,
        height=200,
    )

throttled = [r for r in rows if r["throttle_level"]]
if throttled:
    st.warning(
        f"스로틀이 걸린 표본 {len(throttled)}개 (최대 레벨 "
        f"{max(r['throttle_level'] for r in throttled)}) — 예산 초과로 수집 주기를 늦췄습니다."
    )

# ---------------------------------------------------------------- 저장소

st.markdown("#### 저장소")
state = data.rollup_state()
counts = data.table_counts()

scol = st.columns(4)
with scol[0]:
    st.metric("DB", f"{data.db_size_bytes() / 1048576:.1f} MB")
with scol[1]:
    warm_bytes = data.warm_size_bytes()
    span = data.warm_span()
    st.metric("웜 스토어", f"{span['days']}일치" if span else "—")
    if span:
        size = f"{warm_bytes / 1024:.0f} KB" if warm_bytes < 1048576 else f"{warm_bytes / 1048576:.1f} MB"
        st.caption(f"{span['lo'][5:]}~{span['hi'][5:]} · {size}")
    else:
        st.caption("아직 내보낸 날짜가 없습니다")
with scol[2]:
    if state:
        lag_min = (time.time() - state["watermark_ts"]) / 60
        st.metric("롤업 지연", f"{lag_min:.0f}분")
        st.caption("정상 범위 2~3분")
    else:
        st.metric("롤업 지연", "—")
        st.caption("아직 실행되지 않음")
with scol[3]:
    span = data.rollup_span()
    st.metric("1분 집계", f"{span['n']:,}분" if span else "—")

if state is None:
    st.warning(
        "롤업이 아직 돌지 않았습니다. **원본 정리도 함께 멈춰 있습니다** — "
        "접히기 전에 지우면 그 구간은 어디에도 남지 않기 때문입니다."
    )

st.caption(
    "이 표는 **핫 저장소(SQLite) 보유분**입니다. 롤업(`metrics_1m`·`process_5m`·"
    "`net_activity_5m`)은 이틀이 지나면 웜 스토어(Parquet)로 옮겨가므로 여기서는 "
    "보유 시간이 줄어듭니다 — 사라지는 것이 아니라 위 '웜 스토어'로 이동합니다. "
    "대시보드와 착수 판정은 두 계층을 합쳐 읽습니다."
)
st.dataframe(
    [
        {
            "테이블": row["table"],
            "행": f"{row['n']:,}",
            "시작": f"{datetime.fromtimestamp(row['lo']):%m-%d %H:%M}",
            "끝": f"{datetime.fromtimestamp(row['hi']):%m-%d %H:%M}",
            "보유": f"{(row['hi'] - row['lo']) / 3600:.1f}h",
        }
        for row in counts
    ],
    width='stretch',
    hide_index=True,
)

# ---------------------------------------------------------------- 사건·평가

_CAUSE_KO = {
    "suspend_or_stall": "절전·정지",
    "clock_change": "시각 변경",
    "clock_backwards": "시각 역행",
    "reboot_or_power_loss": "재부팅·전원 차단",
    "process_killed_or_crash": "강제 종료·크래시",
    "unknown": "불명",
}


def _cause(detail: str | None) -> str:
    if not detail:
        return ""
    try:
        cause = json.loads(detail).get("likely_cause")
    except (ValueError, AttributeError):
        return ""
    return _CAUSE_KO.get(cause, cause or "")


ecol, vcol = st.columns(2)
with ecol:
    st.markdown("#### 시스템 사건")
    events = data.system_events(hours=hours)
    if events:
        st.dataframe(
            [
                {
                    "시각": f"{datetime.fromtimestamp(e['ts']):%m-%d %H:%M:%S}",
                    "사건": e["event"],
                    "공백(초)": f"{e['gap_seconds']:.0f}" if e["gap_seconds"] else "",
                    # 사건 이름만으로는 "왜"가 안 보인다. 절전인지 재부팅인지 크래시인지가
                    # 사후 진단의 전부라, detail 안의 추정 원인을 끌어올린다.
                    "추정 원인": _cause(e["detail"]),
                }
                for e in events[:15]
            ],
            width='stretch',
            hide_index=True,
        )
    else:
        st.caption("절전·시각 변경 없음")

with vcol:
    st.markdown("#### 탐지기 스코어보드")
    runs = data.eval_runs(limit=12)
    if runs:
        st.dataframe(
            [
                {
                    "탐지기": r["detector"],
                    "F1": f"{r['f1']:.3f}" if r["f1"] is not None else "—",
                    "정밀도": f"{r['precision_pct']:.0f}%" if r["precision_pct"] is not None else "—",
                    "재현율": f"{r['recall_pct']:.0f}%" if r["recall_pct"] is not None else "—",
                    "오탐/h": f"{r['fp_per_hour']:.2f}" if r["fp_per_hour"] is not None else "—",
                    "실행": f"{datetime.fromtimestamp(r['ts']):%m-%d %H:%M}",
                }
                for r in runs
            ],
            width='stretch',
            hide_index=True,
        )
        st.caption("`python -m argus.eval --detector all --save` 로 갱신됩니다.")
    else:
        st.caption("평가 실행 기록이 없습니다.")

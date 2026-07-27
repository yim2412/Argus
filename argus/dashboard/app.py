"""Argus 대시보드 진입점.

실행: `python -m argus.dashboard` (또는 `streamlit run argus/dashboard/app.py`)

Streamlit 은 같은 폴더의 `pages/` 를 자동으로 멀티페이지로 잡는다.
지금 있는 페이지는 데이터가 이미 존재하는 것들뿐이다 — 레짐(Phase 4-B)과
모델(학습된 모델)은 그 단계가 오면 붙인다. 빈 화면을
미리 만들어 두면 "아직 없음"과 "고장남"을 구분할 수 없다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit 은 페이지 파일을 `exec` 으로 돌린다 — 패키지 컨텍스트가 없어서
# 상대 import(`from . import ...`)가 통째로 실패한다. HTTP 200 이 돌아와도
# 페이지를 열면 터지므로, 절대 import 로 쓰고 루트를 직접 세운다.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from argus.dashboard import data  # noqa: E402
from argus.dashboard.common import page_header, sidebar_status  # noqa: E402

st.set_page_config(page_title="Argus", page_icon="👁", layout="wide")

page_header("Argus", "PC 성능 이상 탐지 — 평소와 다른 상태를 찾고 원인을 지목한다")

if not data.db_exists():
    st.error(
        "데이터베이스가 없습니다. `python -m argus` 로 수집을 먼저 시작하세요.\n\n"
        f"찾은 위치: `{data.db_path()}`"
    )
    st.stop()

sidebar_status()

span = data.rollup_span()
counts = {row["table"]: row for row in data.table_counts()}

col1, col2, col3 = st.columns(3)
raw = counts.get("metrics_raw")
with col1:
    st.metric("원본 보유", f"{(raw['hi'] - raw['lo']) / 3600:.1f}시간" if raw else "—")
    st.caption(f"{raw['n']:,}행" if raw else "수집 전")
with col2:
    st.metric("1분 집계", f"{span['n']:,}분" if span else "—")
    st.caption(f"{(span['hi'] - span['lo']) / 3600:.1f}시간 분량" if span else "롤업 전")
with col3:
    st.metric("DB 크기", f"{data.db_size_bytes() / 1048576:.1f} MB")
    warm = data.warm_size_bytes()
    st.caption(f"웜 스토어 {warm / 1024:.0f} KB" if warm else "웜 파티션 없음")

st.markdown(
    """
### 페이지

- **실시간** — 지금 이 순간의 상태와 최근 10분
- **타임라인** — 1분 집계 시간축. 결함 주입 구간과 탐지 신호가 겹쳐 보인다
- **프로세스** — 프로세스별 사용량 랭킹과 포어그라운드
- **자기 상태** — Argus 자신의 상태. 관측자가 병목이 되고 있지 않은지
- **사건** — 무슨 일이 있었고 **왜** 그랬는가

준비 중: 레짐(Phase 4-B) · 모델(학습 후)
"""
)

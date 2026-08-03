"""네이티브 창(PySide6)과 그 조회 계층.

**창을 띄우지 않는다.** 화면에 보이는지는 사람이 봐야 하고, 마우스를 움직이는
자동화는 쓰지 않는다(CLAUDE.md). 여기서 고정하는 것은 UI 없이 확인되는 것들이다 —
캐시 의미, 모니터 배치 규칙, 그리고 **조회 계층이 Streamlit 없이 도는가**.

마지막 항목이 이 파일의 핵심이다. `data.py` 가 UI 프레임워크에 묶여 있으면 창을
바꿀 때마다 조회 코드까지 따라 옮겨야 한다.
"""

from __future__ import annotations

import sys
import time

import pytest


# ---------------------------------------------------------------- 조회 계층 독립성

def test_data_layer_does_not_require_streamlit() -> None:
    """**조회 계층은 UI 를 모른다.**

    2026-08-03 까지 캐시가 `st.cache_data` 라 `data.py` 가 Streamlit 없이는 import 조차
    되지 않았다. 네이티브 창으로 옮기는 순간 이 계층이 발목을 잡았을 것이다.
    """
    for module in [m for m in list(sys.modules) if m.startswith("streamlit")]:
        del sys.modules[module]

    import argus.dashboard.data  # noqa: F401

    assert "streamlit" not in sys.modules, "조회 계층이 Streamlit 을 끌어들인다"


# ---------------------------------------------------------------- TTL 캐시

def test_ttl_cache_reuses_within_window() -> None:
    from argus.dashboard.data import ttl_cache

    calls = []

    @ttl_cache(60.0)
    def fetch(n: int) -> int:
        calls.append(n)
        return n * 2

    assert fetch(1) == 2
    assert fetch(1) == 2
    assert calls == [1], "창 안인데 다시 조회했다"

    assert fetch(2) == 4
    assert calls == [1, 2], "인자가 다르면 따로 캐시해야 한다"


def test_ttl_cache_expires() -> None:
    """**시간 기반 만료가 없으면 대시보드가 영원히 옛 값을 보여준다.**

    `functools.lru_cache` 를 쓰지 않는 이유가 이것이다.
    """
    from argus.dashboard.data import ttl_cache

    calls = []

    @ttl_cache(0.05)
    def fetch() -> int:
        calls.append(1)
        return len(calls)

    assert fetch() == 1
    time.sleep(0.08)
    assert fetch() == 2, "TTL 이 지났는데 옛 값을 돌려줬다"


def test_realtime_ttl_matches_collection_period() -> None:
    """실시간 조회 캐시가 수집 주기(1초)보다 길면 갱신이 그만큼 느려진다.

    예광탄 실측: TTL 2초일 때 **12초에 6개**만 그렸다. 창은 1초마다 물었는데 캐시가
    절반을 옛 값으로 돌려준 것이다. 눈으로만 봤으면 "좀 굼뜬가" 로 넘어갔을 문제다.
    """
    from argus.dashboard import data

    assert data.latest_metrics.cache_ttl <= 1.0, (
        f"실시간 지표 캐시가 {data.latest_metrics.cache_ttl}초 — 1초 주기 갱신을 막는다"
    )
    assert data.latest_gpu.cache_ttl <= 1.0


# ---------------------------------------------------------------- 모니터 배치

@pytest.mark.parametrize(
    "value,expected_fragment",
    [("", "기본 위치"), ("abc", "못 읽었다"), ("99", "없음")],
)
def test_screen_placement_falls_back_quietly(monkeypatch, value, expected_fragment) -> None:
    """**개발 편의 기능이 실행을 막으면 안 된다.**

    지정이 없거나 그런 모니터가 없으면 기본 위치로 간다. 예외를 던지면 창이 아예
    안 뜬다.
    """
    pytest.importorskip("PySide6")
    from argus.desktop.app import ENV_SCREEN, place_on_configured_screen

    monkeypatch.setenv(ENV_SCREEN, value)

    class _FakeWindow:
        def rect(self):
            return None

        def move(self, *_args):
            raise AssertionError("기본 위치여야 하는데 창을 옮겼다")

    result = place_on_configured_screen(_FakeWindow())
    assert expected_fragment in result, result

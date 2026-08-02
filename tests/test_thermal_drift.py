"""냉각 열화 — 같은 부하에서 예전보다 뜨거운가.

**결과를 이미 아는 데이터 두 개로 보정한다.** 실제 데이터(이 PC, 부하 시 온도가 6일 내내
93.0도로 불변)에서는 발화하지 않아야 하고, 열화를 넣은 데이터에서는 발화해야 한다.
한쪽만 보면 "아무것도 발화하지 않는" 구현과 "무조건 발화하는" 구현이 각각 통과한다.

여기서 지키는 설계는 셋이다.

    부하를 맞춰 비교한다  — 유휴를 섞으면 "게임을 많이 했다"가 "냉각이 나빠졌다"가 된다
    표본이 모자라면 침묵  — 데이터가 없는데 "정상"이라고 하는 것도 거짓말이다
    절대 온도 문턱이 없다 — 노트북(87도)이든 데스크탑(70도)이든 같은 코드가 맞아야 한다
"""

from __future__ import annotations

import pytest

from argus.config.loader import ThermalDriftSettings
from argus.detection.thermal import assess


def _days(temps: list[float], start_day: int = 1) -> dict[str, float]:
    """`{'2026-07-01': 93.0, ...}` 형태로. 날짜 문자열 정렬이 곧 시간 순서다."""
    return {f"2026-07-{start_day + i:02d}": t for i, t in enumerate(temps)}


def test_stable_cooling_says_nothing() -> None:
    """온도가 그대로면 발화하지 않는다.

    이 PC 의 실제 모양이다 — 부하 구간(`gpu_util_mean >= 80`) 온도가 6일 내내 정확히
    93.0도였다(사용률 89~98%, 전력 219~228W). 절대 온도로는 시간의 19.7% 가 90도를
    넘지만 냉각은 멀쩡하다.
    """
    assert assess(_days([93.0] * 10), ThermalDriftSettings()) is None


def test_gradual_rise_is_caught() -> None:
    """같은 부하에서 온도가 오르면 잡는다. 먼지·서멀 노화가 이 모양이다."""
    daily = _days([88.0] * 7 + [95.0, 96.0, 95.0])
    verdict = assess(daily, ThermalDriftSettings())

    assert verdict is not None, "7도 상승을 놓쳤다"
    assert verdict.rise_c == pytest.approx(7.0, abs=0.5)
    assert verdict.baseline_c == pytest.approx(88.0)
    assert verdict.recent_c == pytest.approx(95.0)
    assert "먼지" in verdict.explain and "95도" in verdict.explain.replace("95.", "95")


def test_small_wobble_is_ignored() -> None:
    """문턱 아래의 흔들림은 무시한다. 1~2도는 실내 온도로도 움직인다."""
    daily = _days([88.0, 89.0, 88.0, 87.0, 88.0, 89.0, 88.0, 90.0, 89.0, 90.0])
    assert assess(daily, ThermalDriftSettings()) is None


def test_cooling_improvement_is_not_reported() -> None:
    """청소 뒤처럼 **내려간** 경우는 알리지 않는다. 좋아진 것은 사건이 아니다."""
    daily = _days([95.0] * 7 + [86.0, 85.0, 86.0])
    assert assess(daily, ThermalDriftSettings()) is None


def test_too_few_days_stays_silent() -> None:
    """표본이 모자라면 판정하지 않는다 — 부트스트랩 기간(탐지 규칙 4).

    **"정상"이라고 답하지 않는 것이 요점이다.** 데이터가 없는데 정상이라고 하면 그것도
    거짓말이고, 사용자는 확인받았다고 믿는다.
    """
    settings = ThermalDriftSettings()
    # 상승은 뚜렷하지만 표본이 min_days 에 못 미친다.
    daily = _days([88.0, 88.0, 96.0, 97.0, 96.0])
    assert len(daily) < settings.min_days
    assert assess(daily, settings) is None


def test_threshold_comes_from_config() -> None:
    """상승 문턱이 config 에서 온다(규칙 3). 코드에 박혀 있으면 튜닝할 수 없다."""
    daily = _days([88.0] * 7 + [90.0, 90.0, 90.0])  # +2.0도

    assert assess(daily, ThermalDriftSettings()) is None, "기본 문턱(3도)에서는 조용해야 한다"
    loose = assess(daily, ThermalDriftSettings(rise_c=1.5))
    assert loose is not None, "문턱을 1.5도로 낮췄는데 반영되지 않았다"
    assert loose.rise_c == pytest.approx(2.0)


def test_no_absolute_temperature_threshold_anywhere() -> None:
    """**절대 온도 문턱이 없다**(규칙 2). 뜨거운 노트북에서도 같은 판정이어야 한다.

    같은 상승폭을 60도대와 90도대에서 각각 준다. 어딘가에 절대 문턱이 숨어 있으면
    둘 중 하나만 잡힌다 — GPU 온도 룰이 정확히 그래서 노트북에서 상시 경고가 됐다.
    """
    settings = ThermalDriftSettings()
    cool = assess(_days([62.0] * 7 + [69.0, 70.0, 69.0]), settings)
    hot = assess(_days([88.0] * 7 + [95.0, 96.0, 95.0]), settings)

    assert cool is not None, "시원한 GPU 의 7도 상승을 놓쳤다"
    assert hot is not None, "뜨거운 GPU 의 7도 상승을 놓쳤다"
    assert cool.rise_c == pytest.approx(hot.rise_c, abs=0.5), (
        f"같은 상승폭인데 판정이 다르다: {cool.rise_c} vs {hot.rise_c} — 절대 문턱이 숨어 있다"
    )

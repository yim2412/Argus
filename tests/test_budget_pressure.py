"""RSS 경고선은 실제 압박이 동반될 때만 스로틀한다 (2026-08-25).

**왜 바꿨나.** 두 기계 186,840 표본에서 스로틀이 막아낸 사고가 0건이었다. 스로틀이
걸리지 않은 `lv0` 구간이 곧 "막지 않았으면 무엇이 일어났나"의 대조군인데, 거기서도
`drop_count` 0 · 큐 최대 12.0%/4.8% 였다. 반대로 스로틀 3 은 수집 주기를 x10 으로
늘려 관측 해상도를 1/10 로 떨어뜨린다 — 이득 없이 손해만 내고 있었다.

**이 파일이 재는 것.** 보호 장치를 재는 테스트는 "막지 않았으면 무엇이 일어났을
것인가"를 먼저 단언해야 한다. 그게 없으면 완화를 통째로 뜯어내도 결과가 같아
전부 통과한다. 그래서 모든 케이스가 **같은 입력에서 압박만 바꾼 쌍**으로 되어 있다.

로직과 배선을 따로 잰다. 배선은 **기본값이 아닌 값**으로 재야 한다 —
`assert guard.settings.x == cfg.x` 는 코드 기본값과 YAML 기본값이 같으면
배선이 끊겨도 참이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import BudgetSettings  # noqa: E402
from argus.runtime.budget import BudgetGuard  # noqa: E402
from argus.runtime.stats import STATS  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_stats():
    """STATS 는 프로세스 전역이라 테스트 간에 샌다."""
    STATS.set_queue_depth(0)
    STATS.set_queue_capacity(0)
    yield
    STATS.set_queue_depth(0)
    STATS.set_queue_capacity(0)


def _settings(**over) -> BudgetSettings:
    base = dict(
        cpu_percent=9999.0,  # CPU 로는 절대 안 걸리게 — RSS 축만 재려는 것이다
        rss_mb=1,            # 실제 RSS 는 항상 이보다 크므로 경고선은 늘 초과 상태
        rss_hard_mb=10_000,  # 안전망에는 안 닿게
        breach_streak_to_throttle=2,
    )
    base.update(over)
    return BudgetSettings(**base)


def _pump(guard: BudgetGuard, times: int = 3) -> int:
    for _ in range(times):
        level = guard.update()
    return level


def test_soft_breach_alone_does_not_throttle():
    """경고선만 넘고 압박이 없으면 스로틀하지 않는다."""
    guard = BudgetGuard(_settings())
    assert _pump(guard) == 0


def test_soft_breach_with_queue_pressure_throttles():
    """같은 입력에 큐 압박만 더하면 스로틀한다 — 위 테스트의 대조 짝.

    이것이 없으면 `update()` 에서 RSS 조건을 통째로 지워도 위 테스트가 통과한다.
    """
    guard = BudgetGuard(_settings(pressure_queue_ratio=0.5))
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(60)  # 60% — 문턱 50% 초과
    assert _pump(guard) >= 1


def test_queue_below_ratio_is_not_pressure():
    """문턱 아래 큐는 압박이 아니다. 실측 최대(12%)가 여기 해당한다."""
    guard = BudgetGuard(_settings(pressure_queue_ratio=0.5))
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(12)
    assert _pump(guard) == 0


def test_sustained_drops_are_pressure():
    """계속 버리고 있으면 큐가 비어 있어도 압박이다 — 이미 손해가 나는 중이다."""
    guard = BudgetGuard(_settings())
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(0)
    level = 0
    for _ in range(3):
        STATS.add_drops(1)  # 매 주기 유실이 이어진다
        level = guard.update()
    assert level >= 1


def test_a_single_drop_does_not_throttle():
    """단발 유실은 스로틀하지 않는다 — 순간 스파이크는 이상이 아니다(탐지 규칙 1).

    히스테리시스(`breach_streak_to_throttle`)가 이것을 걸러낸다. 위 테스트와 다른 것은
    **유실이 이어지는가** 하나뿐이고, 그 차이가 판정을 가르는 것이 이 설계의 핵심이다.
    """
    guard = BudgetGuard(_settings())
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(0)
    STATS.add_drops(1)  # 한 번뿐
    assert _pump(guard) == 0


def test_drop_count_is_a_delta_not_a_level():
    """`drop_count` 는 누적값이다. 옛날에 한 번 버린 것이 영구 압박이 되면 안 된다.

    이 규칙이 없으면 상주가 켜진 뒤 단 한 번의 유실로 남은 수명 내내 스로틀이 걸린다.
    """
    STATS.add_drops(5)  # 가드가 생기기 **전에** 버린 것
    guard = BudgetGuard(_settings())
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(0)
    assert _pump(guard) == 0


def test_unknown_capacity_is_not_pressure():
    """큐 상한을 모르면 압박이 아니다.

    모르는 것을 압박으로 읽으면 상한 등록이 끊긴 순간 **상시 스로틀**이 된다.
    조용히 깨지는 쪽이라 예외로는 안 잡힌다.
    """
    guard = BudgetGuard(_settings(pressure_queue_ratio=0.5))
    STATS.set_queue_capacity(0)   # 등록 안 됨
    STATS.set_queue_depth(99_999)
    assert _pump(guard) == 0


def test_hard_limit_throttles_without_pressure():
    """안전망은 압박과 무관하게 건다 — 08-12 형 워킹셋 폭주(745MB)를 잡는 자리."""
    guard = BudgetGuard(_settings(rss_hard_mb=2))  # 실제 RSS(수십 MB)는 늘 이보다 크다
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(0)
    assert _pump(guard) >= 1


def test_hard_must_exceed_soft():
    """안전망이 경고선 이하면 경고선이 도달 불가가 되어 완화가 통째로 죽는다."""
    with pytest.raises(ValueError):
        BudgetSettings(rss_mb=300, rss_hard_mb=300)


def test_queue_registers_its_own_capacity():
    """큐가 자기 상한을 등록한다. 문턱이 예산 절에 복제되면 설정이 두 곳이 된다."""
    from argus.storage.queue import SampleQueue

    SampleQueue(maxsize=4321)
    assert STATS.snapshot().queue_capacity == 4321


def test_yaml_wiring_reaches_the_guard():
    """배선 — YAML 값이 실제 판정을 움직이는가.

    **기본값(600 / 0.5)을 쓰지 않는다.** 코드 기본값과 YAML 기본값이 같으면
    배선이 끊겨도 통과하기 때문이다. 값이 아니라 *판정이 갈리는지*를 본다.
    """
    STATS.set_queue_capacity(100)
    STATS.set_queue_depth(30)  # 30%

    lenient = BudgetGuard(_settings(pressure_queue_ratio=0.77))  # 30% < 77% -> 조용
    assert _pump(lenient) == 0

    strict = BudgetGuard(_settings(pressure_queue_ratio=0.11))   # 30% > 11% -> 스로틀
    assert _pump(strict) >= 1

"""힙 센서스가 실제로 무엇을 보는가 (2026-08-25).

**왜 만들었나.** 두 기계 모두 `private_mb` 가 프로세스 수명 내내 단조 증가하는데
(노트북 부팅 후 117시간에 40 -> 198MB · 이 PC 세션마다 52 -> 250MB), 같은 구간에서
`threads`(36.9)·`handles`(472) 는 소수점까지 상수였다. 스레드도 핸들도 아니고
파이썬 힙 안에서 자란다는 뜻이라, `gc` 가 보는 것을 직접 세기로 했다.

**이 파일이 지키는 것은 "개수만 세면 안 된다"이다.** CPython 은 원자값만 든 컨테이너를
untrack 하고 `str`·`bytes`·`int` 는 애초에 추적하지 않는다. 캐시가 대개 그 모양이라
개수만 보면 **정작 자라는 것을 통째로 놓친다.** 개발 중 스모크가 이 결함을 잡았고,
그 순간을 여기 고정한다 — 나중에 "원소 수는 왜 세지"라는 질문이 오면 이 테스트가 답이다.

**그리고 원소 수도 만능이 아니다.** untracked 객체는 `gc.get_objects()` 에 나오지
않으므로 자신의 크기를 물을 수조차 없고, 잡히는 것은 *그것을 담은 tracked 컨테이너가
커졌다*는 간접 신호뿐이다. 이 한계는
`test_untracked_objects_are_caught_indirectly_by_their_holder` 에 적어 두었다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.runtime.heapcensus import census  # noqa: E402

_N = 30_000


def test_tracked_containers_show_up_in_the_object_count():
    """값이 컨테이너인 dict 는 gc 가 추적한다 — 개수로 보인다."""
    before, _, _, _ = census(top_n=5)
    ballast = [{"k": []} for _ in range(_N)]
    after, _, _, _ = census(top_n=5)
    del ballast
    assert after - before >= _N


def test_untracked_containers_are_invisible_to_the_object_count():
    """**문자열만 든 dict 는 개수로 안 보인다.** 이것이 원소 수를 세는 이유다.

    이 단언이 깨졌다면 CPython 의 untrack 동작이 바뀐 것이다. 그때는 통과가 아니라
    **이 계측의 전제를 다시 보라는 신호**로 읽어야 한다.
    """
    before, _, _, _ = census(top_n=5)
    ballast = [{"k": "v" * 8} for _ in range(_N)]
    after, _, _, _ = census(top_n=5)
    del ballast
    assert after - before < _N // 10


def test_untracked_objects_are_caught_indirectly_by_their_holder():
    """개수로 안 보이는 것이 **그것을 담은 컨테이너의 원소 수**로 잡힌다.

    ⚠️ 정확히 하자. 원소 수는 untracked 객체 *자신* 을 세지 않는다 — 그것들은
    `gc.get_objects()` 에 아예 나오지 않으므로 `len()` 을 물을 대상조차 없다.
    잡히는 것은 **그것들을 담고 있는 tracked 컨테이너가 커졌다**는 사실이다.
    실제로 자라는 자료구조는 거의 항상 무언가에 담겨 있으므로 이걸로 방향은 잡힌다.

    **그래서 한계가 있다.** 최상위 컨테이너 자신이 untracked 인 경우(예: 모듈 어디에도
    안 붙고 str -> str 만 담는 캐시)는 이 계측이 못 본다. 원소 수로도 정체가 안
    잡히면 그때가 `tracemalloc` 을 진단 모드로 도입할 시점이다.
    """
    _, before, _, _ = census(top_n=5)
    ballast = [{"k": "v" * 8} for _ in range(_N)]
    _, after, _, _ = census(top_n=5)
    del ballast
    # 정확한 수를 단언하지 않는다 — 두 census 사이에 인터프리터가 자기 객체를
    # 만들고 버리므로 몇십 개는 늘 흔들린다. 재려는 것은 "3만 규모가 보이는가"다.
    assert after - before >= _N * 0.9


def test_top_types_is_ranked_by_items_not_count():
    """상위 선정은 원소 수 기준이다.

    개수 기준으로 뽑으면 `function`·`wrapper_descriptor` 가 늘 상위를 차지해
    **무엇이 자라는지 말해 주지 않는다** — 자라는 쪽은 대개 큰 컨테이너다.
    """
    ballast = [{"k": "v"} for _ in range(_N)]
    _, _, _, top = census(top_n=5)
    del ballast
    ranked = list(top)
    assert ranked[0] == "dict"
    # 개수 기준이었다면 원소 0 인 타입이 상위에 섞인다.
    assert all(top[k]["items"] > 0 for k in ranked[:3])


def test_scan_reports_its_own_cost():
    """관측자는 자기 비용을 기록한다 (설계 규칙 1)."""
    _, _, ms, _ = census(top_n=5)
    assert ms > 0.0


def test_census_survives_a_hostile_len():
    """남의 `__len__` 이 터져도 계측은 계속된다.

    개별 실패가 전체를 죽이지 않는다 — 수집 규칙 1 의 계측판이다.
    """

    class Hostile(list):
        def __len__(self) -> int:
            raise RuntimeError("일부러 터뜨린다")

    bomb = Hostile()
    total, items, ms, top = census(top_n=5)
    del bomb
    assert total > 0 and items >= 0 and ms > 0.0

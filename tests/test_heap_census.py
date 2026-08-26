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


def test_zero_item_types_are_kept_for_time_series_comparison():
    """원소가 0인 타입도 담긴다 — 없으면 시계열 비교가 어긋난다.

    원소 수로만 상위를 고르면 `function`·`type` 처럼 원소가 0인 타입이 표본마다
    들락날락한다. 실제로 첫 30분 데이터에서 `type` 이 "0 -> 1,252" 로 보였는데
    늘어난 것이 아니라 앞 표본의 목록에 없었을 뿐이었다(2026-08-25). 그 상태로는
    **클래스가 실제로 누적되는 경우와 구분할 방법이 없다.**
    """
    _, _, _, top = census(top_n=20)
    zero_item = [k for k, v in top.items() if v["items"] == 0]
    assert zero_item, "원소 0인 타입이 하나도 안 담기면 개수 축이 통째로 안 보인다"
    # function 은 어느 파이썬 프로세스에서도 개수 최상위권이다.
    assert "function" in top


def test_ranking_is_stable_across_samples():
    """연속한 두 표본의 상위 목록이 크게 흔들리지 않는다.

    흔들리면 "늘었다"가 *실제 증가* 인지 *목록에 새로 들어온 것* 인지 가릴 수 없다.
    """
    _, _, _, a = census(top_n=20)
    _, _, _, b = census(top_n=20)
    common = set(a) & set(b)
    assert len(common) >= len(a) * 0.9


# --- 스파이크 프로브 (2026-08-26) -------------------------------------------
#
# **왜 붙였나.** 08-26 19:45 에 `container_items` 가 한 틱만 98,604 -> 147,308 로
# 튀었다가 다음 틱에 사라졌다. `list` 가 9개 늘고 원소가 +52,355 인데 `tuple` 은
# 오히려 줄어 "원소가 튜플이 아닌 큰 리스트 몇 개"까지는 읽혔지만, **어느 리스트인지
# 남은 것이 없어 거기서 끝났다.** 롤업의 float 시계열을 의심했으나 census 291건 중
# 282건이 프로세스 롤업 완료 2초 이내인데 전부 평상치라 기각됐다. 다음 스파이크가
# 스스로 정체를 말하게 하는 것이 이 프로브의 존재 이유다.

import threading  # noqa: E402

from argus.config.loader import load_settings  # noqa: E402
from argus.runtime.heapcensus import HeapCensus, probe_largest  # noqa: E402


def test_probe_names_the_thread_holding_a_local():
    """**다른 스레드의 지역 변수**를 지목한다.

    이것이 이 프로브의 핵심이고, `gc.get_referrers()` 만으로는 **불가능하다** —
    CPython 3.12 에서 함수 지역 변수를 담은 리스트의 참조자를 물으면 0건이 돌아온다
    (2026-08-26 실측). 한 틱만에 나타났다 사라지는 스파이크가 바로 그 모양이므로,
    이 경로가 죽으면 정작 찾으려던 것을 영영 못 본다.
    """
    go, done = threading.Event(), threading.Event()

    def worker():
        haystack = list(range(150_000))  # noqa: F841
        go.set()
        done.wait(10)
        del haystack

    t = threading.Thread(target=worker, name="spike-victim")
    t.start()
    try:
        go.wait(10)
        found = probe_largest(top_k=3)
    finally:
        done.set()
        t.join(10)

    holders = [h for f in found for h in f["holders"]]
    assert any("spike-victim" in h and "worker()" in h for h in holders), holders


def test_probe_leaves_no_content_behind():
    """**내용은 남기지 않는다**(설계 규칙 5).

    컨테이너 안에는 프로세스명·경로·네트워크 목적지가 들어 있을 수 있다. 프로브가
    남기는 것은 타입·크기·원소 타입 이름·보유자뿐이어야 한다. 값이 새어 나가면
    로그가 곧 개인정보가 된다.
    """
    secret = "s3cret-process-name-\u314f"
    ballast = [secret] * 200_000  # noqa: F841
    found = probe_largest(top_k=2)
    del ballast
    assert secret not in repr(found)


def test_spike_branch_stays_quiet_when_nothing_spikes():
    """평상시에는 프로브를 부르지 않는다 — 관측자는 가벼워야 한다(설계 규칙 1)."""
    calls = []
    census_ = HeapCensus(_FakeDb(), spike_ratio=1.3)
    census_._last_items = 100_000
    _run_tick(census_, total_items=101_000, on_probe=calls.append)
    assert calls == []


def test_spike_branch_fires_when_it_spikes():
    """**막지 않았으면 무엇이 일어났을 것인가** — 같은 값이 문턱 아래면 조용하고,
    위면 프로브가 돈다. 두 방향을 다 재야 문턱이 실제로 무언가를 가른다."""
    calls = []
    census_ = HeapCensus(_FakeDb(), spike_ratio=1.3)
    census_._last_items = 100_000
    _run_tick(census_, total_items=150_000, on_probe=calls.append)
    assert len(calls) == 1


def test_spike_ratio_is_wired_from_config():
    """**기본값이 아닌 값으로 잰다.** 코드 기본값(1.3)과 YAML 기본값이 같으면
    `assert engine.x == cfg.x` 는 배선이 끊겨도 참이다(2026-08-04 에 네 번 당했다).
    그래서 기본값과 다른 값을 넣고, 그 값이 실제 판정을 바꾸는지 본다."""
    import os

    os.environ["ARGUS_HEAP_CENSUS__SPIKE_RATIO"] = "2.5"
    try:
        cfg = load_settings(use_user_file=False)
    finally:
        del os.environ["ARGUS_HEAP_CENSUS__SPIKE_RATIO"]
    assert cfg.heap_census.spike_ratio == 2.5

    # 그 값으로 만든 컴포넌트는 1.5배에 **침묵해야** 한다 — 기본값 1.3 이었다면 울린다.
    calls = []
    census_ = HeapCensus(_FakeDb(), spike_ratio=cfg.heap_census.spike_ratio)
    census_._last_items = 100_000
    _run_tick(census_, total_items=150_000, on_probe=calls.append)
    assert calls == [], "config 의 2.5 가 아니라 코드 기본값 1.3 으로 판정하고 있다"


class _FakeDb:
    def insert_many(self, *a, **k):
        pass


def _run_tick(component, total_items, on_probe):
    """`tick()` 을 돌리되 힙 실측 대신 주어진 원소 수를 먹인다.

    실제 힙을 부풀려 문턱을 넘기려 하면 그 숫자를 만드느라 테스트가 느리고 불안정해진다.
    여기서 재려는 것은 **분기 판정**이지 힙 스캔이 아니다.
    """
    import argus.runtime.heapcensus as mod

    orig_census, orig_probe = mod.census, mod.probe_largest
    mod.census = lambda top_n: (1000, total_items, 1.0, {})
    mod.probe_largest = lambda top_k: (on_probe(top_k), [])[1]
    try:
        component.tick()
    finally:
        mod.census, mod.probe_largest = orig_census, orig_probe

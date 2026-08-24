"""파이썬 힙에 무엇이 쌓이는지 센다.

**왜 있나.** 두 기계 실측에서 `private_mb` 가 프로세스 수명 내내 단조 증가했다
(노트북 부팅 후 117시간에 40 -> 198MB · 이 PC 세션마다 52 -> 250MB). 같은 구간에서
`threads` 와 `handles` 는 소수점까지 상수였다 — **스레드도 핸들도 아니고 파이썬 힙
안에서 자란다.** OS 지표로는 여기까지가 끝이라 `gc` 가 보는 객체를 직접 센다.

**관측자는 가벼워야 한다(설계 규칙 1).** 그래서 셋을 지켰다.

- 주기를 길게 잡는다(기본 300초). 이 계측은 분 단위로 변하는 것을 쫓지 않는다.
- 상위 N종만 남긴다. 전체 타입을 담으면 행이 커지는데 정작 보는 것은 상위 몇 개다.
- **자기 비용을 자기가 기록한다**(`scan_ms`). 이것이 없으면 나중에 "Argus 가
  무거워진 게 이 계측 때문인가"에 답할 수 없다.

**개수만 세면 절반을 놓친다 — 스모크가 이걸 잡았다(2026-08-25).** 빈 `dict` 5만 개를
만들고 다시 셌더니 전체가 **오히려 줄었다.** CPython 은 원자값만 든 컨테이너를
untrack 하고, `str`·`bytes`·`int` 는 애초에 `gc` 가 추적하지 않는다. 즉 캐시에
문자열 100만 개가 쌓여도 `dict` 하나가 늘 뿐이라 개수로는 아무것도 안 보인다.
그래서 **컨테이너의 원소 수(`len`)를 함께 센다.**

**원소 수가 무엇을 세는지 정확히 해 둔다.** untracked 객체 자신을 세는 것이 아니다 —
그것들은 `gc.get_objects()` 에 나오지 않으므로 `len()` 을 물을 대상조차 없다. 세는
것은 **그것들을 담고 있는 tracked 컨테이너가 커졌다**는 사실이다. 실제로 자라는
자료구조는 거의 항상 무언가에 담겨 있으므로 이걸로 방향은 잡히지만, 최상위 컨테이너
자신이 untracked 인 경우는 못 본다. 거기까지 가면 `tracemalloc` 진단 모드가 필요하다.

`tracemalloc` 은 쓰지 않는다. 상시 켜면 할당마다 후킹이 걸려 규칙 1 과 정면 충돌한다.
개수와 원소 수로도 정체가 안 잡히면 그때 진단 모드로 따로 도입한다.
"""

from __future__ import annotations

import gc
import json
import time
from collections import Counter

from ..logging_setup import get_logger
from ..storage.hot import Database
from .supervisor import Component

log = get_logger(__name__)

_COLUMNS = ("ts", "total_objects", "container_items", "scan_ms", "top_types")

# `len()` 을 물어도 안전하고 싼 것만. 임의 객체에 `len()` 을 부르면 사용자 코드가
# 돌 수 있고, 계측이 남의 `__len__` 때문에 죽으면 안 된다.
_SIZED = (dict, list, set, frozenset, tuple, bytearray)


def census(top_n: int) -> tuple[int, int, float, dict[str, dict[str, int]]]:
    """(전체 객체 수, 컨테이너 원소 총합, 소요 ms, 타입별 {n, items}).

    담기는 타입은 **원소 수 상위 N 과 개수 상위 N 의 합집합**이라 최대 2N 종이다.
    한쪽 기준만 쓰면 시계열 비교가 어긋나는 이유는 아래 주석에 있다.

    `gc.get_objects()` 는 스냅샷을 리스트로 만들어 돌려주므로 그 자체가 큰 리스트다.
    지역 변수로만 잡고 바로 버려 세대에 남기지 않는다.

    **원소 수를 함께 세는 이유는 모듈 독스트링에 있다.** 개수만으로는 `str`·`bytes`
    같은 비추적 객체가 통째로 안 보인다.
    """
    t0 = time.perf_counter()
    objects = gc.get_objects()
    total = len(objects)
    counts: Counter[str] = Counter()
    items: Counter[str] = Counter()
    total_items = 0
    for o in objects:
        name = type(o).__name__
        counts[name] += 1
        if isinstance(o, _SIZED):
            try:
                n = len(o)
            except Exception:  # noqa: BLE001 - 계측이 남의 __len__ 때문에 죽으면 안 된다
                continue
            items[name] += n
            total_items += n
    del objects  # 다음 줄로 넘어가기 전에 놓는다 — 이 리스트가 곧 수십 MB 다

    # **두 기준의 합집합을 담는다.**
    #
    # 원소 수 기준만 쓰면 원소가 0인 타입(`function`·`type` 등)이 표본마다 상위에
    # 들락날락해서 **시계열 비교가 어긋난다.** 실제로 첫 30분 데이터에서 `type` 이
    # "0 -> 1,252" 로 보였는데, 늘어난 것이 아니라 첫 표본의 상위 목록에 없었을
    # 뿐이었다(2026-08-25 실측). 그 상태로는 클래스가 실제로 누적되는 경우와
    # 구분할 방법이 없다.
    #
    # 개수 기준만 쓰면 반대 문제가 생긴다 — 늘 `function`·`wrapper_descriptor` 로
    # 고정돼 무엇이 자라는지 말해 주지 않는다. 그래서 둘 다 담는다.
    # 원소 기준을 앞에 두어 JSON 을 눈으로 읽을 때 자라는 쪽이 먼저 오게 한다.
    by_items = sorted(counts, key=lambda k: (items[k], counts[k]), reverse=True)[:top_n]
    by_count = sorted(counts, key=lambda k: (counts[k], items[k]), reverse=True)[:top_n]
    ranked = list(dict.fromkeys(by_items + by_count))
    top = {k: {"n": counts[k], "items": items[k]} for k in ranked}
    return total, total_items, (time.perf_counter() - t0) * 1000.0, top


class HeapCensus(Component):
    """주기적으로 힙 센서스를 남긴다."""

    name = "heap_census"
    # 스로틀을 받지 않는다. 스로틀이 걸린 순간이 오히려 "무엇이 자랐나"를 가장 알고
    # 싶은 순간이고, 주기가 이미 300초라 x10 이면 50분이 되어 곡선이 끊긴다.
    # 대신 주기 자체를 길게 잡아 비용을 낸다 — `self_telemetry` 와 같은 논리다.
    throttleable = False

    def __init__(self, db: Database, interval_s: float = 300.0, top_n: int = 20) -> None:
        self.db = db
        self.interval_s = interval_s
        self.top_n = top_n

    def tick(self) -> None:
        total, total_items, scan_ms, top = census(self.top_n)
        self.db.insert_many(
            "heap_census",
            _COLUMNS,
            [
                (
                    time.time(),
                    total,
                    total_items,
                    round(scan_ms, 3),
                    json.dumps(top, ensure_ascii=False),
                )
            ],
        )


if __name__ == "__main__":  # 스모크: python -m argus.runtime.heapcensus
    total, total_items, ms, top = census(top_n=8)
    print(f"  객체 {total:,}개 · 원소 {total_items:,}개 · 스캔 {ms:.2f}ms")
    for name, d in top.items():
        print(f"    {name:<20} {d['n']:>7,}개  원소 {d['items']:>9,}")

    # **센서스가 실제로 변화를 보는가.** 안 보면 아무것도 재지 않는 계측이다.
    #
    # 두 종류를 나눠 넣는다. 이 대조가 곧 "왜 원소 수를 세는가"의 답이다.
    #  (1) 값이 컨테이너인 dict -> gc 가 추적한다. 개수·원소 둘 다 잡혀야 한다
    #  (2) 값이 문자열뿐인 dict -> CPython 이 untrack 한다. **개수로는 안 보인다**
    #      원소 수만이 이것을 잡는다. 실제 캐시가 대개 (2) 의 모양이다.
    tracked = [{"k": []} for _ in range(50_000)]
    t_obj, t_items, _, _ = census(top_n=8)
    print(f"  (1) 추적되는 dict 5만  -> 객체 {t_obj - total:+,} · 원소 {t_items - total_items:+,}")

    untracked = [{"k": "v" * 8} for _ in range(50_000)]
    u_obj, u_items, _, _ = census(top_n=8)
    print(f"  (2) untrack 되는 dict 5만 -> 객체 {u_obj - t_obj:+,} · 원소 {u_items - t_items:+,}")
    del tracked, untracked

    if t_obj - total < 40_000:
        print("[FAIL] 추적되는 dict 5만개가 개수에 안 잡혔다")
        raise SystemExit(1)
    if u_items - t_items < 40_000:
        print("[FAIL] untrack 된 dict 의 원소가 안 잡혔다 — 원소 수를 세는 의미가 없어진다")
        raise SystemExit(1)
    if u_obj - t_obj > 10_000:
        print("[FAIL] untrack 될 것이 개수에 잡혔다 — 전제가 바뀌었으니 이 계측을 다시 본다")
        raise SystemExit(1)
    if ms > 500:
        print(f"[FAIL] 스캔이 {ms:.0f}ms — 관측자가 무겁다")
        raise SystemExit(1)
    print("[OK] runtime.heapcensus")

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
import os
import sys
import threading
import time
from collections import Counter
from types import FrameType, ModuleType

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


def _frame_holders(target: object) -> list[str]:
    """`target` 을 **지역 변수로** 들고 있는 스레드·함수.

    **`gc.get_referrers()` 로는 이걸 못 본다 — 2026-08-26 실측.** CPython 3.12
    에서 함수 지역 변수를 담은 리스트의 참조자를 물으면 **0건**이 돌아온다(프레임의
    지역 슬롯이 gc 순회 대상이 아니다). 그런데 한 틱만에 나타났다 사라지는
    스파이크는 바로 그 모양 — 어떤 스레드가 지금 무언가를 모으는 중 — 일 가능성이
    가장 높다. 참조자 경로만 두면 정작 찾으려던 것을 영영 못 본다.

    그래서 살아 있는 프레임을 직접 훑는다. **값 자체는 남기지 않는다**(설계 규칙 5).
    """
    names = {t.ident: t.name for t in threading.enumerate()}
    me = threading.get_ident()
    out: list[str] = []
    for tid, frame in sys._current_frames().items():
        if tid == me:  # 프로브 자신의 스택은 볼 필요가 없다
            continue
        f: object = frame
        depth = 0
        while isinstance(f, FrameType) and depth < 30:
            try:
                found = any(v is target for v in f.f_locals.values())
            except Exception:  # noqa: BLE001 - 계측이 남의 프레임 때문에 죽으면 안 된다
                found = False
            if found:
                code = f.f_code
                thread = names.get(tid, str(tid))
                out.append(f"[{thread}] {code.co_name}() {os.path.basename(code.co_filename)}:{f.f_lineno}")
                break
            f = f.f_back
            depth += 1
    return out


def _describe_holder(r: object) -> str:
    """참조자 하나를 사람이 읽는 한 줄로. **내용은 남기지 않는다.**

    `dict` 에서 한 단계 더 올라가는 이유: 전역이나 인스턴스 속성에 담긴 컨테이너는
    참조자가 `dict`(모듈 `__dict__`·인스턴스 `__dict__`) 로만 나와 **어느 dict 인지
    말해 주지 않는다.** 그 dict 를 누가 들고 있는지까지 봐야 클래스명이나 모듈명이
    나오고, 그래야 코드에서 찾을 수 있다.
    """
    if isinstance(r, FrameType):
        code = r.f_code
        return f"{code.co_name}() {os.path.basename(code.co_filename)}:{r.f_lineno}"
    if isinstance(r, dict):
        for owner in gc.get_referrers(r):
            if isinstance(owner, ModuleType):
                return f"dict of module {owner.__name__}"
            if hasattr(owner, "__class__") and getattr(owner, "__dict__", None) is r:
                return f"dict of {type(owner).__name__}"
        return "dict (소유자 불명)"
    return type(r).__name__


def probe_largest(top_k: int) -> list[dict[str, object]]:
    """가장 큰 컨테이너 `top_k` 개가 **무엇이고 누가 들고 있는지**.

    **스파이크가 났을 때만 부른다.** `gc.get_referrers()` 는 전체 힙을 훑으므로
    상시로 돌릴 것이 아니다(설계 규칙 1). 평상시 비용은 0 이다.

    **`repr` 을 남기지 않는다.** 컨테이너 안에는 프로세스명·경로·네트워크 목적지가
    들어 있을 수 있다(설계 규칙 5). 남기는 것은 타입·크기·원소 타입 분포와,
    참조자가 프레임일 때의 **함수명·파일·줄**뿐이다 — 정체를 말하는 데 그것으로 충분하고
    내용은 필요 없다.
    """
    objects = gc.get_objects()
    sized = [o for o in objects if isinstance(o, _SIZED)]
    try:
        biggest = sorted(sized, key=len, reverse=True)[:top_k]
    except Exception:  # noqa: BLE001 - 계측이 남의 __len__ 때문에 죽으면 안 된다
        return []
    del objects, sized

    out: list[dict[str, object]] = []
    for o in biggest:
        elem = Counter(type(x).__name__ for x in list(o)[:200]) if isinstance(o, (list, tuple)) else Counter()
        # 프레임 지역 변수를 먼저 본다 — 참조자로는 안 보이는 자리다(위 주석).
        holders: list[str] = _frame_holders(o)
        for r in gc.get_referrers(o):
            # 프로브 자신이 만든 목록은 건너뛴다. 안 거르면 `biggest`·`out` 이
            # 참조자 자리를 먼저 차지해 정작 진짜 보유자가 잘려 나간다(스모크가 잡았다).
            if r is biggest or r is out:
                continue
            holders.append(_describe_holder(r))
            if len(holders) >= 4:
                break
        out.append(
            {
                "type": type(o).__name__,
                "len": len(o),
                "elems": dict(elem.most_common(4)),
                "holders": holders,
            }
        )
    return out


class HeapCensus(Component):
    """주기적으로 힙 센서스를 남긴다."""

    name = "heap_census"
    # 스로틀을 받지 않는다. 스로틀이 걸린 순간이 오히려 "무엇이 자랐나"를 가장 알고
    # 싶은 순간이고, 주기가 이미 300초라 x10 이면 50분이 되어 곡선이 끊긴다.
    # 대신 주기 자체를 길게 잡아 비용을 낸다 — `self_telemetry` 와 같은 논리다.
    throttleable = False

    def __init__(
        self,
        db: Database,
        interval_s: float = 300.0,
        top_n: int = 20,
        spike_ratio: float = 1.3,
        spike_top_k: int = 3,
    ) -> None:
        self.db = db
        self.interval_s = interval_s
        self.top_n = top_n
        self.spike_ratio = spike_ratio
        self.spike_top_k = spike_top_k
        # 직전 틱의 원소 총합. 첫 틱에는 비교 대상이 없으므로 스파이크로 보지 않는다.
        self._last_items: int | None = None

    def tick(self) -> None:
        total, total_items, scan_ms, top = census(self.top_n)

        # **튄 틱에서만 정체를 캔다.** 2026-08-26 19:45 에 `container_items` 가
        # 98,604(중앙값) -> 147,308 로 한 틱 튀었다가 다음 틱에 사라졌다. `list` 가
        # 9개 늘고 원소가 +52,355 인데 `tuple` 은 오히려 줄어, 원소가 튜플이 아닌
        # 큰 리스트 몇 개라는 것까지는 읽혔다. 거기서 멈췄다 — **표본이 1건이고
        # 어느 리스트인지 남은 것이 없었다.** 롤업의 float 시계열을 의심했으나
        # census 291건 중 282건이 프로세스 롤업 완료 2초 이내인데 전부 평범해
        # 기각됐다. 그래서 추측을 더 쌓는 대신 다음 스파이크가 스스로 말하게 한다.
        #
        # 평상시 비용은 0 이다. `probe_largest` 는 이 분기 안에서만 돈다.
        if self._last_items and total_items > self._last_items * self.spike_ratio:
            log.warning(
                "힙 원소가 튀었다",
                extra={
                    "items": total_items,
                    "prev_items": self._last_items,
                    "ratio": round(total_items / self._last_items, 2),
                    "total_objects": total,
                    "largest": probe_largest(self.spike_top_k),
                },
            )
        self._last_items = total_items

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

    # **스파이크 프로브가 실제로 범인을 지목하는가.** 이 스모크가 없으면 정작
    # 스파이크가 났을 때 빈 목록만 남고 다시 아무것도 모르게 된다.
    # 실제로 자라는 것은 **지역 변수**(스레드가 도는 중)이거나 **인스턴스 속성**
    # (캐시가 쌓이는 중)이다. 둘 다 지목되는지 나눠 본다.
    import threading as _th

    result: dict[str, object] = {}
    go, done = _th.Event(), _th.Event()

    def _worker() -> None:
        haystack = list(range(120_000))  # noqa: F841 - 프로브가 찾아야 할 대상
        go.set()
        done.wait(10)
        del haystack

    def _local_holder() -> list[dict[str, object]]:
        t = _th.Thread(target=_worker, name="probe-victim")
        t.start()
        go.wait(10)
        try:
            return probe_largest(top_k=3)
        finally:
            done.set()
            t.join(10)

    found = _local_holder()
    if not found or found[0]["len"] < 100_000:
        print(f"[FAIL] 12만 원소 리스트를 못 찾았다 -> {found}")
        raise SystemExit(1)
    holders = found[0]["holders"]
    print(f"  프로브(다른 스레드 지역변수): {found[0]['type']} {found[0]['len']:,}원소  보유자 {holders}")
    if not any("_worker()" in h and "probe-victim" in h for h in holders):
        print(f"[FAIL] 다른 스레드의 지역 변수가 안 잡혔다 — 한 틱짜리 스파이크를 영영 못 본다 -> {holders}")
        raise SystemExit(1)

    class _Cache:
        def __init__(self) -> None:
            self.rows = list(range(130_000))

    cache = _Cache()
    found2 = probe_largest(top_k=3)
    holders2 = found2[0]["holders"] if found2 else []
    print(f"  프로브(인스턴스 속성): {found2[0]['type']} {found2[0]['len']:,}원소  보유자 {holders2}")
    if not any("_Cache" in h for h in holders2):
        print(f"[FAIL] 보유 인스턴스가 안 잡혔다 — dict 에서 한 단계 더 올라가지 못했다 -> {holders2}")
        raise SystemExit(1)
    del cache
    print("[OK] runtime.heapcensus")

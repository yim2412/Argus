"""DB 전역 락의 보유자를 재는 진단 모드 (기본 꺼짐).

**왜 필요했나.** `db._lock` 은 읽기·쓰기를 함께 덮는 전역 락이고, 2026-08-29 16:13:34
에 writer 의 단일 행 쓰기가 **115초**였다. 2026-09-10 에 롤업을 용의선상에서 지웠다 —
타이머 12회에서 `lock_held_ms` 최대 41ms 였고, 롤업이 오히려 락 대기 3.8초를 겪고
있었다. 즉 **락을 쥐고 있는 다른 쪽이 있다.** 이 모듈은 그쪽을 지목하려고 붙였다.

**두 질문을 갈라서 잰다.** 08-31 의 기준은 `insert_ms`·`commit_ms` 하나로 "우리가
가해자인가"를 물었는데, 그 한 지표로는 답할 수 없는 질문이 섞여 있었다.

  1. **한 번에 오래 쥐는가** — 보유자별 `max_ms`·`p95_ms`
  2. **자주 잡는가** — 보유자별 `count`·총 보유시간
  3. **(그 순간) 누가 나를 막았나** — 대기가 길었을 때 그때의 보유자를 남긴다

3번이 없으면 집계는 "누가 락을 많이 쓰나"까지만 말하고 115초 그 순간에는 답하지
못한다. 집계는 용의자를 줄이고, 3번이 현행범을 잡는다.

**설계 규칙 1(관측자는 가벼워야 한다) 을 지키는 방법 셋.**
  - 꺼져 있으면 `Database` 는 순수 `threading.RLock` 을 들고 돈다 — 오버헤드 0.
  - 켜져도 **DB 에 쓰지 않는다.** 진단이 DB 를 쓰면 그 쓰기가 락을 다시 잡아
    자기참조가 된다. 메모리 집계 + 주기 로그뿐이다.
  - 집계는 **락을 놓은 뒤에** 한다. 보유 구간 안에서 집계하면 재는 행위가 재려는
    값을 키운다.

**보유자 이름은 호출부에서 자동으로 얻는다**(`sys._getframe`). 40여 군데의
`with db._lock:` 에 손으로 이름을 붙이지 않는 이유는 하나다 — 붙이면 새 호출부가
생길 때 **조용히 누락**되고, 그 누락이 곧 "용의자에 없다"로 읽힌다.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

# 보유자 하나당 보관하는 최근 표본 수. p95 를 내기 위한 것이고, 보유자가 40여
# 종이므로 40 x 512 x 8B 정도로 상한이 잡힌다.
_MAX_SAMPLES = 512

# 느린 사건 보관 개수. 로그로도 나가지만 스냅샷에서 바로 보려고 들고 있는다.
_MAX_SLOW_EVENTS = 200

# 느린 사건 로그의 최소 간격(초). 경합이 폭주할 때 로그 자체가 부하가 되면
# 관측이 관측 대상을 바꾼다.
_SLOW_LOG_MIN_GAP_S = 1.0

# 주기 보고에 싣는 보유자 수.
_REPORT_TOP = 8

# 공통 통로가 있는 파일. 여기서 락을 잡았으면 부른 쪽을 한 칸 더 올라가 붙인다.
_HOT_FILE = "hot.py"


def _caller() -> str:
    """`with db._lock:` 을 쓴 호출부를 `모듈.함수:줄` 로.

    건너뛸 프레임을 **파일 이름이 아니라 코드 객체로** 고른다. 파일로 거르면 이
    모듈 안에서 락을 쓰는 코드(스모크가 그렇다)의 호출부까지 통째로 건너뛰어
    `threading.run` 같은 엉뚱한 이름이 나온다 — 이름이 틀리면 용의자 표가 통째로
    무의미해지므로 넓게 거르지 않는다.
    """
    frame: Any = sys._getframe(1)
    while frame is not None and frame.f_code in _INTERNAL_CODE:
        frame = frame.f_back
    if frame is None:
        return "?"
    name = _frame_name(frame)
    if Path(frame.f_code.co_filename).name == _HOT_FILE and frame.f_back is not None:
        # `Database.query`·`insert_many` 는 **모든 조회·쓰기의 공통 통로**다. 여기서
        # 멈추면 209회가 `hot.query` 한 줄로 뭉쳐 "누가 399ms 를 쥐었나"에 답할 수
        # 없다(2026-09-10 첫 집계에서 실제로 그랬다). 한 칸 더 올라가 부른 쪽을 붙인다.
        name = f"{_frame_name(frame.f_back)} → {name}"
    return name


def _frame_name(frame: Any) -> str:
    return f"{Path(frame.f_code.co_filename).stem}.{frame.f_code.co_name}:{frame.f_lineno}"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


@dataclass
class HolderStat:
    """보유자 한 명의 누적."""

    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    wait_total_ms: float = 0.0
    wait_max_ms: float = 0.0
    samples: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))


class TracedRLock:
    """`threading.RLock` 과 같은 자리에 끼우는 계측 래퍼.

    `RLock` 이므로 **재진입은 세지 않는다.** 같은 스레드가 중첩해서 잡은 것을 각각
    세면 한 번의 보유가 여러 번으로 부풀고, 안쪽 보유시간이 바깥 것에 중복으로 더해진다.
    """

    def __init__(
        self,
        *,
        slow_wait_ms: float = 200.0,
        slow_held_ms: float = 1000.0,
        report_interval_s: float = 300.0,
    ) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        # 집계 전용 락. **DB 락 밖에서만** 잡고 짧게만 쥔다.
        self._stats_lock = threading.Lock()

        self.slow_wait_ms = slow_wait_ms
        self.slow_held_ms = slow_held_ms
        self.report_interval_s = report_interval_s

        self._holders: dict[str, HolderStat] = {}
        self._slow: deque = deque(maxlen=_MAX_SLOW_EVENTS)
        self._acquires = 0
        self._contended = 0
        # 지금 쥐고 있는 쪽. 대기를 시작하는 쪽이 "누가 나를 막고 있나"를 읽는 자리다.
        self._current: tuple[str, float] | None = None
        # 직전에 놓은 쪽. 내가 깨어난 직후에 보면 "방금까지 막고 있던 쪽"이 된다.
        self._last_released: tuple[str, float] | None = None
        self._started = time.time()
        self._last_report = time.monotonic()
        self._last_slow_log = 0.0

    # ------------------------------------------------------------------ 락 규약

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        depth = getattr(self._local, "depth", 0)
        if depth:
            acquired = self._lock.acquire(blocking, timeout)
            if acquired:
                self._local.depth = depth + 1
            return acquired

        name = _caller()
        blocker = self._current
        t0 = time.perf_counter()
        acquired = self._lock.acquire(blocking, timeout)
        if not acquired:
            return False
        t1 = time.perf_counter()

        self._local.depth = 1
        self._local.name = name
        self._local.acquired_at = t1
        self._current = (name, t1)

        wait_ms = (t1 - t0) * 1000.0
        self._local.wait_ms = wait_ms
        if wait_ms >= self.slow_wait_ms:
            # **깬 직후에 읽는다.** 기다리기 시작한 순간의 보유자(`blocker`)와 내가
            # 깨기 직전에 놓은 쪽(`_last_released`)은 다를 수 있다 — 그 사이에 여러
            # 명이 지나갔다면 후자가 마지막 주자다. 둘 다 남긴다.
            self._note_slow_wait(name, wait_ms, blocker, self._last_released)
        return True

    def release(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth > 1:
            self._local.depth = depth - 1
            self._lock.release()
            return

        name = getattr(self._local, "name", "?")
        held_ms = (time.perf_counter() - getattr(self._local, "acquired_at", 0.0)) * 1000.0
        wait_ms = getattr(self._local, "wait_ms", 0.0)
        self._local.depth = 0

        # 다음 대기자가 낡은 상태를 읽지 않도록 **놓기 전에** 갱신한다.
        self._current = None
        self._last_released = (name, held_ms)
        self._lock.release()

        # 여기부터는 DB 락 밖이다. 집계 비용이 보유시간에 섞이지 않는다.
        self._record(name, held_ms, wait_ms)

    def __enter__(self) -> "TracedRLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # ------------------------------------------------------------------ 집계

    def _record(self, name: str, held_ms: float, wait_ms: float) -> None:
        with self._stats_lock:
            stat = self._holders.get(name)
            if stat is None:
                stat = self._holders[name] = HolderStat()
            stat.count += 1
            stat.total_ms += held_ms
            stat.max_ms = max(stat.max_ms, held_ms)
            stat.wait_total_ms += wait_ms
            stat.wait_max_ms = max(stat.wait_max_ms, wait_ms)
            stat.samples.append(held_ms)
            self._acquires += 1
            if wait_ms >= self.slow_wait_ms:
                self._contended += 1
            due = (time.monotonic() - self._last_report) >= self.report_interval_s
            if due:
                self._last_report = time.monotonic()

        if held_ms >= self.slow_held_ms:
            # 여기까지 오면 **이 보유자가 가해자**다. 이 한 줄이 찾던 답이다.
            log.warning(
                "DB 락을 오래 쥐었다",
                extra={"holder": name, "held_ms": round(held_ms, 1)},
            )
        if due:
            self.report()

    def _note_slow_wait(
        self,
        waiter: str,
        wait_ms: float,
        blocker: tuple[str, float] | None,
        last_released: tuple[str, float] | None,
    ) -> None:
        event = {
            "waiter": waiter,
            "wait_ms": round(wait_ms, 1),
            "blocked_by": blocker[0] if blocker else None,
            "last_holder": last_released[0] if last_released else None,
            "last_held_ms": round(last_released[1], 1) if last_released else None,
            "ts": round(time.time(), 3),
        }
        with self._stats_lock:
            self._slow.append(event)
            now = time.monotonic()
            emit = (now - self._last_slow_log) >= _SLOW_LOG_MIN_GAP_S
            if emit:
                self._last_slow_log = now
        if emit:
            log.warning("DB 락을 오래 기다렸다", extra=event)

    # ------------------------------------------------------------------ 조회

    def snapshot(self) -> dict[str, Any]:
        with self._stats_lock:
            holders = []
            for name, stat in self._holders.items():
                samples = list(stat.samples)
                holders.append(
                    {
                        "holder": name,
                        "count": stat.count,
                        "total_ms": round(stat.total_ms, 1),
                        "max_ms": round(stat.max_ms, 1),
                        "p95_ms": round(_percentile(samples, 0.95), 1),
                        "wait_total_ms": round(stat.wait_total_ms, 1),
                        "wait_max_ms": round(stat.wait_max_ms, 1),
                    }
                )
            slow = list(self._slow)
            acquires = self._acquires
            contended = self._contended
            started = self._started
        holders.sort(key=lambda h: h["total_ms"], reverse=True)
        return {
            "elapsed_s": round(time.time() - started, 1),
            "acquires": acquires,
            "contended": contended,
            "holders": holders,
            "slow": slow,
        }

    def report(self) -> dict[str, Any]:
        """주기 보고 한 줄. DEBUG 는 배포판에서 꺼지므로 INFO 로 남긴다."""
        snap = self.snapshot()
        log.info(
            "DB 락 보유자",
            extra={
                "elapsed_s": snap["elapsed_s"],
                "acquires": snap["acquires"],
                "contended": snap["contended"],
                "top": snap["holders"][:_REPORT_TOP],
            },
        )
        return snap


# 호출부를 찾을 때 건너뛸 우리 자신의 프레임. `_caller` 가 파일이 아니라 이 집합으로
# 거른다(위 주석 참조). 락 진입 경로가 늘면 여기에 같이 넣는다.
_INTERNAL_CODE = frozenset(
    {
        _caller.__code__,
        TracedRLock.acquire.__code__,
        TracedRLock.__enter__.__code__,
    }
)


# ---------------------------------------------------------------------- 배선

_SETTINGS_CACHE: Any = None


def _settings() -> Any:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        from ..config.loader import load_settings

        _SETTINGS_CACHE = load_settings().storage
    return _SETTINGS_CACHE


def reset_settings_cache() -> None:
    """테스트가 설정을 바꾼 뒤 다시 읽게 한다."""
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def make_lock() -> Any:
    """진단이 켜져 있으면 계측 락을, 아니면 순수 `RLock` 을.

    설정을 못 읽어도 앱이 죽지 않는다 — 진단은 부가 기능이고, 이것 때문에
    저장소가 열리지 않으면 본말이 뒤집힌다.
    """
    try:
        cfg = _settings()
        if not getattr(cfg, "lock_trace", False):
            return threading.RLock()
        return TracedRLock(
            slow_wait_ms=cfg.lock_trace_slow_wait_ms,
            slow_held_ms=cfg.lock_trace_slow_held_ms,
            report_interval_s=cfg.lock_trace_report_s,
        )
    except Exception:  # noqa: BLE001 - 진단 실패가 저장소를 막지 않는다
        log.debug("락 진단 설정을 읽지 못해 끈다", exc_info=True)
        return threading.RLock()


if __name__ == "__main__":  # 스모크: python -m argus.storage.locktrace
    from ..logging_setup import setup

    setup()
    lock = TracedRLock(slow_wait_ms=10.0, slow_held_ms=10_000.0, report_interval_s=10_000.0)
    ok = True

    with lock:
        with lock:  # 재진입
            time.sleep(0.05)
    first = lock.snapshot()["holders"][0]
    if first["count"] != 1:
        print(f"[FAIL] 재진입을 {first['count']}회로 셌다 (1회여야 한다)")
        ok = False
    if first["max_ms"] < 45:
        print(f"[FAIL] 보유시간 {first['max_ms']}ms — 50ms 를 못 쟀다")
        ok = False

    def _hold() -> None:
        with lock:
            time.sleep(0.2)

    thread = threading.Thread(target=_hold)
    thread.start()
    time.sleep(0.05)
    with lock:
        pass
    thread.join()

    snap = lock.snapshot()
    if not snap["slow"]:
        print("[FAIL] 경합했는데 느린 대기가 기록되지 않았다")
        ok = False
    else:
        event = snap["slow"][-1]
        print(f"  대기 {event['wait_ms']}ms · 막은 쪽 {event['blocked_by']}")
        if event["blocked_by"] is None:
            print("[FAIL] 막은 쪽을 지목하지 못했다")
            ok = False

    for row in snap["holders"]:
        print(f"  {row['holder']:<44} {row['count']:>4}회  max {row['max_ms']:>8.1f}ms")
    print("[OK] storage.locktrace" if ok else "[FAIL] storage.locktrace")
    raise SystemExit(0 if ok else 1)

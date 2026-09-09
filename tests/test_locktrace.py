"""DB 락 진단이 **무엇을 재고 있는지**를 고정한다.

이 테스트가 지키는 것은 넷이다. 넷 다 "조용히 틀리는" 자리다 — 예외가 아니라
값만 어긋나므로, 깨져도 앱은 멀쩡히 돌고 용의자 표만 거짓이 된다.

  1. **재진입을 중복으로 세지 않는다** — 세면 한 번의 보유가 여러 번이 되고
     보유시간이 중복으로 더해져, 재진입이 잦은 쪽이 자동으로 1위가 된다.
  2. **보유자 이름이 실제 호출부다** — 이름이 틀리면 표 전체가 무의미하다.
  3. **막은 쪽을 지목한다** — 집계만으로는 "그 순간 누가 쥐었나"에 답할 수 없다.
  4. **설정이 실제로 실린다** — 기본값이 아닌 값으로 잰다. `assert lock.slow_wait_ms
     == 200` 같은 단언은 코드 기본값과 YAML 기본값이 같으면 **배선이 끊겨도 참이다.**
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from argus.config.loader import StorageSettings
from argus.storage import locktrace
from argus.storage.hot import Database
from argus.storage.locktrace import TracedRLock


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    locktrace.reset_settings_cache()
    yield
    locktrace.reset_settings_cache()


def _lock(**kwargs) -> TracedRLock:
    kwargs.setdefault("slow_wait_ms", 30.0)
    kwargs.setdefault("slow_held_ms", 10_000.0)
    kwargs.setdefault("report_interval_s", 10_000.0)
    return TracedRLock(**kwargs)


def test_재진입은_한_번의_보유로_센다():
    lock = _lock()
    with lock:
        with lock:
            with lock:
                time.sleep(0.02)

    holders = lock.snapshot()["holders"]
    assert len(holders) == 1, f"재진입이 여러 보유자로 갈렸다: {holders}"
    assert holders[0]["count"] == 1
    # 안쪽 보유가 바깥에 더해졌다면 20ms 를 훌쩍 넘는다.
    assert holders[0]["max_ms"] < 60


def test_보유시간을_실제로_잰다():
    lock = _lock()
    with lock:
        time.sleep(0.08)
    holder = lock.snapshot()["holders"][0]
    assert holder["max_ms"] >= 70, f"80ms 를 쥐었는데 {holder['max_ms']}ms 로 쟀다"


def test_보유자_이름이_호출부다():
    lock = _lock()
    with lock:
        pass
    holder = lock.snapshot()["holders"][0]["holder"]
    module, _, line = holder.partition(":")
    assert module == f"{Path(__file__).stem}.test_보유자_이름이_호출부다", holder
    assert line.isdigit() and int(line) > 0, holder


def test_막은_쪽을_지목한다():
    lock = _lock(slow_wait_ms=20.0)
    started = threading.Event()

    def hold() -> None:
        with lock:
            started.set()
            time.sleep(0.15)

    thread = threading.Thread(target=hold)
    thread.start()
    started.wait(timeout=2.0)
    with lock:
        pass
    thread.join()

    slow = lock.snapshot()["slow"]
    assert slow, "150ms 를 기다렸는데 느린 대기가 기록되지 않았다"
    event = slow[-1]
    assert event["wait_ms"] >= 20
    assert "hold" in (event["blocked_by"] or ""), event
    # 깬 직후의 마지막 주자도 같은 쪽이어야 한다 — 경쟁자가 하나뿐이다.
    assert "hold" in (event["last_holder"] or ""), event


def test_빠른_보유는_느린_사건으로_기록하지_않는다():
    """문턱이 실제로 무언가를 가르는지 — 안 그러면 3번 테스트도 항상 통과한다."""
    lock = _lock(slow_wait_ms=500.0)
    started = threading.Event()

    def hold() -> None:
        with lock:
            started.set()
            time.sleep(0.05)

    thread = threading.Thread(target=hold)
    thread.start()
    started.wait(timeout=2.0)
    with lock:
        pass
    thread.join()

    assert lock.snapshot()["slow"] == []


def test_꺼져_있으면_계측_락이_끼워지지_않는다(tmp_path, monkeypatch):
    monkeypatch.setattr(locktrace, "_SETTINGS_CACHE", StorageSettings(lock_trace=False))
    db = Database(tmp_path / "off.db").open()
    try:
        assert not isinstance(db._lock, TracedRLock)  # noqa: SLF001
    finally:
        db.close()


def test_설정이_락에_실린다(tmp_path, monkeypatch):
    """**기본값이 아닌 값으로 잰다.** 기본값으로 재면 배선이 끊겨도 통과한다."""
    settings = StorageSettings(
        lock_trace=True,
        lock_trace_slow_wait_ms=37.5,
        lock_trace_slow_held_ms=123.5,
        lock_trace_report_s=11.5,
    )
    monkeypatch.setattr(locktrace, "_SETTINGS_CACHE", settings)
    db = Database(tmp_path / "on.db").open()
    try:
        lock = db._lock  # noqa: SLF001
        assert isinstance(lock, TracedRLock)
        assert lock.slow_wait_ms == 37.5
        assert lock.slow_held_ms == 123.5
        assert lock.report_interval_s == 11.5
    finally:
        db.close()


def test_켜면_실제_DB_작업이_보유자로_잡힌다(tmp_path, monkeypatch):
    """배선 테스트가 로직까지 보는지 — 락 객체만 바뀌고 계측이 안 돌 수 있다."""
    monkeypatch.setattr(locktrace, "_SETTINGS_CACHE", StorageSettings(lock_trace=True))
    db = Database(tmp_path / "live.db").open()
    try:
        db.set_meta("probe", "1")
        holders = db._lock.snapshot()["holders"]  # noqa: SLF001
    finally:
        db.close()

    names = [h["holder"] for h in holders]
    assert any("hot.set_meta" in n for n in names), names


def test_진단_설정을_못_읽어도_저장소는_열린다(monkeypatch):
    def boom() -> None:
        raise RuntimeError("설정 폭발")

    monkeypatch.setattr(locktrace, "_settings", boom)
    lock = locktrace.make_lock()
    assert isinstance(lock, type(threading.RLock()))

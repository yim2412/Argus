"""활성 집합 선택 — 핸들 누수 프로세스가 저장 대상에서 빠지지 않는지.

**2026-07-30 에 이것 때문에 주입 5건 중 3건이 미탐이었다.** 활성 집합은 CPU 상위 15 +
메모리 상위 10 + 포어그라운드였고, 나머지는 `full_store_interval_s`(30초)에 한 번만
저장됐다. 핸들 누수 프로세스는 CPU·메모리를 거의 쓰지 않아 어느 상위에도 못 든다 —
그래서 720초 구간에 표본이 29~37행뿐이었고, 첫 표본이 40~50초 늦게 잡혀 그 사이 이미
1,400~1,800 핸들이 늘었다. `procleak` 이 보는 `first` 가 부풀면 배수가 무너진다
(실측 2.25~2.89 < 문턱 3.0).

`procleak` 의 존재 이유가 핸들·RSS 누수인데 수집이 그 지표를 우선하지 않던 구조적 어긋남이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.collector.process import ProcessCollector  # noqa: E402
from argus.collector.procsource import ProcSample  # noqa: E402
from argus.storage.queue import SampleQueue  # noqa: E402


def _sample(pid: int, name: str, *, cpu=0.0, rss=50.0, handles=300) -> ProcSample:
    return ProcSample(
        pid=pid, name=name, cpu_percent=cpu, rss_mb=rss,
        io_read_bps=0.0, io_write_bps=0.0, handles=handles, threads=4,
    )


def _collector(**kwargs) -> ProcessCollector:
    return ProcessCollector(SampleQueue(maxsize=1000), **kwargs)


def _busy(count: int, *, start_pid: int = 100) -> dict[int, ProcSample]:
    """CPU·메모리 상위를 가득 채우는 프로세스들. 핸들은 늘지 않는다."""
    return {
        start_pid + i: _sample(start_pid + i, f"busy{i}", cpu=90.0 - i, rss=900.0 - i)
        for i in range(count)
    }


def test_handle_growth_puts_a_quiet_leaker_in_the_active_set():
    """CPU·메모리 상위를 다른 프로세스가 다 차지해도 누수는 잡혀야 한다."""
    collector = _collector(top_cpu=15, top_memory=10, top_handle_growth=10)

    first = _busy(40)
    first[9999] = _sample(9999, "leaker", cpu=0.1, rss=20.0, handles=400)
    collector._select_active(first, None)  # noqa: SLF001 — 직전 값을 기억시킨다

    second = _busy(40)
    second[9999] = _sample(9999, "leaker", cpu=0.1, rss=20.0, handles=900)
    active = collector._select_active(second, None)  # noqa: SLF001

    assert 9999 in active, (
        "핸들이 400 → 900 으로 늘었는데 활성 집합에 없다 — 30초 주기로만 저장된다"
    )


def test_quiet_leaker_is_missed_without_the_handle_axis():
    """축을 끄면 놓친다 — 이 테스트가 위 테스트의 근거다.

    끄고도 통과한다면 다른 이유로 잡히고 있다는 뜻이고, 그러면 위 테스트는 아무것도
    검증하지 않는다.
    """
    collector = _collector(top_cpu=15, top_memory=10, top_handle_growth=0)

    first = _busy(40)
    first[9999] = _sample(9999, "leaker", cpu=0.1, rss=20.0, handles=400)
    collector._select_active(first, None)  # noqa: SLF001

    second = _busy(40)
    second[9999] = _sample(9999, "leaker", cpu=0.1, rss=20.0, handles=900)
    active = collector._select_active(second, None)  # noqa: SLF001

    assert 9999 not in active


def test_steady_high_handle_holder_is_not_selected():
    """**보유량이 아니라 증가량으로 고른다.**

    상시 핸들이 많은 프로그램(브라우저 등)을 고르면 자리를 다 차지해, 400개에서 시작해
    새는 프로세스가 밀려난다. 늘어나는 것이 누수다.
    """
    collector = _collector(top_cpu=0, top_memory=0, top_handle_growth=1)

    holder = {7000: _sample(7000, "browser", handles=50_000)}
    holder[9999] = _sample(9999, "leaker", handles=400)
    collector._select_active(holder, None)  # noqa: SLF001

    nxt = {7000: _sample(7000, "browser", handles=50_000)}  # 그대로
    nxt[9999] = _sample(9999, "leaker", handles=600)        # 200 늘었다
    active = collector._select_active(nxt, None)  # noqa: SLF001

    assert 9999 in active, "증가한 쪽이 아니라 많이 가진 쪽을 골랐다"
    assert 7000 not in active


def test_time_gap_clears_the_growth_baseline():
    """절전 복귀 직후의 차분은 "지금 늘어나는 중"이 아니다.

    그대로 두면 수백 개가 증가량 상위로 잡혀 활성 집합이 통째로 뒤집힌다.
    """
    collector = _collector(top_handle_growth=10)
    snapshot = {1: _sample(1, "a", handles=100)}
    collector._select_active(snapshot, None)  # noqa: SLF001
    assert collector._prev_handles  # noqa: SLF001

    collector.on_time_gap(600.0)
    assert not collector._prev_handles, "공백 뒤에도 이전 핸들 값을 들고 있다"  # noqa: SLF001


def test_missing_handle_values_do_not_crash_selection():
    """핸들을 못 읽는 프로세스(권한 없음)가 있어도 선택이 죽지 않는다."""
    collector = _collector(top_handle_growth=5)
    first = {1: _sample(1, "a", handles=None), 2: _sample(2, "b", handles=100)}
    collector._select_active(first, None)  # noqa: SLF001
    second = {1: _sample(1, "a", handles=None), 2: _sample(2, "b", handles=500)}
    active = collector._select_active(second, None)  # noqa: SLF001
    assert 2 in active

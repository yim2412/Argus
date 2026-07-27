"""워킹셋 트림 상황에서 메모리 지표가 무엇을 말하는지 고정한다.

6시간 소크 중간 집계에서 RSS 가 63MB → 18MB 로 내려갔다. 메모리를 반납한 게 아니라
백그라운드 프로세스의 워킹셋을 Windows 가 트림한 것이었다. RSS 만 보면 "누수 없음"으로
읽히지만 실제로 쓰는 메모리는 그대로다 — **관측 지표가 틀리면 결론도 틀린다.**

여기서는 트림을 인위로 일으켜 `rss_mb` 는 무너지고 `private_mb` 는 버티는 것을 확인한다.
이게 깨지면 자기 계측의 누수 판정 근거가 사라진 것이므로 실패로 봐야 한다.
"""

from __future__ import annotations

import os

import pytest

from argus.config.loader import BudgetSettings
from argus.runtime.budget import BudgetGuard

pytestmark = pytest.mark.skipif(os.name != "nt", reason="워킹셋 트림은 Windows 개념이다")


def _empty_working_set() -> None:
    """현재 프로세스의 워킹셋을 강제로 비운다.

    `SetProcessWorkingSetSize` 에 (-1, -1) 을 주면 OS 가 트림해도 된다는 뜻이 된다.
    유휴 백그라운드 프로세스에서 Windows 가 알아서 하는 일과 같은 것을 즉시 일으킨다.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # argtypes 를 명시하지 않으면 64비트에서 SIZE_T(-1) 이 32비트로 잘려
    # 호출이 조용히 실패한다. 실패가 skip 으로 새면 검증이 없어진 걸 모른다.
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t]
    kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

    handle = kernel32.GetCurrentProcess()
    minus_one = ctypes.c_size_t(-1)
    if not kernel32.SetProcessWorkingSetSize(handle, minus_one, minus_one):
        err = ctypes.get_last_error()
        pytest.skip(f"SetProcessWorkingSetSize 실패(GetLastError={err}) — 트림을 만들 수 없다")


def test_private_survives_working_set_trim() -> None:
    guard = BudgetGuard(BudgetSettings())

    # 트림해도 사라지지 않을 만큼 실제로 건드린 메모리를 만든다.
    # bytearray 를 채우는 이유: 할당만 하면 페이지가 실제로 커밋되지 않을 수 있다.
    ballast = bytearray(64 * 1024 * 1024)
    for i in range(0, len(ballast), 4096):
        ballast[i] = 1

    before = guard._memory()
    assert before.private_mb is not None, "Windows 에서 private 이 없다면 psutil 쪽이 바뀐 것이다"
    assert before.peak_wset_mb is not None
    assert before.page_faults is not None

    _empty_working_set()
    after = guard._memory()

    # RSS 는 무너진다 — 이것이 소크에서 본 착시의 정체다.
    assert after.rss_mb < before.rss_mb * 0.5, (
        f"트림이 일어나지 않았다 (rss {before.rss_mb:.1f} → {after.rss_mb:.1f}MB). "
        "이 환경에서는 이 테스트가 검증하려는 상황 자체가 재현되지 않았다."
    )

    # private 은 버틴다 — 누수 판정은 여기에 걸어야 한다.
    assert after.private_mb is not None
    assert after.private_mb > before.private_mb * 0.9, (
        f"private 이 트림과 함께 줄었다 ({before.private_mb:.1f} → {after.private_mb:.1f}MB). "
        "누수 추세를 판정할 지표가 없다는 뜻이다."
    )

    # peak_wset 은 되돌아가지 않는다 — 트림된 구간에서도 실제 사용량이 남는다.
    assert after.peak_wset_mb is not None
    assert after.peak_wset_mb >= before.peak_wset_mb

    del ballast


def test_measure_keeps_rss_for_budget() -> None:
    """예산 판정은 계속 RSS 로 한다.

    예산 "RSS 300MB" 는 *지금 물리 메모리를 얼마나 붙들고 있는가* 이므로,
    트림된 뒤 줄어든 값이 맞다. private 로 바꾸면 예산의 의미가 달라진다.
    """
    guard = BudgetGuard(BudgetSettings())
    cpu, rss_mb = guard.measure()
    assert rss_mb == pytest.approx(guard.last_memory().rss_mb)
    assert guard.last() == (cpu, rss_mb)

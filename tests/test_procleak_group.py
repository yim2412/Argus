"""분산 누수를 잡는 그룹 축(`GROUP_RULES`) 검증.

**왜 생겼는가.** `procleak` 은 `(pid, name, metric)` 로 추적하므로 누수가 여러
프로세스에 흩어지면 각자는 문턱 아래가 된다. 2026-08-16 실주입에서 총 11.8GB 를
8개로 나눴더니 60분 내내 아무 신호도 나지 않았다(라벨 `#65`·`#66`). 같은 양을 한
프로세스로 넣으면 5~9분에 잡힌다 — **분산 하나만으로 무력화된다.**

여기 테스트는 그 구멍을 재는 것이라, **"막지 않았으면 무엇이 일어났을 것인가"를
먼저 단언한다**(`test_pid_axis_alone_misses_a_spread_leak`). 그게 없으면 그룹 축을
통째로 뜯어내도 나머지 테스트가 통과할 수 있다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection.base import Observation, ProcessView  # noqa: E402
from argus.detection.procleak import GROUP_RULES, ProcessLeakDetector  # noqa: E402

STEP = 30.0          # tier2 수집 간격
TICKS = 40           # 20분 — min_duration_s(300s)·min_samples(20) 를 넘긴다
GROUP_MIN_DELTA = GROUP_RULES[0].min_delta      # 1536MB


def detector() -> ProcessLeakDetector:
    return ProcessLeakDetector(window_s=900.0, min_duration_s=300.0, min_samples=20)


def spread_stream(n_procs: int, total_growth_mb: float, *, name="python.exe",
                  base_mb=20.0, ticks=TICKS, start=1000.0):
    """`n_procs` 개가 각자 조금씩 자라 합계가 `total_growth_mb` 가 되는 관측 스트림."""
    per_tick = total_growth_mb / n_procs / (ticks - 1)
    out = []
    for i in range(ticks):
        procs = [
            ProcessView(pid=1000 + k, name=name, rss_mb=base_mb + per_tick * i)
            for k in range(n_procs)
        ]
        out.append(Observation(ts=start + i * STEP, processes=procs))
    return out


def run(stream) -> list:
    det = detector()
    det.reset()
    return [d for d in (det.observe(o) for o in stream) if d is not None]


# ------------------------------------------------- 막지 않았으면 어땠는가

def test_pid_axis_alone_misses_a_spread_leak():
    """**이 테스트가 그룹 축의 존재 이유다.**

    같은 총량을 8개로 나누면 개별 프로세스의 증가량이 `min_delta`(512MB) 아래라
    PID별 축은 아무것도 못 잡는다. 그룹 축을 뜯어내면 이 상황이 그대로 미탐이 된다.
    """
    det = detector()
    det.reset()
    # 그룹 축을 끈 상태를 흉내 낸다 — 판정만 비활성, 학습 경로는 그대로 둔다.
    det._evaluate_groups = lambda obs, best: best  # type: ignore[method-assign]

    fired = [d for d in (det.observe(o) for o in spread_stream(8, 4000.0)) if d is not None]
    assert not fired, f"PID별 축이 분산 누수를 잡았다면 이 축은 필요 없다: {fired}"


def test_group_axis_catches_what_pid_axis_misses():
    """같은 입력을 그룹 축이 있는 온전한 탐지기에 넣으면 잡힌다."""
    fired = run(spread_stream(8, 4000.0))
    assert fired, "분산 누수를 그룹 축이 잡지 못했다"
    f = fired[0].features
    assert f["metric"] == "rss_mb"
    assert "합계" in f["rule"], f["rule"]
    assert "8개 프로세스" in f["explain"], f["explain"]


# ------------------------------------------------- 문턱

def test_group_threshold_is_where_we_think_it_is():
    """문턱 값 자체를 고정한다. 아래 두 테스트의 입력이 이 값을 전제로 한다."""
    assert GROUP_RULES[0].min_delta == 1536.0


def test_group_below_threshold_does_not_fire():
    """합계가 문턱을 못 넘으면 조용하다.

    **입력을 상수에서 유도하지 않는다.** 처음에는 `GROUP_MIN_DELTA * 0.6` 을 썼는데,
    그러면 문턱을 1MB 로 무력화해도 입력이 0.6MB 로 같이 줄어 **테스트가 따라 움직여
    통과한다**(무력화 검증에서 실제로 놓쳤다). 900 은 1536 보다 작다는 사실만으로
    성립하는 고정값이다.
    """
    fired = run(spread_stream(8, 900.0))
    assert not fired, f"문턱 미달(900MB < 1536MB)인데 발화했다: {fired}"


# ------------------------------------------------- 새 프로세스 계단

def test_new_process_baseline_is_not_counted_as_growth():
    """**새로 뜬 프로세스의 기본 보유량이 증가분으로 둔갑하면 안 된다.**

    자라지 않는 프로세스가 하나씩 새로 뜨기만 하는 상황이다. 합계를 그냥 더하면
    계단이 쌓여 문턱을 넘지만, 증가분만 더하면 0 이다. 크롬 탭을 여는 것이 정확히
    이 모양이라 이걸 막지 못하면 오탐이 쏟아진다.
    """
    stream = []
    for i in range(TICKS):
        n = 1 + i // 2                      # 두 틱마다 한 개씩 새로 뜬다
        procs = [
            ProcessView(pid=2000 + k, name="chrome.exe", rss_mb=800.0)   # 전혀 안 자란다
            for k in range(n)
        ]
        stream.append(Observation(ts=1000.0 + i * STEP, processes=procs))
    fired = run(stream)
    assert not fired, f"새로 뜬 프로세스의 기본 보유량을 누수로 셌다: {fired}"


# ------------------------------------------------- 묶으면 안 되는 것

def test_shared_hosts_are_not_grouped():
    """`svchost` 는 이름이 같아도 서로 다른 프로그램이다. 합치면 틀린 답이 나온다.

    **대조를 같은 테스트 안에 둔다.** "svchost 가 발화 안 함"만 보면 그룹 축이
    통째로 죽어도 통과한다 — 실제로 무력화 검증에서 그렇게 놓쳤다. 이름만 바꾼
    같은 입력이 발화해야, svchost 의 침묵이 *공유 호스트라서* 임을 말할 수 있다.
    """
    shared = run(spread_stream(8, 4000.0, name="svchost.exe"))
    normal = run(spread_stream(8, 4000.0, name="python.exe"))
    assert normal, "대조군이 발화하지 않았다 — 이 테스트는 아무것도 검증하지 못한다"
    assert not shared, f"공유 호스트를 이름으로 합쳤다: {shared}"


def test_single_process_is_not_reported_twice():
    """프로세스가 하나면 PID별 축이 이미 본다. 그룹이 또 신고하면 사건이 둘로 쪼개진다."""
    fired = run(spread_stream(1, 4000.0))
    assert len(fired) <= 1, f"같은 누수를 두 축이 각각 신고했다: {fired}"
    if fired:
        assert not fired[0].features.get("explain", "").startswith("python.exe 1개"), \
            "프로세스 1개짜리를 그룹으로 신고했다"

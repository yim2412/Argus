"""프로세스 누수 탐지(Phase 6-A) 검증.

**여기서 조심할 것: 조건을 하나씩만 위반하는 케이스여야 한다.**

이 규칙은 조용히 깨진다 — 조건 하나를 지워도 예외가 나지 않고 값만 달라진다. 그런데
여러 조건을 동시에 어기는 케이스만 두면, 어느 하나를 지워도 나머지가 막아 줘서
테스트가 통과한다. 2026-07-29 첫 판이 정확히 그 상태였다(네 조건이 검증되지 않았다).
그래서 아래 케이스는 **각 조건을 정확히 하나씩만** 위반하도록 값을 맞춰 두었다.
값을 고칠 때는 다른 조건에 걸리지 않는지 함께 확인할 것.

`argus/detection/procleak.py` 의 `__main__` 스모크와 역할이 다르다. 스모크는 탐지기
전체를 스트림으로 돌려 보고, 여기서는 판정 함수(`judge`)를 직접 찔러 경계를 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection.base import Observation, ProcessView, run_detector  # noqa: E402
from argus.detection.procleak import (  # noqa: E402
    MetricRule,
    ProcessLeakDetector,
    _Track,
    judge,
    rules_from_settings,
)

RULE = MetricRule("handles", "핸들", "개", growth_ratio=3.0, min_delta=500.0, monotonic_ratio=0.85)
JUDGE_KW = {"min_duration_s": 300.0, "min_samples": 20}


def track_of(values, *, step=1.0, start=1000.0) -> _Track:
    """급락 리셋을 거치지 않고 시계열을 그대로 넣는다 (판정 로직만 보기 위해)."""
    t = _Track()
    for i, v in enumerate(values):
        t.samples.append((start + i * step, float(v)))
    return t


def stream(values, *, pid=999, name="leaky", attr="handles", step=1.0, start=1000.0):
    return [
        Observation(ts=start + i * step, processes=[ProcessView(pid=pid, name=name, **{attr: v})])
        for i, v in enumerate(values)
    ]


# --------------------------------------------------------------- 판정 경계

def test_detects_steady_growth():
    """계단식으로 계속 자라는 것은 누수다."""
    verdict = judge(track_of([400 + i * 10 for i in range(600)]), RULE, **JUDGE_KW)
    assert verdict.leaking, verdict.reason
    assert verdict.ratio > 3.0


@pytest.mark.parametrize("label,values,expected_reason", [
    # 배수만 부족 — 증가량 900, 단조 증가, 10분. 1.2배라 누수가 아니다.
    ("배수", [5000 + i * 2 for i in range(600)], "배수 부족"),
    # 증가량만 부족 — 10배지만 핸들 90개다.
    ("증가량", [10 + i * 0.15 for i in range(600)], "증가량 부족"),
    # 단조성만 부족 — 400→4000 이지만 매 틱 오르내린다. 일하는 중이지 새는 게 아니다.
    ("단조성", [(400 + i * 6) * (1.2 if i % 2 else 0.8) for i in range(600)], "등락함"),
    # 지속만 부족 — 배수·증가량·단조성 충분하지만 2분짜리다. 켤 때의 급증이다.
    ("지속", [400 + i * 30 for i in range(120)], "지속 부족"),
    # 표본만 부족 — 30분에 걸쳐 10번만 관측됐다. 분포를 말할 표본이 아니다.
    ("표본", [400 + i * 400 for i in range(10)], "표본 부족"),
])
def test_rejects_when_single_condition_fails(label, values, expected_reason):
    """**각 케이스는 조건을 정확히 하나만 위반한다.** 그래야 그 조건이 검증된다."""
    step = 180.0 if label == "표본" else 1.0
    verdict = judge(track_of(values, step=step), RULE, **JUDGE_KW)
    assert not verdict.leaking, f"{label}: 걸러야 하는데 누수로 판정했다"
    assert expected_reason in verdict.reason, f"{label}: 다른 조건에 걸렸다 — {verdict.reason}"


# --------------------------------------------------------------- 추적 상태

def test_drop_resets_track():
    """값이 크게 떨어지면 이력을 버린다. PID 재사용으로 남의 값이 이어 붙는 것을 막는다."""
    t = _Track()
    for i in range(100):
        t.add(1000.0 + i, 1000.0 + i, window_s=900.0, drop_ratio=0.5)
    assert len(t.samples) == 100
    t.add(1100.0, 100.0, window_s=900.0, drop_ratio=0.5)
    assert len(t.samples) == 1, "급락 후에도 이전 이력이 남아 있다"


def test_pid_reuse_does_not_extend_duration():
    """죽은 프로세스의 이력이 이어지면 3분짜리가 '13분간 상승'으로 보인다."""
    reused = stream(
        [100 + i * 10 for i in range(300)] + [100 + i * 17 for i in range(180)]
    )
    late = [d for d in run_detector(ProcessLeakDetector(), reused) if d.ts >= 1000.0 + 300]
    assert not late, "PID 재사용 뒤 3분 급증에서 발화했다"


def test_cooldown_limits_repeats():
    """누수는 고칠 때까지 계속된다. 쿨다운이 없으면 알림이 영원히 반복된다."""
    long_leak = stream([400 + i * 10 for i in range(1800)])
    hits = run_detector(ProcessLeakDetector(cooldown_s=1800.0), long_leak)
    assert 0 < len(hits) <= 2, f"30분 지속 누수에서 {len(hits)}건 — 쿨다운이 듣지 않는다"


def test_tracking_is_bounded():
    """프로세스가 많은 PC 에서도 추적 대상에 상한이 있어야 한다.

    정리는 **틱 경계**에서 일어난다. 한 틱 안에서 자리가 없을 때마다 축출하면 쫓겨난
    프로세스가 다음 순번의 자리를 빼앗는 도미노가 돌기 때문이다(`_enforce_limit` 참고).
    그래서 한 틱 동안은 잠시 초과하고, 다음 틱 시작에 상한으로 돌아온다.
    """
    detector = ProcessLeakDetector(max_tracked=10, eval_interval_s=30.0)
    procs = [ProcessView(pid=i, name=f"p{i}", handles=100) for i in range(200)]
    detector.observe(Observation(ts=1000.0, processes=procs))
    detector.observe(Observation(ts=1030.0, processes=procs))
    assert len(detector._tracks) <= 10 + len(procs), "초과가 무한정 쌓인다"
    # 정리 주기가 지나야 상한으로 돌아온다 (매 틱 훑으면 관측자가 무거워지므로).
    detector.observe(Observation(ts=1060.0, processes=procs[:1]))
    assert len(detector._tracks) <= 11, "정리 주기가 지나도 상한으로 돌아오지 않는다"


def test_new_process_is_admitted_when_full():
    """**상한이 찼어도 새 프로세스를 받아야 한다.**

    상한을 넘으면 새 트랙을 거절하는 구조였고, 그래서 프로세스가 200종인 PC 에서는
    기동 직후 자리가 다 차고 그 뒤에 뜨는 프로세스가 영영 관측되지 않았다. 누수는
    대개 새로 뜬 프로세스에서 생기므로 봐야 할 것을 정확히 못 보는 상태였다.
    2026-07-29 결함주입에서 주입 프로세스를 통째로 놓치며 드러났다.
    """
    old = [ProcessView(pid=i, name=f"old{i}", handles=100) for i in range(50)]
    detector = ProcessLeakDetector(max_tracked=10, eval_interval_s=30.0)
    for i in range(3):  # 정리 주기를 넘겨 가며 상한까지 줄인다
        detector.observe(Observation(ts=1000.0 + i * 30, processes=old))
    detector.observe(Observation(ts=1120.0, processes=[]))
    assert len(detector._tracks) <= 10

    # 뒤늦게 뜬 프로세스. 상한은 이미 찼다.
    detector.observe(Observation(
        ts=1010.0, processes=[ProcessView(pid=9999, name="latecomer", handles=100)]
    ))
    assert any(k[0] == 9999 for k in detector._tracks), "상한이 찼다고 새 프로세스를 거절했다"


def test_leak_is_caught_when_tracking_is_already_full():
    """상한이 **이미 꽉 찬** 뒤에 뜬 프로세스의 누수도 잡아야 한다 — 위 버그의 종단 확인.

    07-29 실측이 정확히 이 모양이었다: 프로세스 200종 × 지표 2개 = 트랙 400 으로
    상한 400 이 정확히 찼고, 그 뒤에 뜬 주입 프로세스가 트랙을 얻지 못해 12분간
    핸들이 210 → 8523 으로 늘도록 아무 일도 일어나지 않았다.

    상한을 트랙 수와 같게 두어 **자리를 다투는 상태**를 만든다. 평평한 배경보다
    자라는 트랙이 살아남아야 한다.
    """
    noise = [ProcessView(pid=1000 + i, name=f"bg{i}", handles=200) for i in range(200)]
    detector = ProcessLeakDetector(max_tracked=len(noise))

    # 먼저 배경만으로 상한을 가득 채운다.
    for i in range(30):
        detector.observe(Observation(ts=1000.0 + i, processes=noise))
    assert len(detector._tracks) == len(noise)

    # 그 뒤에 누수 프로세스가 등장한다. 자리는 이미 없다.
    fired = []
    for i in range(30, 700):
        obs = Observation(
            ts=1000.0 + i,
            processes=noise + [ProcessView(pid=42, name="leaky", handles=400 + i * 10)],
        )
        d = detector.observe(obs)
        if d is not None:
            fired.append(d)

    assert fired, "상한이 찬 뒤 등장한 프로세스의 누수를 놓쳤다"
    assert fired[0].features["pid"] == 42


def test_dead_processes_are_evicted():
    """사라진 프로세스가 메모리에 쌓이면 관측자가 무거워진다."""
    detector = ProcessLeakDetector(window_s=100.0)
    detector.observe(Observation(ts=1000.0, processes=[ProcessView(pid=1, name="gone", handles=50)]))
    assert detector._tracks
    detector.observe(Observation(ts=2000.0, processes=[ProcessView(pid=2, name="alive", handles=50)]))
    assert not any(k[0] == 1 for k in detector._tracks), "죽은 프로세스가 남아 있다"


# --------------------------------------------------------------- 비용

def test_evaluation_cost_stays_bounded():
    """**관측자는 가벼워야 한다.** 판정이 매 틱 전체 트랙을 훑으면 예산을 넘긴다.

    처음엔 매 틱(1초) 추적 중인 트랙 전부에 중앙값 계산을 돌렸다. 실측 758트랙 ×
    30,654틱에서 24시간 리플레이가 CPU 617초를 쓰고도 끝나지 않았다. 실시간에서도
    CPU 예산 2% 를 지킬 수 없다는 뜻이라 판정에 주기를 뒀다(`eval_interval_s`).

    시간을 재는 대신 **판정 횟수**를 센다. 시간은 기계 사양에 따라 흔들려 회귀를
    가리지만, 호출 횟수는 결정적이다.
    """
    import argus.detection.procleak as mod

    calls = []
    real_judge = mod.judge
    mod.judge = lambda *a, **kw: (calls.append(1), real_judge(*a, **kw))[1]
    try:
        detector = ProcessLeakDetector(eval_interval_s=30.0)
        procs = [ProcessView(pid=i, name=f"p{i}", handles=100 + i) for i in range(300)]
        for i in range(600):  # 10분치 1Hz
            detector.observe(Observation(ts=1000.0 + i, processes=procs))
    finally:
        mod.judge = real_judge

    # 주기 없이 매 틱 돌면 600틱 × 300트랙 = 180,000 번이다.
    assert len(calls) < 20_000, f"판정이 {len(calls)}회 — 주기·사전배제가 듣지 않는다"


def test_prefilter_never_hides_a_leak():
    """사전 배제는 **놓치는 쪽으로 틀리면 안 된다.** 정식 판정과 결과가 같아야 한다."""
    from argus.detection.procleak import _may_grow

    for values in (
        [400 + i * 10 for i in range(600)],                       # 누수
        [5000 + i * 2 for i in range(600)],                       # 배수 부족
        [(400 + i * 6) * (1.2 if i % 2 else 0.8) for i in range(600)],  # 톱니
        [10 + i * 0.15 for i in range(600)],                      # 증가량 부족
    ):
        t = track_of(values)
        verdict = judge(t, RULE, **JUDGE_KW)
        if verdict.leaking:
            assert _may_grow(t, RULE.growth_ratio), "사전 배제가 진짜 누수를 걸렀다"


# --------------------------------------------------------------- 하드웨어 독립

def test_rss_threshold_scales_with_ram():
    """RSS 문턱은 절대값이면 안 된다. 4GB PC 와 64GB PC 에서 같은 뜻이어야 한다."""
    class FakeProfile:
        def __init__(self, gb):
            self.memory = {"total_gb": gb}

    from argus.config.loader import ProcessLeakSettings

    cfg = ProcessLeakSettings()
    small = {r.attr: r for r in rules_from_settings(cfg, FakeProfile(4))}["rss_mb"]
    large = {r.attr: r for r in rules_from_settings(cfg, FakeProfile(64))}["rss_mb"]
    assert large.min_delta > small.min_delta * 4, "RAM 이 16배인데 문턱이 따라 오르지 않는다"


def test_rss_threshold_falls_back_without_profile():
    """캘리브레이션 전에는 문턱이 0 이 되면 안 된다 — 0 이면 오탐이 쏟아진다."""
    from argus.config.loader import ProcessLeakSettings

    rules = {r.attr: r for r in rules_from_settings(ProcessLeakSettings(), None)}
    assert rules["rss_mb"].min_delta > 0


# --------------------------------------------------------------- 계약

def test_detector_does_not_read_wall_clock():
    """리플레이 계약: 판정은 `obs.ts` 만 본다. 실제 시각을 읽으면 배속 재생이 틀어진다."""
    import argus.detection.procleak as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    body = source.split('if __name__ ==')[0]  # 스모크는 제외
    assert "time.time()" not in body, "탐지 경로에서 벽시계를 읽는다"

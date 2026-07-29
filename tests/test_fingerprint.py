"""프로세스 지문(Phase 6-B) 검증.

**지문은 조용히 틀리는 쪽이다.** 분위수 계산이 잘못돼도 예외가 나지 않고 숫자만
그럴듯하게 달라진다. 그래서 여기서는 **알려진 분포를 넣어 기대값과 대조한다** —
"돌아간다"가 아니라 "맞다"를 확인해야 한다.

억제 쪽에서 특히 조심할 것은 **방향**이다. 지문이 없을 때 막아 버리면 신규 프로세스의
누수를 통째로 놓친다(6-A 에서 추적 상한 때문에 실제로 겪었다). 그래서 "지문이 없으면
막지 않는다"를 별도로 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection.base import Observation, ProcessView, run_detector  # noqa: E402
from argus.detection.fingerprint import Fingerprint, quantile  # noqa: E402
from argus.detection.procleak import ProcessLeakDetector  # noqa: E402


def fp(name: str, stat: str, p99: float) -> Fingerprint:
    return Fingerprint(name=name, stat=stat, p50=p99 / 4, p95=p99 / 2, p99=p99,
                       maximum=p99 * 1.5, samples=200, days=3)


def leak_stream(values, *, pid=42, name="leaky", attr="handles"):
    return [
        Observation(ts=1000.0 + i, processes=[ProcessView(pid=pid, name=name, **{attr: v})])
        for i, v in enumerate(values)
    ]


# --------------------------------------------------------------- 분위수

def test_quantile_matches_known_distribution():
    """1..100 에서 p50=50, p99=99. 보간이 아니라 **실제 관측된 값**이어야 한다."""
    values = [float(i) for i in range(1, 101)]
    assert quantile(values, 0.50) == 50.0
    assert quantile(values, 0.95) == 95.0
    assert quantile(values, 0.99) == 99.0


def test_quantile_returns_observed_values_only():
    """지문의 기준은 "실제로 도달한 수준"이다. 없는 값을 만들어 내면 안 된다."""
    values = [10.0, 20.0, 1000.0]
    for p in (0.0, 0.25, 0.5, 0.75, 0.99, 1.0):
        assert quantile(values, p) in values


def test_quantile_is_monotonic():
    """p50 ≤ p95 ≤ p99 가 깨지면 계산이 틀린 것이다 — 예외 없이 조용히 틀린다."""
    import random

    random.seed(3)
    values = [random.random() * 1000 for _ in range(500)]
    assert quantile(values, 0.5) <= quantile(values, 0.95) <= quantile(values, 0.99)


def test_quantile_rejects_empty():
    with pytest.raises(ValueError):
        quantile([], 0.5)


# --------------------------------------------------------------- 억제 방향

def test_suppresses_when_within_normal():
    """평소 범위 안이면 막는다 — medal 핸들 건(도달 1,395 / 평소 p99 12,466)."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 12466)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert not fired, "평소 범위인데 발화했다"
    assert detector.suppressed > 0, "억제 카운터가 오르지 않았다"


def test_does_not_suppress_when_above_normal():
    """평소를 넘으면 막지 않는다 — 주입 건(도달 8,523 / 평소 p99 2,768)."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 2768)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "평소를 넘었는데 억제됐다 — 진짜 누수를 놓친다"


def test_no_fingerprint_means_no_suppression():
    """**지문이 없으면 막지 않는다.** 모르는 것을 막는 방향으로는 틀지 않는다.

    6-A 에서 추적 상한 때문에 신규 프로세스를 통째로 놓친 적이 있다. 누수는 대개
    새로 뜬 프로세스에서 생기므로, 여기서 같은 실수를 반복하면 안 된다.
    """
    detector = ProcessLeakDetector()
    detector.fingerprints = {}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "지문이 없는데 억제됐다"
    assert detector.suppressed == 0


def test_fingerprint_of_another_process_is_not_applied():
    """이름이 다르면 남의 지문을 쓰면 안 된다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("other", "handles_max"): fp("other", "handles_max", 999999)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "다른 프로세스의 지문으로 억제했다"


def test_fingerprint_of_another_metric_is_not_applied():
    """핸들 지문으로 메모리를 억제하면 안 된다 — 단위가 다르다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "rss_p95"): fp("leaky", "rss_p95", 999999)}
    fired = run_detector(detector, leak_stream([400 + i * 10 for i in range(600)]))
    assert fired, "다른 지표의 지문으로 억제했다"


def test_reset_keeps_fingerprints():
    """지문은 상태가 아니라 학습 결과다. reset 으로 버리면 리플레이 재현성이 깨진다."""
    detector = ProcessLeakDetector()
    detector.fingerprints = {("leaky", "handles_max"): fp("leaky", "handles_max", 100)}
    detector.reset()
    assert detector.fingerprints, "reset 이 지문까지 버렸다"

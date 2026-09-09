"""룰이 **왜 발화하지 못했는지**를 남긴다.

`observe()` 는 발화할 때만 판정을 돌려주므로 "안 잡혔다"만 남고 이유는 사라진다.
2026-09-08 을 조사할 때, 원본을 되살려 재생해도 **0건이라는 사실만 다시 확인**할 뿐이었다.

**여기 있는 것은 전부 조용히 깨지는 종류다.** 추적이 통째로 죽어도 탐지는 멀쩡히 돌고
발화도 그대로 난다 — 사라지는 것은 진단 정보뿐이라 실행 중에는 아무 신호가 없다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.detection.rules import Condition, Rule, RuleEngine  # noqa: E402
from argus.detection.trace import RuleTrace  # noqa: E402
from argus.eval.replay import Observation  # noqa: E402


def _engine(rule: Rule, *, window_s: float = 60.0, min_samples: int = 3) -> RuleEngine:
    engine = RuleEngine([rule], window_s=window_s, min_samples=min_samples)
    engine.reset()
    return engine


def _rule(**over) -> Rule:
    """`cpu_total > 50` 이 기본. **문턱을 테스트가 직접 만든다** — 여기서 재는 것은
    판정 로직이 아니라 "어느 관문에서 멈췄는지가 기록되는가"다. 설정 배선은 관심사가 아니다."""
    params = dict(
        name="테스트룰",
        conditions=[Condition(metric="cpu_total", op=">", value=50)],
        for_s=30.0,
        cooldown_s=600.0,
    )
    params.update(over)
    return Rule(**params)


def _obs(ts: float, cpu: float) -> Observation:
    return Observation(ts=ts, metrics={"cpu_total": cpu}, processes=[], gpus=[])


def _feed(engine: RuleEngine, values: list[tuple[float, float]]) -> None:
    for ts, cpu in values:
        engine.observe(_obs(ts, cpu))


# --------------------------------------------------------------- 기본 계약


def test_tracing_is_off_by_default() -> None:
    """**상주는 이 경로를 타지 않는다.** 관측자는 가벼워야 한다(설계 규칙 1)."""
    assert _engine(_rule()).trace is None


def test_tracing_does_not_change_the_verdict() -> None:
    """추적을 켜도 발화가 달라지면 안 된다 — 진단이 판정을 흔들면 쓸 수 없다."""
    values = [(float(i), 90.0) for i in range(120)]

    plain = _engine(_rule())
    _feed(plain, values)

    traced = _engine(_rule())
    traced.trace = RuleTrace()
    _feed(traced, values)

    assert plain._last_fired == traced._last_fired, "추적을 켜자 발화 시각이 달라졌다"


# --------------------------------------------------------------- 관문 구분


def test_holding_records_how_long_it_actually_held() -> None:
    """**이 파일에서 가장 중요한 항목이다.**

    "조건은 참이었는데 최장 N 초" 가 곧 답이다. 조건이 아예 거짓인 것과 지속이
    모자란 것은 고칠 곳이 다른데, 발화 0건만 보면 둘이 구분되지 않는다.
    """
    engine = _engine(_rule(for_s=30.0))
    engine.trace = RuleTrace()
    # 베이스라인을 채운 뒤 20초만 참으로 유지한다 (문턱 30초에 못 미친다)
    _feed(engine, [(float(i), 10.0) for i in range(10)])
    _feed(engine, [(10.0 + i, 90.0) for i in range(20)])

    stat = engine.trace.stats["테스트룰"]
    assert stat.counts["fired"] == 0, "30초를 못 채웠는데 발화했다"
    assert stat.counts["holding"] > 0, "참인데 지속 미달인 구간이 기록되지 않았다"
    assert 18.0 <= stat.longest_hold_s <= 20.0, (
        f"버틴 시간이 실제와 다르다: {stat.longest_hold_s}"
    )
    assert stat.need_s == 30.0, "비교 대상인 문턱이 안 남았다 — 숫자만으로는 판단할 수 없다"
    assert "모자랐다" in stat.verdict


def test_false_and_unknown_are_not_the_same() -> None:
    """지표가 없어서 못 판정한 것을 '조건 거짓'으로 세면 엉뚱한 곳을 고치게 된다."""
    engine = _engine(_rule())
    engine.trace = RuleTrace()
    _feed(engine, [(float(i), 10.0) for i in range(10)])
    # 이 룰이 보는 지표를 뺀 관측
    for i in range(5):
        engine.observe(Observation(ts=100.0 + i, metrics={}, processes=[], gpus=[]))

    stat = engine.trace.stats["테스트룰"]
    assert stat.counts["false"] > 0, "조건이 거짓이던 구간이 없다"
    assert stat.counts["unknown"] > 0, "지표가 없는 구간이 '판정 불가'로 안 세어졌다"


def test_unready_is_recorded_before_the_baseline_stands() -> None:
    """부트스트랩 구간을 '조건 거짓'으로 세면 "한 번도 참이 아니었다"가 거짓말이 된다."""
    engine = _engine(_rule(), min_samples=50)
    engine.trace = RuleTrace()
    _feed(engine, [(float(i), 90.0) for i in range(5)])

    stat = engine.trace.stats["테스트룰"]
    assert stat.counts["unready"] == 5, f"베이스라인 전 구간이 안 세어졌다: {stat.counts}"
    assert stat.counts["false"] == 0


def test_firing_is_recorded_with_the_hold_that_earned_it() -> None:
    """반대쪽. 이게 없으면 위 테스트들은 '항상 발화 안 함'으로도 통과한다."""
    engine = _engine(_rule(for_s=10.0))
    engine.trace = RuleTrace()
    _feed(engine, [(float(i), 10.0) for i in range(10)])
    _feed(engine, [(10.0 + i, 90.0) for i in range(40)])

    stat = engine.trace.stats["테스트룰"]
    assert stat.counts["fired"] >= 1, f"참이 40초 이어졌는데 발화가 없다: {stat.counts}"
    assert stat.longest_hold_s >= 10.0
    assert "발화" in stat.verdict


def test_cooldown_is_distinguished_from_not_qualifying() -> None:
    """쿨다운에 막힌 것은 **문턱을 이미 넘은 것**이다. 안 넘은 것과 같이 세면 안 된다."""
    engine = _engine(_rule(for_s=5.0, cooldown_s=600.0))
    engine.trace = RuleTrace()
    _feed(engine, [(float(i), 10.0) for i in range(10)])
    _feed(engine, [(10.0 + i, 90.0) for i in range(60)])

    stat = engine.trace.stats["테스트룰"]
    assert stat.counts["fired"] == 1, f"쿨다운이 있는데 여러 번 발화했다: {stat.counts}"
    assert stat.counts["cooldown"] > 0, "쿨다운에 막힌 틱이 기록되지 않았다"
    # **억제가 몇 번 일했는지가 한 줄 판정에도 보여야 한다.** "발화 1건"만 보이면
    # 조건이 한 번 스친 것과, 계속 참인데 억제가 수십 번 막은 것이 같아 보인다 —
    # 알림량을 조정할 때 그 둘은 완전히 다른 상황이다.
    assert "쿨다운" in stat.verdict, f"쿨다운이 한 줄 판정에서 사라졌다: {stat.verdict}"
    assert str(stat.counts["cooldown"]) in stat.verdict


# ------------------------------------------------------------------ 정렬


def test_closest_rule_comes_first() -> None:
    """아무 일도 없던 룰이 먼저 나오면 표가 쓸모없어진다."""
    trace = RuleTrace()
    trace.record("조용한룰", "false")
    trace.record("가까웠던룰", "holding", held_s=25.0, need_s=30.0)
    trace.record("발화한룰", "fired", held_s=30.0, need_s=30.0)

    order = [s.name for s in trace.ordered()]
    assert order[0] == "발화한룰", order
    assert order[1] == "가까웠던룰", order

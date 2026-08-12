"""부하 조건부 베이스라인 (2026-08-12).

**온도는 상한이 걸린 지표다.** 스로틀링이 온도를 열 평형점에서 잡아 주므로, 유휴가
섞인 창으로 `median + k*sigma` 를 만들면 문턱이 평형점보다 높아져 어떤 k 로도 도달할
수 없다. `GPU 고온 지속` 룰이 08-03 이후 그 상태였다 — 08-11 의 90도+ 317초 구간을
실제 엔진에 재생하니 문턱이 102~120도로 계산되어 발화 0건이었다.

**여기서 잡으려는 것은 조용히 깨지는 쪽이다.** 이 축이 통째로 죽어도 예외는 나지
않는다 — `stats_under_load` 가 `None` 을 돌려주고 룰은 그냥 판정 불가가 되어, 사용자
눈에는 "아무 이상 없음"과 구별되지 않는다. 그게 정확히 이번에 9일간 벌어진 일이다.

그래서 세 가지를 **따로** 잰다.
  1. 판정 로직   — 부하 축이 유휴를 배제하는가, 문턱이 뜻대로 나오는가
  2. config 배선 — YAML 을 고치면 실제로 판정이 바뀌는가 (**기본값이 아닌 값으로**)
  3. 발화 가능성 — 정상 냉각 PC 는 안 울리고 열화된 PC 는 울리는가 (룰 전체 경로)

2번을 기본값으로 재면 안 되는 이유: `assert engine.load_window_s == cfg.load_window_s`
는 코드 기본값과 YAML 기본값이 같으면 **배선이 끊겨도 참이다.** 2026-08-04 에 같은
유형이 하루에 네 번 나왔다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.detection.base import Observation  # noqa: E402
from argus.detection.baseline import BaselineSet, LoadGate  # noqa: E402
from argus.detection.rules import Condition, Rule, RuleEngine  # noqa: E402

GATE = {"gpu_temp_c": LoadGate(metric="gpu_util_percent", min_value=80.0)}


def _set(**kw) -> BaselineSet:
    opts = dict(
        window_s=100_000.0,
        min_samples=10,
        load_gates=GATE,
        load_window_s=100_000.0,
        load_min_interval_s=0.0,
        load_min_samples=10,
    )
    opts.update(kw)
    return BaselineSet(**opts)


def _feed(bs: BaselineSet, temp: float, util: float, n: int, start: float = 0.0) -> float:
    ts = start
    for _ in range(n):
        bs.observe(ts, {"gpu_temp_c": temp, "gpu_util_percent": util})
        ts += 1.0
    return ts


# --------------------------------------------------------------- 1. 판정 로직

def test_load_axis_excludes_idle():
    """부하 축은 유휴 표본을 담지 않는다 — 이 축의 존재 이유 전부가 여기 있다."""
    bs = _set()
    ts = _feed(bs, temp=40.0, util=5.0, n=200)      # 유휴: 40도
    _feed(bs, temp=93.0, util=95.0, n=60, start=ts)  # 부하: 93도

    load = bs.stats_under_load("gpu_temp_c")
    assert load is not None, "부하 표본이 60개인데 축이 서지 않았다"
    assert load.median == 93.0, f"부하 축에 유휴가 섞였다: median={load.median}"

    # 전역은 유휴가 다수라 40도 쪽으로 끌려간다. 그 값으로 문턱을 만들면
    # 90도가 상시 초과가 되어 08-02 에 기각된 오탐 상태로 돌아간다.
    overall = bs.stats("gpu_temp_c")
    assert overall.median < 90.0, "전역이 부하 쪽으로 끌려갔다 — 표본 구성이 잘못됐다"
    assert load.median != overall.median


def _spread(bs: BaselineSet, temps, util: float, n: int, start: float = 0.0) -> float:
    """온도를 여러 값에 걸쳐 넣는다.

    **산포가 이 문제의 핵심이라 합성 데이터에도 있어야 한다.** 한 값으로만 채우면
    MAD 가 0 이 되어 σ 가 `sigma_floor_ratio`(중앙값의 5%)까지 내려가고, 그러면
    실제 데이터에서 벌어진 일(MAD 6~8도 → 문턱 102~120도)이 재현되지 않는다.
    처음 이 테스트를 그렇게 썼다가 전제가 깨졌다.
    """
    ts = start
    for i in range(n):
        bs.observe(ts, {"gpu_temp_c": temps[i % len(temps)], "gpu_util_percent": util})
        ts += 1.0
    return ts


# 실측 형태를 따른다. 이 PC 의 부하 시 온도는 88~93도 사이에서 흔들렸다.
IDLE_TEMPS = (38.0, 42.0, 46.0, 50.0, 44.0, 40.0)
HOT_LOAD_TEMPS = (88.0, 90.0, 92.0, 93.0, 91.0, 89.0)
COOL_LOAD_TEMPS = (60.0, 64.0, 68.0, 66.0, 62.0, 65.0)
DEGRADED_TEMPS = (91.0, 92.0, 91.0, 93.0, 92.0, 91.0)


def test_capped_metric_threshold_is_unreachable_but_load_axis_is_not():
    """상한이 걸린 지표에서 σ 기반 문턱은 **물리적 상한보다 높다** — 그래서 도달 불가.

    이것이 08-03 이후 `GPU 고온 지속` 이 한 번도 울리지 않은 이유다. 재생에서 엔진이
    계산한 문턱은 102~120도였고 GPU 는 96도를 넘은 적이 없다.
    """
    bs = _set()
    ts = _spread(bs, IDLE_TEMPS, util=3.0, n=200)
    _spread(bs, HOT_LOAD_TEMPS, util=92.0, n=1500, start=ts)  # 게임 중 30분 창

    overall = bs.stats("gpu_temp_c")
    sigma_threshold = overall.threshold(3)
    hottest = max(HOT_LOAD_TEMPS)
    assert sigma_threshold > hottest, (
        f"σ 문턱({sigma_threshold:.1f})이 상한({hottest})보다 낮으면 이 테스트의 전제가 "
        f"틀렸다 — 진단을 다시 봐야 한다"
    )

    load = bs.stats_under_load("gpu_temp_c")
    assert load.median + 5 > hottest, (
        "부하 축 문턱이 상한보다 낮으면 이 기계에서 상시 발화한다 — 08-02 에 기각된 상태"
    )


def test_gate_closed_when_gate_metric_missing():
    """게이트 값을 모르면 담지 않는다. 모르는 것을 부하로 치면 유휴가 섞인다."""
    bs = _set()
    for i in range(200):
        bs.observe(float(i), {"gpu_temp_c": 93.0})   # 사용률 없음
    assert bs.stats_under_load("gpu_temp_c") is None, "게이트 없는 표본이 담겼다"


def test_load_axis_does_not_fall_back_to_global():
    """표본이 덜 모이면 `None`. **전역으로 조용히 물러나지 않는다.**

    프로그램 축은 폴백이 맞았지만 여기는 반대다 — 유휴가 섞인 전역으로 물러나면
    문턱이 다시 도달 불가가 되고, 룰이 서 있는 척하며 발화하지 않는 그 상태로 돌아간다.
    """
    bs = _set(load_min_samples=50)
    ts = _feed(bs, temp=40.0, util=2.0, n=300)
    _feed(bs, temp=93.0, util=95.0, n=10, start=ts)   # 부하 표본 10개 < 50

    assert bs.stats("gpu_temp_c") is not None, "전역은 서 있어야 한다 (대조군)"
    assert bs.stats_under_load("gpu_temp_c") is None, "표본 부족인데 전역으로 폴백했다"


def test_load_min_interval_thins_samples():
    """표본 간 최소 간격이 실제로 솎아 낸다 — 부하 축은 창이 길어 메모리가 걸린다."""
    dense = _set(load_min_interval_s=0.0)
    thin = _set(load_min_interval_s=5.0)
    for bs in (dense, thin):
        _feed(bs, temp=93.0, util=95.0, n=100)
    assert dense.load_readiness()["gpu_temp_c"] == 100
    assert thin.load_readiness()["gpu_temp_c"] == 20, (
        f"5초 간격이면 100초에 20개여야 한다: {thin.load_readiness()}"
    )


def test_reset_clears_load_axis():
    bs = _set()
    _feed(bs, temp=93.0, util=95.0, n=60)
    assert bs.stats_under_load("gpu_temp_c") is not None
    bs.reset()
    assert bs.stats_under_load("gpu_temp_c") is None, "reset 이 부하 축을 남겼다"


def test_no_gate_means_no_load_axis():
    """게이트를 설정하지 않은 메트릭은 축을 만들지 않는다 (메모리)."""
    bs = _set(load_gates={})
    _feed(bs, temp=93.0, util=95.0, n=100)
    assert bs.stats_under_load("gpu_temp_c") is None
    assert bs.load_readiness() == {}


# --------------------------------------------------------------- 2. config 배선

def test_config_wiring_uses_non_default_values(tmp_path, monkeypatch):
    """YAML 을 고치면 판정이 바뀐다. **기본값이 아닌 값으로 재야 의미가 있다.**"""
    from argus.config import loader

    settings_yaml = tmp_path / "settings.yaml"
    # 전부 기본값과 다른 값으로. 기본은 21600 / 5.0 / 60 / util>=80 이다.
    settings_yaml.write_text(
        "detection:\n"
        "  load_window_s: 777.0\n"
        "  load_min_interval_s: 3.0\n"
        "  load_min_samples: 11\n"
        "  load_gates:\n"
        "    gpu_temp_c: {metric: gpu_util_percent, min: 42.0}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "user_config_path", lambda: settings_yaml)

    if True:
        from argus.detection import rules as rules_module

        engine = rules_module.build()
        bs = engine.baselines
        assert bs.load_window_s == 777.0, f"load_window_s 배선 끊김: {bs.load_window_s}"
        assert bs.load_min_interval_s == 3.0, "load_min_interval_s 배선 끊김"
        assert bs.load_min_samples == 11, "load_min_samples 배선 끊김"
        gate = bs.load_gates["gpu_temp_c"]
        assert gate.min_value == 42.0, f"게이트 문턱 배선 끊김: {gate.min_value}"

        # 배선이 값만 옮기고 **판정에 쓰이지 않으면** 위 assert 는 전부 통과한다.
        # 그래서 그 문턱으로 실제 판정이 갈리는지 확인한다: 사용률 50 은 기본값(80)
        # 에서는 유휴지만 이 설정(42)에서는 부하다.
        for i in range(30):
            bs.observe(float(i * 3), {"gpu_temp_c": 70.0, "gpu_util_percent": 50.0})
        load = bs.stats_under_load("gpu_temp_c")
        assert load is not None and load.median == 70.0, (
            "게이트 문턱이 판정에 쓰이지 않았다 — 값만 옮겨진 상태다"
        )


def test_load_gate_is_on_by_default_everywhere(tmp_path, monkeypatch):
    """**부하 게이트는 아무 설정 없이도 켜져 있다.**

    이 축이 없으면 `GPU 고온 지속` 룰은 발화가 **구조적으로 불가능**하다 — 상한이
    걸린 지표(온도)는 전역 MAD 로 문턱을 세우면 102~120도가 나오고, 그 온도에
    도달하는 하드웨어는 없다(08-11 구간 재생: 90도 초과 표본 1,102개, 발화 0건).
    끄면 룰이 조용히 죽는데 **예외도 로그도 없다.**

    **세 경로를 다 본다.** 코드 기본값·`defaults.yaml`·실제 엔진 배선 중 하나만
    끊겨도 배포판에서 꺼진 채 나갈 수 있다. 2026-08-12 까지 코드 기본값은 실제로
    빈 dict 였다 — `_deep_merge` 덕에 동작은 했지만 그건 우연에 가까웠다.
    """
    from argus.config import loader
    from argus.config.loader import DetectionSettings

    # (1) 코드 기본값
    assert "gpu_temp_c" in DetectionSettings().load_gates, (
        "코드 기본값에 부하 게이트가 없다 — defaults.yaml 을 못 읽는 경로에서 꺼진다"
    )

    # (2) 사용자 설정이 전혀 없을 때의 실제 로드값
    monkeypatch.setattr(loader, "user_config_path", lambda: tmp_path / "없는설정.yaml")
    gates = loader.load_settings().detection.load_gates
    assert "gpu_temp_c" in gates, f"기본 설정에 부하 게이트가 없다: {gates}"
    assert gates["gpu_temp_c"].metric == "gpu_util_percent"

    # (3) 엔진까지 닿았는가. 값만 옮겨지고 판정에 안 쓰이면 위 둘은 통과한다.
    from argus.detection import rules as rules_module

    baselines = rules_module.build().baselines
    assert "gpu_temp_c" in baselines.load_gates, "엔진에 게이트가 실리지 않았다"

    # 부하(사용률 90)에서만 표본이 쌓이고 유휴(10)는 빠지는지 — 게이트가 살아 있다는 증거.
    # **기본 `load_min_samples`(60)를 넘겨야 축이 선다.** 그 아래면 판정을 보류하는
    # 것이 정상이라(탐지 규칙 4) 표본 부족과 게이트 고장이 구분되지 않는다.
    for i in range(70):
        baselines.observe(float(i * 6), {"gpu_temp_c": 93.0, "gpu_util_percent": 90.0})
        baselines.observe(float(i * 6 + 3), {"gpu_temp_c": 40.0, "gpu_util_percent": 10.0})
    under_load = baselines.stats_under_load("gpu_temp_c")
    assert under_load is not None, "부하 축이 서지 않았다"
    assert under_load.median == 93.0, (
        f"유휴 표본이 섞였다 — 게이트가 판정에 쓰이지 않는다: {under_load.median}"
    )


def test_warm_span_covers_load_window(monkeypatch):
    """워밍이 **실제로 읽는 구간**이 가장 긴 축을 덮는가.

    30분만 읽으면 부하 축은 매 기동 백지가 되고, 그 축을 쓰는 룰은 몇 시간 동안 판정
    불가가 된다. 이 PC 는 하루 3~4회 재시작하므로(대부분 unclean) 사실상 영구 공백이다.

    **`_needs_load_axis()` 만 확인하면 아무것도 검증하지 않는다** — 그 헬퍼가 참을
    돌려줘도 `warm_from_db` 가 그것을 쓰지 않으면 창은 그대로 30분이다. 처음 이 테스트를
    그렇게 썼고, mutation 에서 무력화해도 404개가 전부 통과했다.
    """
    from argus.eval import replay as replay_module

    seen: list[float] = []

    class _Stub:
        def __init__(self, db, **kw):
            pass

        def stream(self, window):
            seen.append(window.duration_s)
            return iter(())

    monkeypatch.setattr(replay_module, "Replayer", _Stub)

    _set(window_s=1800.0, load_window_s=21600.0).warm_from_db(object(), now=0.0)
    assert seen == [21600.0], f"부하 축 창(21600초)을 덮지 않았다: {seen}"

    seen.clear()
    _set(load_gates={}, window_s=1800.0).warm_from_db(object(), now=0.0)
    assert seen == [1800.0], f"게이트가 없으면 전역 창만 읽어야 한다: {seen}"


# --------------------------------------------------------------- 3. 발화 가능성

def _thermal_rule() -> Rule:
    """배포되는 룰과 같은 형태. 여기서 문턱을 손으로 만들면 룰 파일은 검증되지 않는다."""
    from argus.detection.rules import load_rules

    return next(r for r in load_rules() if r.name == "GPU 고온 지속")


def _engine() -> RuleEngine:
    return RuleEngine(
        [_thermal_rule()],
        window_s=100_000.0,
        min_samples=10,
        load_gates=GATE,
        load_window_s=100_000.0,
        load_min_interval_s=0.0,
        load_min_samples=10,
    )


def _obs(ts: float, temp: float, util: float, throttle: str = "SW_THERMAL") -> Observation:
    return Observation(
        ts=ts,
        metrics={
            "gpu_temp_c": temp,
            "gpu_util_percent": util,
            "gpu_throttle_reasons": throttle,
        },
    )


def _run(engine: RuleEngine, samples) -> list[float]:
    fired = []
    for ts, temp, util in samples:
        det = engine.observe(_obs(ts, temp, util))
        if det is not None:
            fired.append(ts)
    return fired


def _series(blocks) -> list[tuple[float, float, float]]:
    """`(온도 목록, 사용률, 개수)` 블록들을 1Hz 관측 목록으로."""
    out: list[tuple[float, float, float]] = []
    ts = 0.0
    for temps, util, n in blocks:
        for i in range(n):
            out.append((ts, temps[i % len(temps)], util))
            ts += 1.0
    return out


def test_healthy_hot_gpu_does_not_fire():
    """부하 시 평소가 93도인 기계는 울리지 않는다. 그게 이 기계의 정상이다.

    실측 근거: 13일간 부하 시 온도가 정확히 93.0도로 평평했고 전력은 +2.1% 였다.
    여기서 울리면 하루 여러 번 오탐이고, 오탐 3번이면 사용자는 알림을 끈다.
    """
    engine = _engine()
    samples = _series([
        (IDLE_TEMPS, 3.0, 400),
        (HOT_LOAD_TEMPS, 95.0, 1500),   # 부하 25분
    ])
    assert _run(engine, samples) == [], "정상 냉각인데 발화했다 — 오탐"


# 열화 시나리오: 평소 부하 65도로 돌던 기계가 91도로 올라 6분 이상 유지된다.
# 열화 구간이 부하 축의 다수가 되지 않도록 둔다 — 다수가 되면 그것이 곧 "평소"가 되고
# 어떤 롤링 베이스라인도 울리지 않는다(느린 열화는 `thermal_drift` 가 14일 기준으로 본다).
DEGRADED_SCENARIO = [
    (IDLE_TEMPS, 3.0, 600),
    (COOL_LOAD_TEMPS, 92.0, 900),
    (DEGRADED_TEMPS, 92.0, 400),
]


def test_degraded_cooling_fires():
    """평소 65도로 돌던 기계가 91도로 6분 이상 유지되면 울린다.

    **이것이 고침의 목적이다.** 아래 테스트가 같은 입력에서 구 조건이 울리지 못했음을
    함께 고정한다.
    """
    engine = _engine()
    fired = _run(engine, _series(DEGRADED_SCENARIO))
    assert fired, "냉각이 열화됐는데 울리지 않았다 — 미탐"


def test_old_sigma_condition_could_not_fire_on_same_input():
    """되돌림 방지 — **같은 입력**에서 구 조건은 울리지 못한다.

    이 테스트가 없으면 누군가 `load_median` 을 `median` 으로 되돌려도 "왜 안 되는지"가
    남지 않는다. 전제(σ 가 산포 때문에 부풀어 문턱이 상한을 넘는다)를 먼저 확인해,
    깨질 때 무엇이 틀렸는지 알 수 있게 한다.
    """
    old_rule = Rule(
        name="GPU 고온 지속(구)",
        conditions=[
            Condition(metric="gpu_temp_c", op=">", value="median + 3 * sigma"),
            Condition(metric="gpu_temp_c", op=">", value=90),
            Condition(metric="gpu_throttle_reasons", op="contains", value="THERMAL"),
        ],
        for_s=300.0,
        cooldown_s=3600.0,
    )
    engine = RuleEngine([old_rule], window_s=100_000.0, min_samples=10)
    samples = _series(DEGRADED_SCENARIO)
    fired = _run(engine, samples)

    stats = engine.baselines.stats("gpu_temp_c")
    assert stats.mad >= 5.0, (
        f"전제 확인 실패 — 유휴·부하가 섞인 창의 MAD 가 실측(6~8도)만큼 커야 한다: "
        f"{stats.mad:.2f}"
    )
    assert stats.threshold(3) > max(DEGRADED_TEMPS), (
        f"전제 확인 실패 — σ 문턱({stats.threshold(3):.1f})이 상한 아래다"
    )
    assert fired == [], "구 조건이 울렸다면 진단을 다시 봐야 한다"


def test_rule_file_actually_uses_load_axis():
    """배포되는 `rules.yaml` 이 부하 축을 참조하는가. 코드만 고치고 룰을 안 고치면
    전부 통과하면서 아무것도 바뀌지 않는다."""
    rule = _thermal_rule()
    exprs = [str(c.value) for c in rule.conditions]
    assert any("load_median" in e for e in exprs), f"룰이 부하 축을 안 쓴다: {exprs}"
    assert not any("sigma" in e for e in exprs), (
        f"상한 걸린 지표에 σ 기반 조건이 남아 있다: {exprs}"
    )


def test_unready_load_axis_blocks_instead_of_firing():
    """부하 축이 서기 전에는 판정하지 않는다 — 모르는 것을 근거로 알리지 않는다."""
    engine = _engine()
    engine.baselines.load_min_samples = 10_000     # 영원히 서지 않게
    samples = [(float(i), 45.0, 3.0) for i in range(200)]
    samples += [(200.0 + i, 96.0, 95.0) for i in range(900)]   # 96도 15분
    assert _run(engine, samples) == [], "부하 축이 없는데 발화했다"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

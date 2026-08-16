"""프로그램 조건부 베이스라인 (Phase 4-B).

"게임 중 CPU 60%"와 "브라우징 중 CPU 60%"는 다른 사건이다. 2026-08-04 실측에서
포어그라운드로 나누면 리소스 변동계수가 0.61 → 0.38 로 줄었다 — 같은 편차에 대해
z 가 약 1.6배가 된다.

**여기서 잡으려는 것은 조용히 깨지는 쪽이다.** 조건부 축이 끊겨도 전역으로 폴백하므로
예외가 나지 않는다. 즉 **기능이 통째로 죽어도 테스트가 전부 통과할 수 있다** —
그래서 학습/판정 경로가 같은 이름을 쓰는지, 폴백이 의도한 자리에서만 도는지를 따로 잰다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.detection.base import Observation, ProcessView  # noqa: E402
from argus.detection.baseline import BaselineSet  # noqa: E402


def _feed(bs: BaselineSet, program: str | None, value: float, n: int, start: float = 0.0,
          step: float = 10.0) -> float:
    ts = start
    for _ in range(n):
        bs.observe(ts, {"cpu_total": value}, program)
        ts += step
    return ts


def _set(**kw) -> BaselineSet:
    opts = dict(
        window_s=100_000.0, min_samples=10, per_program=True,
        program_metrics=["cpu_total"], program_window_s=100_000.0,
        program_min_interval_s=0.0, program_min_samples=10, max_programs=4,
    )
    opts.update(kw)
    return BaselineSet(**opts)


def test_program_baseline_differs_from_global():
    """핵심 동작 — 같은 값이 프로그램에 따라 다르게 평가된다."""
    bs = _set()
    ts = _feed(bs, "game.exe", 60.0, 30)
    _feed(bs, "chrome.exe", 10.0, 30, start=ts)

    game = bs.stats("cpu_total", "game.exe")
    chrome = bs.stats("cpu_total", "chrome.exe")
    assert game.median == 60.0
    assert chrome.median == 10.0

    # 전역은 둘을 섞어 그 중간 어딘가다 — 그래서 어느 쪽도 제대로 판정하지 못한다
    overall = bs.stats("cpu_total")
    assert overall.median != game.median or overall.median != chrome.median


def test_falls_back_to_global_when_foreground_is_unknown():
    """**포어그라운드를 아예 모르면 전역이다.**

    이건 "프로그램은 아는데 표본이 없는" 경우와 다르다. 무엇을 보고 있는지 모르면
    조건부 판정 자체가 성립하지 않으므로, 여기서 막으면 창이 없는 상태(전체화면
    게임·잠금화면 등) 전체가 탐지 공백이 된다.
    """
    bs = _set()
    _feed(bs, "game.exe", 60.0, 30)
    stats = bs.stats("cpu_total", None)
    assert stats is not None, "포어그라운드 미상에서 판정이 통째로 멈췄다"
    assert stats.samples == 30, "전역이 아니라 다른 것을 봤다"


def test_strict_holds_judgement_until_program_samples_are_enough():
    """**표본이 안 모인 프로그램에서는 판정하지 않는다** (2026-08-17 에 바뀐 결정).

    옛 동작은 전역으로 폴백하는 것이었고 이유가 있었다 — 새 게임을 깔 때마다
    창만큼 탐지 공백이 생긴다. 그런데 그 폴백이 오탐을 만들었다: 게임이 막 떴을
    때는 표본이 없고 **그 순간이 정확히 CPU 가 튀는 때**라, 유휴가 섞인 전역
    문턱으로 판정된다(`#188` 은 그렇게 전역 문턱을 +7.69%p 넘겨 발화했다).

    **탐지 공백이라는 비용은 사라지지 않았다.** 오탐이 미탐보다 비싸다는 판단으로
    맞바꾼 것이고, 되돌리기는 `per_program_strict: false` 한 줄이다.
    """
    bs = _set(program_min_samples=20)
    ts = _feed(bs, "chrome.exe", 10.0, 50)
    _feed(bs, "game.exe", 60.0, 5, start=ts)  # 5개뿐 — 아직 못 믿는다

    assert bs.stats("cpu_total", "game.exe") is None, \
        "표본이 모자란 프로그램에서 전역으로 물러났다 — 그게 #188 오탐의 경로다"

    _feed(bs, "game.exe", 60.0, 20, start=ts + 100)
    assert bs.stats("cpu_total", "game.exe").median == 60.0, "표본이 찼는데 판정하지 않았다"


def test_non_strict_keeps_the_old_fallback():
    """**되돌리기가 설정 한 줄이어야 한다.** 미탐이 늘면 여기로 되돌린다.

    기본값(True)이 아닌 값으로 잰다 — 같은 값으로 재면 배선이 끊겨도 참이다.
    """
    bs = _set(program_min_samples=20, program_strict=False)
    ts = _feed(bs, "chrome.exe", 10.0, 50)
    _feed(bs, "game.exe", 60.0, 5, start=ts)

    early = bs.stats("cpu_total", "game.exe")
    assert early is not None, "폴백을 켰는데 판정을 멈췄다"
    assert early.median != 60.0, "표본 5개짜리 프로그램 기준을 그대로 썼다"


def test_strict_does_not_touch_metrics_that_are_not_split():
    """**나누지 않는 메트릭은 막지 않는다.** 그쪽은 전역이 유일한 기준이다."""
    bs = _set(program_min_samples=20)
    for i in range(30):
        bs.observe(i * 10.0, {"cpu_total": 10.0, "disk_queue": 1.0}, "chrome.exe")
    # `disk_queue` 는 program_metrics 에 없다 — 전역이 나와야 한다.
    assert bs.stats("disk_queue", "never-seen.exe") is not None, \
        "프로그램별로 나누지도 않는 메트릭을 막았다"


def test_disabled_by_default_keeps_old_behaviour():
    """**기본은 꺼짐.** 켜는 것은 탐지 동작을 바꾸는 일이라 평가를 통과한 뒤다."""
    bs = BaselineSet(window_s=100_000.0, min_samples=10)
    assert bs.per_program is False
    ts = _feed(bs, "game.exe", 60.0, 30)
    _feed(bs, "chrome.exe", 10.0, 30, start=ts)
    assert bs.stats("cpu_total", "game.exe").median == bs.stats("cpu_total").median


def test_only_listed_metrics_are_split():
    """룰이 상대 조건으로 쓰지 않는 메트릭은 나누지 않는다 — 나눠 봐야 안 쓰이는데
    메모리는 프로그램 수만큼 곱해진다."""
    bs = _set(program_metrics=["cpu_total"])
    for i in range(30):
        bs.observe(i * 10.0, {"cpu_total": 60.0, "disk_queue": 5.0}, "game.exe")
    assert "cpu_total" in bs.program_readiness()["game.exe"]
    assert "disk_queue" not in bs.program_readiness()["game.exe"]


def test_program_count_is_capped_by_lru():
    """상한이 없으면 이름이 계속 바뀌는 환경에서 메모리가 무한히 는다."""
    bs = _set(max_programs=3)
    ts = 0.0
    for name in ("a", "b", "c", "d", "e"):
        ts = _feed(bs, name, 10.0, 15, start=ts)
    kept = set(bs.program_readiness())
    assert len(kept) == 3, f"상한 3인데 {len(kept)}개를 들고 있다"
    assert "e" in kept and "a" not in kept, "가장 오래된 것이 아니라 다른 것을 버렸다"


def test_min_interval_thins_samples():
    """솎아 담기. 이게 없으면 프로그램 16개 × 6메트릭이 예산을 먹는다."""
    bs = _set(program_min_interval_s=5.0)
    for i in range(100):
        bs.observe(float(i), {"cpu_total": 50.0}, "game.exe")  # 1초 간격
    kept = bs.program_readiness()["game.exe"]["cpu_total"]
    assert 15 <= kept <= 25, f"5초 간격이면 약 20개여야 하는데 {kept}개"


def test_global_keeps_every_sample_even_when_program_axis_is_on():
    """전역은 항상 그대로 채운다 — 폴백의 근거이자 조건부 축을 껐을 때의 동작이다."""
    bs = _set(program_min_interval_s=5.0)
    for i in range(100):
        bs.observe(float(i), {"cpu_total": 50.0}, "game.exe")
    assert bs.readiness()["cpu_total"] == 100, "전역까지 솎였다"


# ---------------------------------------------------------------- 배선


def test_observation_reads_foreground_program():
    obs = Observation(
        ts=1.0,
        processes=[
            ProcessView(pid=1, name="background.exe"),
            ProcessView(pid=2, name="Game.EXE", foreground=True),
        ],
    )
    assert obs.foreground_program == "game.exe", "대소문자가 갈리면 같은 프로그램이 둘로 세어진다"


def test_observation_without_foreground_is_none():
    obs = Observation(ts=1.0, processes=[ProcessView(pid=1, name="a.exe")])
    assert obs.foreground_program is None


def test_rule_engine_learns_and_judges_with_the_same_program():
    """**학습과 판정이 같은 이름을 써야 한다.**

    다르면 학습된 적 없는 이름으로 조회해 늘 전역으로 떨어진다 — 예외도 안 나고
    조건부 축이 있으나 마나가 된다. `flatten_gpus` 를 한 곳에 둔 이유와 같다.
    """
    from argus.detection.rules import RuleEngine

    engine = RuleEngine(rules=[], per_program=True, program_min_samples=5,
                        program_window_s=100_000.0, program_min_interval_s=0.0)
    engine.baselines.program_metrics = {"cpu_total"}

    for i in range(10):
        engine.learn(Observation(
            ts=float(i),
            metrics={"cpu_total": 60.0},
            processes=[ProcessView(pid=1, name="game.exe", foreground=True)],
        ))
    assert "game.exe" in engine.baselines.program_readiness(), "학습이 프로그램을 못 받았다"


def test_relative_metrics_are_taken_from_rules():
    """목록을 코드에 박지 않고 룰에서 뽑는다(규칙 3) — 룰이 바뀌면 따라가야 한다."""
    from argus.detection.rules import Condition, Rule, relative_metrics

    rules = [
        Rule(name="rel", conditions=[Condition(metric="cpu_total", op=">",
                                               value="median + 3 * sigma")]),
        Rule(name="abs", conditions=[Condition(metric="disk_queue", op=">", value=2)]),
    ]
    assert relative_metrics(rules) == {"cpu_total"}


def test_registry_builds_rules_from_config(monkeypatch):
    """레지스트리가 **클래스가 아니라 생성자**를 등록해야 config 가 전달된다.

    2026-08-04 까지 `RuleEngine` 클래스가 그대로 등록돼 있어 `detection.*` 설정이
    룰 엔진에 전달된 적이 없었다. 코드 기본값과 YAML 기본값이 우연히 같아서
    아무 신호도 없었다 — `baseline_window_s` 를 고쳐도 판정이 안 바뀌는 상태였다.

    **기본값으로 비교하면 이 테스트도 같은 함정에 빠진다.** 실제로 빠졌었다:
    배선을 끊는 mutation 에 330개가 전부 통과했다(2026-08-04). 두 기본값이 같으니
    당연하다. 그래서 **기본값이 아닌 값**을 넣고 그게 도착하는지 본다.
    """
    from argus.config import loader
    from argus.detection.registry import build

    tweaked = loader.load_settings().model_copy(deep=True)
    tweaked.detection.baseline_window_s = 4242.0   # 기본 1800
    tweaked.detection.min_samples = 37             # 기본 60
    tweaked.detection.max_programs = 7             # 기본 16
    tweaked.detection.per_program = True           # 기본 False
    monkeypatch.setattr(loader, "load_settings", lambda: tweaked)

    engine = build("rules")
    assert engine.baselines.window_s == 4242.0, "config 창 설정이 룰 엔진에 도착하지 않는다"
    assert engine.baselines.min_samples == 37
    assert engine.baselines.max_programs == 7
    assert engine.baselines.per_program is True


def test_defaults_yaml_declares_per_program_keys():
    import yaml

    raw = yaml.safe_load((ROOT / "argus" / "config" / "defaults.yaml").read_text(encoding="utf-8"))
    for key in ("per_program", "program_window_s", "program_min_interval_s",
                "program_min_samples", "max_programs"):
        assert key in raw["detection"], f"defaults.yaml 의 detection 에 {key} 가 없다"
    assert raw["detection"]["per_program"] is False, "기본이 켜져 있다 — 평가 전에 켜면 안 된다"


def test_per_program_strict_comes_from_config(monkeypatch):
    """**설정 배선을 따로 잰다.** 위 테스트들은 판정 로직만 본다.

    `defaults.yaml` 의 `detection.per_program_strict` 를 고쳤을 때 상주가 그 값을
    실제로 쓰는지는 `build()` 를 지나야만 알 수 있다. 2026-08-04 에 `detection.*`
    가 통째로 무시되던 버그가 이 자리에 테스트가 없어서 살아 있었다.

    **기본값(True)이 아닌 값으로 잰다** — 같은 값으로 재면 배선이 끊겨도 참이다.
    """
    from argus.detection.rules import build

    monkeypatch.setenv("ARGUS_DETECTION__PER_PROGRAM_STRICT", "false")
    assert build().baselines.program_strict is False, "설정이 배선되지 않았다"

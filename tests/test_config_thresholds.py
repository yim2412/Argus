"""판정 문턱이 **정말로** config 에서 온다.

규칙 3 은 "튜닝은 코드 수정 없이 YAML 만 고쳐서 되어야 한다"고 말한다. 값을 `defaults.yaml`
에 적어 두는 것만으로는 그것이 지켜지지 않는다 — **배선이 한 군데라도 끊기면 조용히 코드
기본값으로 돈다.** 예외도 로그도 없고, 사용자는 YAML 을 고쳤는데 아무 일도 일어나지 않는
상황을 겪는다. 그래서 여기서는 "값이 있다"가 아니라 **"바꾸면 판정이 달라진다"** 를 본다.

세 갈래를 각각 고정한다.

    classify()          문턱을 직접 받는 지점
    analyze_incident()  사건 재분석 — 채점과 `rescore_incidents.py` 가 쓴다
    defaults.yaml       파일의 값이 코드 기본값과 어긋나지 않는가
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from argus.config.loader import (
    BottleneckSettings,
    FingerprintSettings,
    IncidentSettings,
    LeakMetricSettings,
    ProcessLeakSettings,
    Settings,
    UsageSettings,
)
from argus.decide.fusion import FusionSettings, analyze_incident
from argus.detection.baseline import BaselineSet
from argus.detection.procleak import _Track, judge, rules_from_settings
from argus.explain.bottleneck import classify
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _baselines(**metrics: float) -> BaselineSet:
    baselines = BaselineSet(window_s=10_000.0, min_samples=10)
    for index in range(120):
        baselines.observe(float(index), dict(metrics))
    return baselines


def test_classify_honours_the_configured_cpu_threshold() -> None:
    """CPU 문턱을 낮추면 같은 지표가 CPU 병목이 된다.

    75% 는 기본값(70)을 넘고, 올린 값(90)에는 못 미친다. 두 방향을 다 보는 이유는
    한쪽만 보면 "무조건 CPU 라고 하는" 구현으로도 통과하기 때문이다.

    **평소값을 75 근처로 둔다.** CPU 에는 경로가 둘이라(`cpu >= cpu_high_percent`, 그리고
    `z 가 높고 cpu >= cpu_elevated_percent`) 평소가 낮으면 뒤쪽이 계속 CPU 를 만들어
    앞쪽 문턱을 올려도 판정이 안 바뀐다 — 처음 이 테스트를 쓸 때 실제로 그랬다.
    """
    baselines = _baselines(cpu_total=74.0, mem_percent=30.0, disk_resp_ms=0.1)
    peak = {"cpu_total": 75.0, "mem_percent": 30.0, "disk_resp_ms": 0.1}

    assert classify(peak, baselines).kind == "CPU", "기본값(70)이면 CPU 여야 한다"

    strict = classify(peak, baselines, settings=BottleneckSettings(cpu_high_percent=90.0))
    assert strict.kind != "CPU", (
        f"문턱을 90 으로 올렸는데 여전히 CPU 다 — 설정이 닿지 않았다: {strict.kind}"
    )


def test_classify_honours_the_configured_memory_tightness() -> None:
    """스왑이 근거가 되는 선(`mem_tight_percent`)도 config 에서 온다.

    18번 수정이 만든 조건이라 여기서 함께 고정한다 — 메모리 50% 는 기본값(60)에
    못 미쳐 스왑이 무시되지만, 문턱을 40 으로 낮추면 근거가 된다.
    """
    baselines = _baselines(cpu_total=20.0, mem_percent=50.0, swap_used_mb=400.0)
    peak = {"cpu_total": 20.0, "mem_percent": 50.0, "swap_used_mb": 800.0}

    assert classify(peak, baselines).kind != "MEMORY", "기본값(60)에서는 스왑이 근거가 아니다"

    loose = classify(peak, baselines, settings=BottleneckSettings(mem_tight_percent=40.0))
    assert loose.kind == "MEMORY", f"문턱을 낮췄는데 반영되지 않았다: {loose.kind}"


def test_incident_reanalysis_uses_the_given_thresholds(db: Database) -> None:
    """`analyze_incident()` 까지 문턱이 흘러간다.

    **이 경로가 실제로 끊겼던 자리다.** `classify` 만 고쳐 두면 단위 테스트는 통과하는데
    제품과 채점은 계속 기본값으로 돈다. 채점(`eval.attribution`)과
    `tools/rescore_incidents.py` 가 둘 다 여기를 지난다.
    """
    now = time.time()
    start = now - 600
    end = start + 120

    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
        # 평소도 74% 다 — z 경로가 아니라 절대 문턱만으로 판정되게 한다(위 테스트 주석).
        [(start - 1800 + i, 74.0, 80.0, 30.0, 0.1) for i in range(1500)]
        + [(start + i, 75.0, 80.0, 30.0, 0.1) for i in range(120)],
    )
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "rss_mb", "handles"),
        [(start + i, 20, "hog", 70.0, 100.0, 300) for i in range(0, 120, 2)],
    )
    db.insert_many(
        "incidents",
        ("id", "ts_start", "ts_end", "severity", "title"),
        [(1, start + 10, end, "warning", "분석 중")],
    )

    default = analyze_incident(db, 1, end)
    assert default is not None and default.bottleneck.kind == "CPU", (
        f"기본 문턱(70)에서 CPU 가 아니다: {default and default.bottleneck.kind}"
    )

    strict = analyze_incident(
        db, 1, end,
        FusionSettings(bottleneck=BottleneckSettings(cpu_high_percent=90.0)),
    )
    assert strict is not None and strict.bottleneck.kind != "CPU", (
        f"문턱을 90 으로 올렸는데 재분석이 그대로 CPU 다 — 설정이 닿지 않았다: "
        f"{strict and strict.bottleneck.kind}"
    )


def _compare_section(raw: dict, code_defaults: dict, where: str) -> None:
    """YAML 절 하나를 코드 기본값과 대조한다. 중첩 절도 같은 규칙으로 내려간다
    (`process_leak.handles` 처럼 지표별 문턱이 하위 절에 있다)."""
    for key, value in raw.items():
        assert key in code_defaults, (
            f"defaults.yaml 의 `{where}.{key}` 가 모델에 없다 — 오타면 조용히 무시된다"
        )
        if isinstance(value, dict):
            _compare_section(value, code_defaults[key], f"{where}.{key}")
            continue
        if isinstance(value, list) and isinstance(code_defaults[key], tuple):
            # YAML 은 목록, 모델은 튜플로 받는다. 순서까지 같아야 한다 —
            # 집합으로 비교하면 중복이 들어가도 통과한다.
            assert value == list(code_defaults[key]), (
                f"`{where}.{key}` 가 어긋난다: YAML {value!r} vs 코드 {code_defaults[key]!r}"
            )
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            assert value == pytest.approx(code_defaults[key]), (
                f"`{where}.{key}` 가 어긋난다: YAML {value} vs 코드 {code_defaults[key]}"
            )
        else:
            assert value == code_defaults[key], (
                f"`{where}.{key}` 가 어긋난다: YAML {value!r} vs 코드 {code_defaults[key]!r}"
            )
    missing = {k for k in code_defaults if k not in raw and code_defaults[k] is not None}
    assert not missing, f"`{where}` 에 YAML 로 노출되지 않은 문턱: {sorted(missing)}"


def test_defaults_yaml_matches_the_code_defaults() -> None:
    """`defaults.yaml` 의 값이 코드 기본값과 같은가.

    둘이 갈리면 **"한 개념에 두 값"이 다시 생긴다** — 디스크 응답 하한이 코드 5.0,
    `rules.yaml` 3 이었던 것이 정확히 그 상태였다(2026-08-02 통일). pydantic 은 모르는
    키를 조용히 무시하므로, YAML 쪽 오타도 여기서만 드러난다.

    **`process_leak`·`fingerprint` 는 2026-08-03 에 들어왔다.** mutation 스윕에서
    `process_leak.*.monotonic_ratio` 를 0 으로 만들어도 241개가 전부 통과했다 —
    누수 판정의 단조성 조건이 config 쪽으로는 검증된 적이 없었다.
    """
    raw = yaml.safe_load(
        (Path("argus/config/defaults.yaml")).read_text(encoding="utf-8")
    )

    sections = (
        ("bottleneck", BottleneckSettings),
        ("incident", IncidentSettings),
        ("process_leak", ProcessLeakSettings),
        ("fingerprint", FingerprintSettings),
        ("usage", UsageSettings),
    )
    for section, model in sections:
        assert section in raw, f"defaults.yaml 에 `{section}` 절이 없다"
        _compare_section(raw[section], model().model_dump(), section)


def test_disk_response_floor_is_one_value(tmp_path: Path) -> None:
    """디스크 응답 하한이 `rules.yaml` 과 `bottleneck` 에서 같은 값이다.

    한 개념에 두 값이면 룰만 먼저 울리고 분류는 IO 라 하지 않는 구간이 생긴다.
    사용자에게는 "디스크 경고가 떴는데 원인은 디스크가 아니라는 리포트"로 보인다.
    """
    rules = yaml.safe_load(Path("argus/config/rules.yaml").read_text(encoding="utf-8"))

    floors = []
    for rule in rules.get("rules", []):
        for clause in (rule.get("when") or {}).get("all", []):
            if clause.get("metric") == "disk_resp_ms" and isinstance(clause.get("value"), (int, float)):
                floors.append(float(clause["value"]))

    assert floors, "rules.yaml 에서 디스크 응답 절대 조건을 찾지 못했다"
    for floor in floors:
        assert floor == pytest.approx(BottleneckSettings().disk_resp_floor_ms), (
            f"rules.yaml 의 {floor} 와 bottleneck 의 "
            f"{BottleneckSettings().disk_resp_floor_ms} 가 다르다"
        )


def _sawtooth_track() -> _Track:
    """배수·증가량·지속·표본은 전부 충분하고 **단조성만** 위반하는 시계열.

    400 → 4000 이지만 매 틱 오르내린다. 일하는 중인 프로그램이 이렇게 보인다.
    """
    track = _Track()
    for i in range(600):
        track.samples.append((1000.0 + i, (400 + i * 6) * (1.2 if i % 2 else 0.8)))
    return track


def _handles_rule(settings: ProcessLeakSettings):
    rules = {rule.attr: rule for rule in rules_from_settings(settings)}
    assert "handles" in rules, f"handles 룰이 없다: {sorted(rules)}"
    return rules["handles"]


def test_process_leak_monotonic_ratio_comes_from_config() -> None:
    """누수 판정의 단조성 조건이 **config 에서** 온다.

    `test_procleak.py` 에도 톱니 케이스가 있지만 그쪽은 `MetricRule` 을 테스트 안에서
    직접 만든다 — 판정 로직은 보지만 **배선은 보지 않는다.** 그래서 2026-08-03 의
    mutation 스윕에서 `monotonic_ratio` 를 코드 기본값과 YAML 양쪽에서 0 으로 만들어도
    전부 통과했다. 튜닝이 조용히 무시되는 상태이고, 그게 규칙 3 이 막으려는 것이다.

    양방향으로 본다. 한쪽만 보면 "무조건 등락함이라고 하는" 구현으로도 통과한다.
    """
    track = _sawtooth_track()
    judge_kw = {"min_duration_s": 300.0, "min_samples": 20}

    strict = judge(track, _handles_rule(ProcessLeakSettings()), **judge_kw)
    assert not strict.leaking and "등락함" in strict.reason, (
        f"기본값(0.85)에서는 톱니를 걸러야 한다: {strict.reason}"
    )

    loose = judge(
        track,
        _handles_rule(ProcessLeakSettings(handles=LeakMetricSettings(monotonic_ratio=0.0))),
        **judge_kw,
    )
    assert loose.leaking, (
        f"단조성 조건을 0 으로 낮췄는데 판정이 그대로다 — 설정이 닿지 않았다: {loose.reason}"
    )


def test_process_leak_growth_and_delta_come_from_config() -> None:
    """같은 배선을 배수·증가량에서도 고정한다. 셋이 한 경로로 흐르므로 하나만
    보면 나머지가 끊겨도 모른다."""
    track = _Track()
    for i in range(600):  # 5000 → 6200: 단조 증가지만 1.24배 · 증가량 1,200
        track.samples.append((1000.0 + i, 5000.0 + i * 2))
    judge_kw = {"min_duration_s": 300.0, "min_samples": 20}

    default = judge(track, _handles_rule(ProcessLeakSettings()), **judge_kw)
    assert not default.leaking and "배수 부족" in default.reason, default.reason

    lenient = judge(
        track,
        _handles_rule(
            ProcessLeakSettings(handles=LeakMetricSettings(growth_ratio=1.1, min_delta=100.0))
        ),
        **judge_kw,
    )
    assert lenient.leaking, f"배수·증가량을 낮췄는데 반영되지 않았다: {lenient.reason}"

    strict = judge(
        track,
        _handles_rule(
            ProcessLeakSettings(handles=LeakMetricSettings(growth_ratio=1.1, min_delta=5000.0))
        ),
        **judge_kw,
    )
    assert not strict.leaking and "증가량 부족" in strict.reason, (
        f"증가량 문턱을 올렸는데 반영되지 않았다: {strict.reason}"
    )


def test_settings_exposes_both_sections() -> None:
    """루트 `Settings` 가 두 절을 들고 있다. 없으면 `load_settings()` 가 읽어도 버린다."""
    settings = Settings()
    assert isinstance(settings.bottleneck, BottleneckSettings)
    assert isinstance(settings.incident, IncidentSettings)

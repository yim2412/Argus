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

from argus.config.loader import BottleneckSettings, IncidentSettings, Settings
from argus.decide.fusion import FusionSettings, analyze_incident
from argus.detection.baseline import BaselineSet
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


def test_defaults_yaml_matches_the_code_defaults() -> None:
    """`defaults.yaml` 의 값이 코드 기본값과 같은가.

    둘이 갈리면 **"한 개념에 두 값"이 다시 생긴다** — 디스크 응답 하한이 코드 5.0,
    `rules.yaml` 3 이었던 것이 정확히 그 상태였다(2026-08-02 통일). pydantic 은 모르는
    키를 조용히 무시하므로, YAML 쪽 오타도 여기서만 드러난다.
    """
    raw = yaml.safe_load(
        (Path("argus/config/defaults.yaml")).read_text(encoding="utf-8")
    )

    for section, model in (("bottleneck", BottleneckSettings), ("incident", IncidentSettings)):
        assert section in raw, f"defaults.yaml 에 `{section}` 절이 없다"
        code_defaults = model().model_dump()
        for key, value in raw[section].items():
            assert key in code_defaults, (
                f"defaults.yaml 의 `{section}.{key}` 가 모델에 없다 — 오타면 조용히 무시된다"
            )
            assert value == pytest.approx(code_defaults[key]), (
                f"`{section}.{key}` 가 어긋난다: YAML {value} vs 코드 {code_defaults[key]}"
            )
        missing = set(code_defaults) - set(raw[section])
        assert not missing, f"`{section}` 에 YAML 로 노출되지 않은 문턱: {sorted(missing)}"


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


def test_settings_exposes_both_sections() -> None:
    """루트 `Settings` 가 두 절을 들고 있다. 없으면 `load_settings()` 가 읽어도 버린다."""
    settings = Settings()
    assert isinstance(settings.bottleneck, BottleneckSettings)
    assert isinstance(settings.incident, IncidentSettings)

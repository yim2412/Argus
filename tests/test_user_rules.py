"""사용자 룰 파일 — 약속대로 읽고, 틀렸으면 기본 룰로 돌며 드러낸다 (감사 F-001·003·004·021).

동봉 `rules.yaml` 머리말은 "사용자 룰은 `%APPDATA%\\Argus\\rules.yaml` 에 두면 이 파일을 대체한다"고
약속했는데 읽는 코드가 없었다(F-001). 읽게 되면 따라오는 것들:
- 파일 구조가 틀리면 AttributeError·ParserError 가 올라가 룰 탐지 전체가 꺼졌다(F-003).
- 없는 지표 이름은 오류 없이 로드되고 영원히 발화하지 않았다(F-004).
- 음수의 분수 거듭제곱이 복소수가 되어 그 틱의 룰 전체가 건너뛰어졌다(F-021).
"""

from __future__ import annotations

import time

import pytest

from argus.detection import expr
from argus.detection.base import Observation
from argus.detection.rules import RuleEngine, RuleError, load_rules

VALID = """version: 1
rules:
  - name: 사용자룰
    when: {all: [{metric: cpu_total, op: '>', value: 1}]}
"""


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_user_rules_file_replaces_the_bundled_one(data_dir):
    bundled = [r.name for r in RuleEngine().rules]
    assert len(bundled) > 1, "대조: 사용자 파일이 없으면 동봉 룰이다"
    (data_dir / "rules.yaml").write_text(VALID, encoding="utf-8")
    engine = RuleEngine()
    assert [r.name for r in engine.rules] == ["사용자룰"], "사용자 rules.yaml 이 무시됐다"
    assert engine.rules_note is None


@pytest.mark.parametrize("broken", [
    "rules: [\n  - name: 따옴표 안 닫힘 '\n",                                    # YAML 문법 오류
    "- 최상위가 목록이다\n",
    "rules: {이름: 매핑}\n",
    "rules:\n  - 룰이 문자열이다\n",
    "rules:\n  - name: x\n    when: 문자열\n",
    "rules:\n  - name: x\n    when: {all: [문자열]}\n",
    "rules:\n  - name: x\n    when: {all: [{metric: cpu_totl, op: '>', value: 1}]}\n",   # 없는 지표(F-004)
])
def test_broken_user_rules_fall_back_to_bundled_and_say_so(data_dir, broken):
    bundled = len(RuleEngine().rules)
    (data_dir / "rules.yaml").write_text(broken, encoding="utf-8")
    engine = RuleEngine()                                          # 예외가 밖으로 나오면 룰 탐지 전체가 꺼진다
    assert len(engine.rules) == bundled, "틀린 사용자 파일 때문에 룰이 사라졌다"
    assert engine.rules_note and "기본 룰" in engine.rules_note, "기본 룰로 도는 것을 드러내지 않는다"


def test_unknown_metric_is_rejected_at_load(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text("rules:\n  - name: 오타\n    when: {all: [{metric: cpu_totl, op: '>', value: 1}]}\n",
                    encoding="utf-8")
    with pytest.raises(RuleError, match="없는 지표: cpu_totl"):
        load_rules(path)
    path.write_text(VALID, encoding="utf-8")
    assert [r.name for r in load_rules(path)] == ["사용자룰"], "대조: 있는 지표는 통과한다"


def test_complex_result_is_an_expression_error_not_a_crash():
    with pytest.raises(expr.ExprError):
        expr.evaluate("x > (y ** 0.5)", {"x": 1.0, "y": -4.0})
    assert expr.evaluate("x > (y ** 0.5)", {"x": 1.0, "y": 4.0}) is False, "대조: 실수면 평소대로"


def test_one_bad_expression_does_not_skip_the_other_rules(tmp_path):
    """복소수 식 하나가 그 틱의 **다른 룰까지** 건너뛰게 하지 않는다."""
    path = tmp_path / "r.yaml"
    path.write_text(
        "rules:\n"
        "  - name: 이상한식\n    for: 0s\n    when: {all: [{metric: cpu_total, op: '>', value: '(0 - median) ** 0.5'}]}\n"
        "  - name: 멀쩡한룰\n    for: 0s\n    when: {all: [{metric: cpu_total, op: '>', value: 50}]}\n",
        encoding="utf-8")
    engine = RuleEngine(rules=load_rules(path), min_samples=10)
    t = time.time()
    for i in range(20):
        engine.observe(Observation(ts=t + i, metrics={"cpu_total": 10.0 + i % 3}))
    fired = None
    for i in range(3):
        fired = engine.observe(Observation(ts=t + 30 + i, metrics={"cpu_total": 90.0})) or fired
    assert fired is not None and "멀쩡한룰" in (fired.features.get("rules") or [fired.features.get("rule")])


def test_window_shows_the_rules_note(data_dir):
    from argus.desktop.app import _health_line
    from argus.detection.live import DetectionComponent
    from argus.storage.hot import Database

    (data_dir / "rules.yaml").write_text("- 최상위가 목록이다\n", encoding="utf-8")
    with Database(data_dir / "t.db") as db:
        comp = DetectionComponent(db, detector_name="rules")
        comp.setup()
        note = comp.health_note()
    assert note and "기본 룰" in note
    now = time.time()
    base = {"sample_ts": now - 1, "open": None, "last_end_ts": None, "unlabeled": 0}
    text, detail, _c, _id = _health_line({**base, "broken": [{"name": "detection", "status": "degraded", "note": note}]}, now)
    assert text == "설정 확인이 필요합니다" and "기본 룰" in detail

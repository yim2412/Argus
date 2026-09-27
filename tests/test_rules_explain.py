"""룰 알림 문장의 시간이 실제 지속 조건(`for`)과 같다 (감사 F-005).

「CPU 과부하」가 "45초 이상 지속"이라 말하는데 실제 발화 조건은 30초였다 — 주석("45초로 잡았더니
… 놓쳤다")대로 `for` 만 내리고 문장을 안 고쳤다. 사용자에게 사실과 다른 말을 하는 자리다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from argus.detection.rules import parse_duration

RULES = yaml.safe_load((Path(__file__).resolve().parent.parent / "argus/config/rules.yaml").read_text(encoding="utf-8"))["rules"]


def _said_seconds(explain: str) -> list[int]:
    return [int(n) * (60 if unit == "분" else 1) for n, unit in re.findall(r"(\d+)\s*(초|분)", explain)]


def test_parser_reads_minutes_and_seconds():
    """대조 — 문장에서 시간을 못 읽으면 아래 검사는 전부 통과해 아무것도 안 잰다."""
    assert _said_seconds("45초 이상 지속 · 5분째") == [45, 300]


@pytest.mark.parametrize("rule", RULES, ids=[r["name"] for r in RULES])
def test_explain_duration_matches_for(rule):
    said = _said_seconds(rule.get("explain", ""))
    for_s = parse_duration(rule.get("for", "30s"), field_name="for")
    assert not said or for_s in said, f"문장은 {said}초라 말하는데 for 는 {for_s:g}초다"

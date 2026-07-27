"""탐지기 이름 → 생성자.

스코어보드(`python -m argus.eval --detector <이름>`)와 실시간 실행이 **같은 표**를
보게 하려고 한 곳에 모았다. 두 곳에서 따로 만들면 "평가한 것"과 "실제로 도는 것"이
달라지고, 그러면 평가 수치가 거짓말이 된다.

기준선(`always`/`fixed_*`)은 지우지 않는다. 새 탐지기가 좋은지는 비교 대상이 있어야
말할 수 있고, 스코어보드에 계속 남아 있어야 회귀도 보인다.
"""

from __future__ import annotations

from typing import Callable

from .base import BaseDetector

# 이름 → 생성자. import 는 함수 안에서 한다 — 룰 파일 파싱 실패가 기준선 탐지기
# 로드까지 막으면 안 된다.
_BUILDERS: dict[str, Callable[[], BaseDetector]] = {}


def register(name: str, builder: Callable[[], BaseDetector]) -> None:
    _BUILDERS[name] = builder


def names() -> list[str]:
    _ensure_loaded()
    return sorted(_BUILDERS)


def build(name: str) -> BaseDetector:
    _ensure_loaded()
    if name not in _BUILDERS:
        raise KeyError(f"알 수 없는 탐지기: {name} (가능: {', '.join(sorted(_BUILDERS))})")
    return _BUILDERS[name]()


_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True

    from ..eval import baselines
    from .rules import RuleEngine

    for baseline_name, builder in baselines.REGISTRY.items():
        register(baseline_name, builder)
    register("rules", RuleEngine)

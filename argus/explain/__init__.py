"""귀인 — "왜 느렸는지"를 만드는 계층.

탐지는 "이상함"까지만 말한다. 그건 가치가 낮다. 제품은
"디스크 병목, 기여도 1위 chrome 68%, 40초 선행"이다.
"""

from .attribution import Contributor, attribute, process_trees
from .bottleneck import Bottleneck, classify
from .changepoint import find_onset
from .report import Incident, build_incident, render

__all__ = [
    "Contributor",
    "attribute",
    "process_trees",
    "Bottleneck",
    "classify",
    "find_onset",
    "Incident",
    "build_incident",
    "render",
]

"""신호를 사건으로 바꾸고, 무엇을 알릴지 정한다.

탐지기는 1초에도 여러 번 "이상하다"고 말한다. 그걸 그대로 보여 주면 사용자는 같은
일을 수십 번 읽게 되고, 그 순간 알림을 끈다. 여기서 하나로 접는다.
"""

from .budget import Decision, NotificationBudget
from .fusion import Fusion, FusionSettings, close_incident, open_incident
from .suppression import apply_suppression

__all__ = [
    "Decision",
    "Fusion",
    "FusionSettings",
    "NotificationBudget",
    "apply_suppression",
    "close_incident",
    "open_incident",
]

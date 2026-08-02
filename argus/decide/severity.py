"""등급을 **두 축**으로 매긴다 — 지금 겪는 손실과, 방치했을 때의 위험.

지금까지 등급은 **탐지기 종류**에 붙어 있었다. `procleak` 은 무조건 warning, GPU 룰은
`rules.yaml` 에 적힌 값. 그래서 누수 규모나 실제 성능 손실과 무관하게 결정됐고, 실사용
122건에서 역전이 나왔다 — `병목 없음 — compattelrunner 72%`(가치 없음)가 warning 이고
`발열 스로틀링 — GPU 91°C`(SM 클럭 1,918→1,715MHz)가 info 였다.

**축이 하나면 반드시 한쪽이 왜곡된다.** GPU 스로틀은 지금 클럭이 깎이는 *현재 손실*이고,
핸들 누수는 지금은 아무 증상이 없지만 방치하면 프로세스가 죽는 *미래 위험*이다. 둘은
다른 종류이고, 한 자에 억지로 놓으면 "증상 없는 누수"가 영원히 info 로 묻히거나
"정상 동작인 스로틀링"이 상시 warning 이 된다.

    등급 = max(현재 손실, 위험)

**후보 축 넷 중 둘은 실측에서 탈락했다** (2026-08-03, procleak 신호 22건).

    주입 python 핸들 : ratio 3.3~8.4배  · delta 2,700~3,700개   ← 진짜 결함
    medal   메모리   : ratio 6.5~27.8배 · delta 1,321~2,693MB   ← 게임 녹화, 정상

- **배수**: 가장 높은 배수(27.8)가 정상 프로세스다. 심각도와 무관하다.
- **절대 증가량**: 핸들 3,664개와 메모리 2,693MB 를 같은 자에 놓을 수 없다. 단위가 다르다.
- **지문 대비 위치**: 이 데이터에서 실제로 갈린다 — medal 핸들은 자기 p99 의 12%,
  주입 python 은 216%. 그래서 위험 축이 이걸 쓴다.
- **소진까지 남은 시간**: 사용자가 겪을 일에 가장 가깝지만 상한을 알아야 한다. 핸들의
  실질 상한은 시스템 전역이라 프로세스별로 말할 수 없어 뺐다.

**문턱은 config 에 있다**(`severity` 절). 이 모듈에 숫자를 박으면 규칙 3 위반이고,
튜닝하려면 코드를 고쳐야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.loader import SeveritySettings
from ..detection.base import SEVERITY_CRITICAL, SEVERITY_INFO, SEVERITY_ORDER, SEVERITY_WARNING

# 축 이름. 등급을 정한 쪽을 사건에 남겨, 나중에 "왜 warning 인가"를 답할 수 있게 한다.
AXIS_IMPACT = "impact"
AXIS_RISK = "risk"
AXIS_NONE = "none"


@dataclass(frozen=True)
class Grade:
    """등급과 그 근거."""

    severity: str
    axis: str
    """등급을 정한 축. 둘이 같으면 `impact` 를 적는다 (현재 손실이 더 설명하기 쉽다)."""
    reason: str
    """사람이 읽는 한 줄. 대시보드와 리포트가 그대로 쓴다."""

    impact: str = SEVERITY_INFO
    risk: str = SEVERITY_INFO


def _worse(a: str, b: str) -> str:
    return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b


def _band(value: float, warning_at: float, critical_at: float) -> str:
    """값을 등급 셋 중 하나로. 문턱은 호출자가 config 에서 가져와 넘긴다."""
    if value >= critical_at:
        return SEVERITY_CRITICAL
    if value >= warning_at:
        return SEVERITY_WARNING
    return SEVERITY_INFO


# --------------------------------------------------------------------- 위험 축
def leak_risk(
    *,
    last: float,
    fingerprint_p99: float | None,
    monotonic: float,
    settings: SeveritySettings,
) -> tuple[str, str]:
    """누수 신호의 위험도. **"그 프로그램의 평소와 비교해 어디쯤인가."**

    지문이 없으면(신규·희귀 프로세스) 판단할 근거가 없다. 그때는 `unknown_leak` 로
    물러선다 — 억제는 한쪽으로만 작동한다는 6-B 의 원칙과 같다. 모르는 것을 조용히
    info 로 내리면 진짜 누수가 묻히고, 조용히 critical 로 올리면 신규 프로세스마다 운다.

    단조성을 함께 보는 이유: 자기 p99 를 넘었더라도 등락하는 중이면 일하는 것에 가깝다.
    `procleak` 이 이미 발화 조건으로 쓰지만, 여기서는 **등급을 가르는 데** 다시 쓴다.
    """
    if fingerprint_p99 is None or fingerprint_p99 <= 0:
        return settings.unknown_leak, "이 프로그램의 평소를 아직 모른다 (지문 없음)"

    position = last / fingerprint_p99
    severity = _band(position, settings.risk_warning_ratio, settings.risk_critical_ratio)

    # 등락하는 중이면 한 단계 내린다. 계단식으로만 오르는 것이 누수의 모양이다.
    if monotonic < settings.risk_monotonic_floor and severity != SEVERITY_INFO:
        index = SEVERITY_ORDER.get(severity, 0)
        severity = [SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL][max(0, index - 1)]
        return severity, (
            f"평소 상한의 {position * 100:.0f}% 이지만 등락한다 (비감소 {monotonic:.2f})"
        )

    return severity, f"평소 상한(p99)의 {position * 100:.0f}%"


# ------------------------------------------------------------------- 현재 손실 축
def clock_loss_impact(
    *, observed_mhz: float, baseline_mhz: float | None, settings: SeveritySettings
) -> tuple[str, str]:
    """클럭이 평소보다 얼마나 깎였나. **같은 부하 구간끼리만 비교해야 뜻이 있다** —
    유휴를 섞으면 "요즘 게임을 많이 했다"가 "손실이 크다"로 둔갑한다
    (`detection/thermal.py` 가 세운 패턴).
    """
    if not baseline_mhz or baseline_mhz <= 0 or observed_mhz <= 0:
        return SEVERITY_INFO, ""

    loss = 1.0 - (observed_mhz / baseline_mhz)
    if loss <= 0:
        return SEVERITY_INFO, ""
    severity = _band(loss, settings.impact_warning_loss, settings.impact_critical_loss)
    return severity, f"클럭 {baseline_mhz:.0f} → {observed_mhz:.0f}MHz ({loss * 100:.0f}% 손실)"


def combine(
    impact: tuple[str, str], risk: tuple[str, str], *, fallback: str = SEVERITY_INFO
) -> Grade:
    """두 축을 접는다. 등급은 높은 쪽, 근거는 **등급을 정한 축의 것**을 쓴다.

    근거를 둘 다 붙이지 않는 이유: 사용자가 읽는 것은 "왜 이 등급인가"이고, 등급을
    정하지 않은 축의 문장은 그 질문에 답하지 않는다. 둘 다 필요하면 `Grade` 에 남아 있다.
    """
    impact_severity, impact_reason = impact
    risk_severity, risk_reason = risk
    severity = _worse(_worse(impact_severity, risk_severity), fallback)

    if severity == impact_severity and impact_reason:
        axis, reason = AXIS_IMPACT, impact_reason
    elif severity == risk_severity and risk_reason:
        axis, reason = AXIS_RISK, risk_reason
    else:
        axis, reason = AXIS_NONE, ""

    return Grade(
        severity=severity,
        axis=axis,
        reason=reason,
        impact=impact_severity,
        risk=risk_severity,
    )

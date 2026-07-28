"""리포트 — 사람이 읽는 결과물.

**이 파일이 제품이다.** 앞의 모듈들은 전부 여기 한 문단을 만들기 위해 있다.
"이상 감지"는 알림을 끄게 만들고, "디스크 병목, 기여도 1위 chrome 68%, 40초 선행"은
행동을 만든다.

숫자를 그대로 늘어놓지 않는다. 사용자가 알아야 하는 것은 순서로 셋이다.
  1. 무엇이 아팠나 (증상 — 체감으로 말한다)
  2. 누구 때문인가 (기여도 — 순위와 몫)
  3. 왜 그렇게 판단했나 (근거 — 선행성, 평소 대비)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .attribution import Contributor
from .bottleneck import Bottleneck, label_for

# 자원별 표시 단위. 내부는 전부 원단위(%, MB, B/s)로 다루고 여기서만 사람 단위로 바꾼다.
_UNITS = {
    "cpu": ("%", 1.0),
    "rss": ("MB", 1.0),
    "io_read": ("MB/s", 1 / 1048576),
    "io_write": ("MB/s", 1 / 1048576),
    "handles": ("개", 1.0),
}

# 이보다 기여가 작은 후보는 선행성을 표시하지 않는다.
# 상승폭이 작으면 "오르기 시작한 시점"의 판정 기준이 그 프로세스의 자연 변동 범위
# 안으로 들어가, 아무 의미 없는 값이 큰 숫자로 나온다(실측: 기여도 5% 짜리가
# "255초 선행"). 용의자가 아닌 것의 선행성은 정보가 아니라 잡음이다.
_LEAD_MIN_SHARE = 0.10

# 프로세스 저장 해상도(초). 활성 집합에 들기 전에는 이 간격으로만 기록되므로
# 상승 개시 시점을 이보다 정밀하게 알 수 없다. 이 안의 차이는 "동시"로 본다.
_LEAD_RESOLUTION_S = 30.0


@dataclass
class Incident:
    """설명이 붙은 하나의 사건."""

    ts_start: float
    ts_end: float
    bottleneck: Bottleneck
    contributors: list[Contributor] = field(default_factory=list)
    symptom: str = ""
    onset_lead_s: float | None = None
    regime: str | None = None
    triggers: list[str] = field(default_factory=list)
    """이 사건을 연 룰 이름들.

    방아쇠와 설명이 다른 말을 하는 일이 실제로 있었다 — GPU 온도 룰이 울렸는데
    제목은 "CPU 병목 — op.gg 22%" 였다. 둘 다 사실이지만 인과가 이어지지 않는다.
    무엇이 울렸는지 적어 두면 그 어긋남이 눈에 보인다.
    """

    @property
    def duration_s(self) -> float:
        return self.ts_end - self.ts_start

    @property
    def prime_suspect(self) -> Contributor | None:
        return self.contributors[0] if self.contributors else None


def build_incident(
    ts_start: float,
    ts_end: float,
    bottleneck: Bottleneck,
    contributors: list[Contributor],
    *,
    symptom: str = "",
    onset_lead_s: float | None = None,
    regime: str | None = None,
    triggers: list[str] | None = None,
) -> Incident:
    return Incident(
        ts_start=ts_start,
        ts_end=ts_end,
        bottleneck=bottleneck,
        contributors=contributors,
        symptom=symptom,
        onset_lead_s=onset_lead_s,
        regime=regime,
        triggers=list(triggers or []),
    )


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}초"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}분 {rest}초" if rest else f"{minutes}분"


def render(incident: Incident, resource: str | None = None) -> str:
    """사람이 읽는 리포트(Markdown)."""
    resource = resource or incident.bottleneck.resource
    unit, scale = _UNITS.get(resource, ("", 1.0))

    start = datetime.fromtimestamp(incident.ts_start)
    end = datetime.fromtimestamp(incident.ts_end)

    lines = [
        f"**{incident.bottleneck.label}** — {start:%H:%M:%S} ~ {end:%H:%M:%S} "
        f"({_fmt_duration(incident.duration_s)})",
    ]

    if incident.symptom:
        lines.append(f"체감 영향: {incident.symptom}")
    if incident.bottleneck.evidence:
        lines.append("근거: " + " · ".join(incident.bottleneck.evidence))
    if incident.triggers:
        lines.append("발화한 룰: " + " · ".join(incident.triggers))
    if incident.bottleneck.overridden_from:
        # 방아쇠와 다른 답을 낼 수는 있지만 말없이 그러면 안 된다. 사용자는 자기가 받은
        # 알림이 왜 다른 이야기를 하는지 알 권리가 있다.
        origin = label_for(incident.bottleneck.overridden_from)
        lines.append(
            f"참고: 이 사건을 연 것은 {origin} 신호였지만, 구간의 지표는 "
            f"{incident.bottleneck.label}이 더 뚜렷했다"
        )

    if incident.contributors:
        lines.append("")
        if incident.bottleneck.attributable:
            lines.append("원인 후보:")
        else:
            # 자원이 다르면 순위는 답이 아니라 정황이다. 제목·1위를 원인처럼 읽지
            # 않도록 여기서 분명히 끊는다.
            lines.append(
                f"참고 — CPU 사용 상위 (이 병목의 원인 프로세스는 특정할 수 없습니다: "
                f"{incident.bottleneck.label}은 프로세스별 사용량을 얻을 수 없습니다)"
            )
        for rank, contributor in enumerate(incident.contributors[:5], start=1):
            share = contributor.share * 100
            delta = contributor.delta * scale
            after = contributor.after * scale
            note = []
            if contributor.is_new:
                note.append("이상 구간에 새로 시작됨")
            lead = contributor.lead_s
            if lead is not None and contributor.share >= _LEAD_MIN_SHARE:
                if abs(lead) <= _LEAD_RESOLUTION_S:
                    note.append("거의 동시")
                elif lead > 0:
                    note.append(f"{lead:.0f}초 선행")
                else:
                    note.append(f"{-lead:.0f}초 후행")
            suffix = f"  ← {', '.join(note)}" if note else ""
            lines.append(
                f"  {rank}. {contributor.label():<34} 기여도 {share:4.0f}%"
                f"  ({after:.1f}{unit}, +{delta:.1f}{unit}){suffix}"
            )

    if incident.onset_lead_s:
        lines.append("")
        lines.append(
            f"탐지는 실제 시작보다 {incident.onset_lead_s:.0f}초 늦었다 "
            "(지속 조건과 베이스라인 창 때문. 기여도는 실제 시작 시각 기준으로 계산했다)"
        )
    if incident.regime:
        lines.append(f"레짐: {incident.regime}")

    return "\n".join(lines)


def render_plain(incident: Incident, resource: str | None = None) -> str:
    """터미널용. Markdown 강조만 걷어낸다."""
    return render(incident, resource).replace("**", "")

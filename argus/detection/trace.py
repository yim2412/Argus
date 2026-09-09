"""룰이 **왜 발화하지 못했는지**를 모은다. 진단 전용 — 판정에 관여하지 않는다.

`observe()` 는 발화할 때만 판정을 돌려주므로, "안 잡혔다"만 남고 그 이유는 아무 데도
남지 않는다. 2026-09-08 에 "부하는 평소와 같은데 신호 0건"을 조사하려 했을 때, 원본을
되살려 재생해도 **여전히 0건이라는 사실만 다시 확인할 뿐**이었다.

**조건식의 수치 여유(근접도)를 재지 않는다.** 룰 표현식은 임의의 AST 라 "얼마나
모자랐나"를 일반적으로 계산하려면 평가기를 통째로 뜯어야 하고, 그렇게 얻은 숫자는
조건이 여러 개(`all`/`any`)일 때 뜻이 흐려진다. 대신 **판정이 어느 관문에서 멈췄는지**를
남긴다 — 그건 코드가 이미 알고 있고, 답으로서 더 정확하다.

가장 쓸모 있는 것은 `holding` 이다. "조건은 참이었는데 최장 22초만 버텼다(필요 30초)"는
문턱을 어디로 옮겨야 하는지를 바로 말해 준다. 반면 `unknown` 이 많으면 그건 탐지 문제가
아니라 **지표가 안 들어온 것**이고, 그 둘을 섞으면 엉뚱한 곳을 고치게 된다.

**상주에서는 꺼져 있다**(`RuleEngine.trace is None`). 관측자는 가벼워야 한다(설계 규칙 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 판정이 멈출 수 있는 관문. 순서가 곧 파이프라인 순서다.
OUTCOMES = ("unready", "unknown", "false", "holding", "cooldown", "fired")

#: 사람이 읽는 이름. 로그가 아니라 사람이 보는 표에 들어간다.
LABELS = {
    "unready": "베이스라인 안 섬",
    "unknown": "판정 불가(지표 없음)",
    "false": "조건 거짓",
    "holding": "참인데 지속 미달",
    "cooldown": "쿨다운에 막힘",
    "fired": "발화",
}


@dataclass
class RuleStat:
    """룰 하나의 집계."""

    name: str
    counts: dict[str, int] = field(default_factory=lambda: {k: 0 for k in OUTCOMES})
    #: 조건이 참으로 이어진 **최장** 시간(초). 발화 문턱(`for_s`)과 비교하는 값이다.
    longest_hold_s: float = 0.0
    #: 그 룰의 지속 문턱. 룰마다 다르므로 함께 들고 있어야 비교가 된다.
    need_s: float = 0.0

    @property
    def ticks(self) -> int:
        return sum(self.counts.values())

    @property
    def verdict(self) -> str:
        """이 룰에 대해 할 수 있는 한 문장."""
        if self.counts["fired"]:
            blocked = self.counts["cooldown"]
            # **쿨다운 횟수를 함께 말한다.** 발화 1건만 보이면 "한 번 걸렸다"로 읽히는데,
            # 실제로는 조건이 계속 참이라 억제가 수십 번 일했을 수 있다. 그 둘은
            # 알림량을 조정할 때 완전히 다른 상황이다.
            if blocked:
                return f"발화 {self.counts['fired']}건 (이어진 {blocked}틱은 쿨다운이 막았다)"
            return f"발화 {self.counts['fired']}건"
        if self.counts["cooldown"]:
            return f"지속까지 갔지만 쿨다운에 {self.counts['cooldown']}번 막혔다"
        if self.longest_hold_s > 0:
            short = self.need_s - self.longest_hold_s
            return (
                f"조건은 참이었지만 최장 {self.longest_hold_s:.0f}초 "
                f"(필요 {self.need_s:.0f}초 — {short:.0f}초 모자랐다)"
            )
        if self.counts["false"] == 0 and self.counts["unknown"]:
            return "한 번도 판정하지 못했다 — 이 룰이 보는 지표가 안 들어왔다"
        if self.counts["unready"] == self.ticks and self.ticks:
            return "베이스라인이 끝까지 서지 않았다"
        return "조건이 한 번도 참이 되지 않았다"


class RuleTrace:
    """룰별 결과를 센다. **판정을 바꾸지 않는다** — 기록만 한다."""

    def __init__(self) -> None:
        self.stats: dict[str, RuleStat] = {}

    def record(
        self,
        rule: str,
        outcome: str,
        *,
        held_s: float = 0.0,
        need_s: float = 0.0,
    ) -> None:
        stat = self.stats.get(rule)
        if stat is None:
            stat = self.stats[rule] = RuleStat(rule)
        stat.counts[outcome] = stat.counts.get(outcome, 0) + 1
        if need_s:
            stat.need_s = need_s
        if held_s > stat.longest_hold_s:
            stat.longest_hold_s = held_s

    def ordered(self) -> list[RuleStat]:
        """가까이 갔던 룰부터. **아무 일도 없던 룰을 먼저 보여주면 표가 쓸모없어진다.**"""
        return sorted(
            self.stats.values(),
            key=lambda s: (
                s.counts["fired"],
                s.counts["cooldown"],
                s.longest_hold_s,
                -s.counts["unknown"],
            ),
            reverse=True,
        )

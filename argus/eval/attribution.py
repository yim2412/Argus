"""귀인 채점 — 원인 프로세스를 1순위로 지목했는가.

Phase 8 DoD 는 "결함 주입 시 원인 프로세스를 1순위로 지목하는 비율 85% 이상"이다.

**정답을 프로세스 트리로 확장한다.** `fault_injections.pid` 는 주입기 부모 하나뿐인데
실제 부하는 자식들이 낸다 — CPU 스핀은 GIL 때문에 프로세스로 fork 하므로, 부모는
CPU 를 0.1% 쓰고 자식 8개가 각 4.2% 를 쓴다. 부모만 정답으로 두면 **어떤 귀인 엔진도
통과할 수 없다.** 실측에서 정답 PID 는 5분 구간에 12행만 관측됐고 그나마 CPU 는 0 이었다.

채점에서 빼는 구간이 둘 있고, 둘 다 빼는 것이 옳다.
- `completed=0`: 주입은 했으나 **증상이 관측되지 않은** 구간(이 NVMe 의 `disk_thrash`).
  증상 없는 구간을 맞히라는 요구는 오탐을 요구하는 것과 같다.
- 프로세스 메트릭이 없는 구간: 그 시각에 수집이 죽어 있었다. 데이터가 없는 것을
  못 맞혔다고 세면 탐지기가 아니라 수집기를 채점하는 셈이다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..explain.attribution import attribute, descendants
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Verdict:
    """한 주입 구간의 채점 결과."""

    fault_id: int
    scenario: str
    resource: str
    answer_pids: set[int]
    ranked: list[tuple[str, float, set[int]]]
    """(이름, 기여도, PID 집합) 상위 순."""
    skipped: str | None = None

    @property
    def hit_rank(self) -> int | None:
        """정답이 몇 위였나. 없으면 None."""
        for rank, (_, _, pids) in enumerate(self.ranked, start=1):
            if pids & self.answer_pids:
                return rank
        return None

    @property
    def is_top1(self) -> bool:
        return self.hit_rank == 1


# 시나리오별로 어떤 자원을 봐야 하는가. 주입기가 만드는 부하의 종류다.
SCENARIO_RESOURCE = {
    "cpu_spin": "cpu",
    "memory_leak": "rss",
    "disk_thrash": "io_write",
    "handle_leak": "handles",
}

# **`manual` 은 귀인 채점 대상이 아니다.**
# 이 시나리오는 부하를 만들지 않는다 — 사용자가 실제로 하는 일(게임·빌드)에 라벨만
# 붙인다. 주입기 프로세스 자신은 아무 일도 하지 않으므로(실측: 25개 시각 중 1행,
# CPU 0) "주입 PID 를 지목하라"는 요구 자체가 성립하지 않는다. 원인은 게임이지
# 라벨을 붙인 도구가 아니다. 레짐 학습(Phase 4-B)용 라벨로만 쓴다.
NOT_ATTRIBUTABLE = frozenset({"manual"})


def score_fault(db, fault: dict, *, margin_s: float = 30.0, limit: int = 8) -> Verdict:
    """주입 구간 하나를 채점한다.

    비교 창은 주입 **직전**과 주입 **중**이다. 직전 창을 주입 시작에 딱 붙이지 않고
    `margin_s` 만큼 떼는 이유: 주입기가 뜨는 동안(프로세스 fork, 파일 준비) 이미
    자원을 쓰기 시작해서, 붙여 두면 그 상승이 '평소'에 섞여 델타가 줄어든다.
    """
    scenario = fault["scenario"]
    resource = SCENARIO_RESOURCE.get(scenario, "cpu")
    ts_start, ts_end = float(fault["ts_start"]), float(fault["ts_end"] or 0)

    verdict = Verdict(
        fault_id=int(fault["id"]),
        scenario=scenario,
        resource=resource,
        answer_pids=set(),
        ranked=[],
    )

    if scenario in NOT_ATTRIBUTABLE:
        verdict.skipped = "부하를 만들지 않는 라벨 (귀인 대상 아님)"
        return verdict
    if not ts_end:
        verdict.skipped = "주입이 완료되지 않음"
        return verdict
    if not fault["completed"]:
        verdict.skipped = "증상 미관측 (채점 제외)"
        return verdict
    if not fault["pid"]:
        verdict.skipped = "정답 PID 없음"
        return verdict

    verdict.answer_pids = descendants(db, int(fault["pid"]), ts_start, ts_end)

    rows = db.query(
        "SELECT COUNT(*) AS n FROM process_metrics WHERE ts >= ? AND ts <= ?",
        (ts_start, ts_end),
    )
    if not rows or not rows[0]["n"]:
        verdict.skipped = "구간에 프로세스 메트릭이 없음 (수집 중단)"
        return verdict

    before = (ts_start - margin_s - 120.0, ts_start - margin_s)
    after = (ts_start, ts_end)
    contributors = attribute(db, resource, before=before, after=after, limit=limit)
    verdict.ranked = [(c.name, c.share, set(c.pids)) for c in contributors]
    return verdict


def score_all(db, *, scenarios: list[str] | None = None, hours: float | None = None) -> list[Verdict]:
    sql = "SELECT * FROM fault_injections"
    params: list[object] = []
    if hours:
        import time

        sql += " WHERE ts_start > ?"
        params.append(time.time() - hours * 3600)
    sql += " ORDER BY id"
    faults = db.query(sql, params)
    if scenarios:
        faults = [f for f in faults if f["scenario"] in scenarios]
    return [score_fault(db, dict(f)) for f in faults]


def report(verdicts: list[Verdict]) -> str:
    scored = [v for v in verdicts if not v.skipped]
    skipped = [v for v in verdicts if v.skipped]

    lines = [
        "",
        "귀인 채점 — 원인 프로세스를 1순위로 지목했는가",
        "=" * 78,
        f"{'id':>3}  {'시나리오':<13} {'자원':<9} {'정답순위':>8}  지목 1위",
        "-" * 78,
    ]
    for v in scored:
        rank = v.hit_rank
        rank_text = f"{rank}위" if rank else "미지목"
        top = v.ranked[0] if v.ranked else None
        top_text = f"{top[0]} ({top[1]*100:.0f}%)" if top else "—"
        mark = "✅" if v.is_top1 else ("△" if rank else "❌")
        lines.append(
            f"{v.fault_id:>3}  {v.scenario:<13} {v.resource:<9} {rank_text:>8} {mark}  {top_text}"
        )

    if scored:
        top1 = sum(1 for v in scored if v.is_top1)
        top3 = sum(1 for v in scored if v.hit_rank and v.hit_rank <= 3)
        lines += [
            "-" * 78,
            f"1순위 지목률 : {top1}/{len(scored)} = {top1 / len(scored) * 100:.1f}%  (DoD 85%)",
            f"3위 이내     : {top3}/{len(scored)} = {top3 / len(scored) * 100:.1f}%",
        ]
    else:
        lines.append("채점 가능한 구간이 없다.")

    if skipped:
        lines += ["", f"제외 {len(skipped)}건:"]
        for v in skipped:
            lines.append(f"  #{v.fault_id:<3} {v.scenario:<13} {v.skipped}")

    return "\n".join(lines)

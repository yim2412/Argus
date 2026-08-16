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
    incident_id: int | None = None
    """제품 경로 채점일 때 근거가 된 사건. 함수 경로 채점에서는 None."""

    title: str = ""
    """제품 경로 채점일 때 사용자가 보게 되는 제목."""

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
#
# **여기 없는 시나리오는 채점하지 않는다**(예전에는 `cpu` 로 떨어졌다). 2026-08-16 에
# `memory_leak_spread` 를 추가하면서 이 표를 안 고쳤는데, 기본값이 있으니 아무 경고
# 없이 **메모리 누수를 CPU 기준으로 채점**할 뻔했다. 조용히 틀린 점수를 내느니
# 채점을 건너뛰고 이유를 남기는 편이 낫다 — `tests/test_eval_attribution.py` 가
# 주입기의 `SCENARIOS` 와 이 표를 대조해 누락을 기계적으로 잡는다.
SCENARIO_RESOURCE = {
    "cpu_spin": "cpu",
    "memory_leak": "rss",
    "memory_leak_spread": "rss",
    "disk_thrash": "io_write",
    "handle_leak": "handles",
}

# **`manual` 은 귀인 채점 대상이 아니다.**
# 이 시나리오는 부하를 만들지 않는다 — 사용자가 실제로 하는 일(게임·빌드)에 라벨만
# 붙인다. 주입기 프로세스 자신은 아무 일도 하지 않으므로(실측: 25개 시각 중 1행,
# CPU 0) "주입 PID 를 지목하라"는 요구 자체가 성립하지 않는다. 원인은 게임이지
# 라벨을 붙인 도구가 아니다. 레짐 학습(Phase 4-B)용 라벨로만 쓴다.
NOT_ATTRIBUTABLE = frozenset({"manual"})


def _has_global_metrics(db, ts_start: float, ts_end: float) -> bool:
    """구간에 병목 분류가 읽을 전역 지표가 남아 있는가."""
    rows = db.query(
        "SELECT COUNT(*) AS n FROM metrics_raw WHERE ts >= ? AND ts <= ?",
        (ts_start, ts_end),
    )
    return bool(rows and rows[0]["n"])


def score_fault(db, fault: dict, *, margin_s: float = 30.0, limit: int = 8) -> Verdict:
    """주입 구간 하나를 채점한다.

    비교 창은 주입 **직전**과 주입 **중**이다. 직전 창을 주입 시작에 딱 붙이지 않고
    `margin_s` 만큼 떼는 이유: 주입기가 뜨는 동안(프로세스 fork, 파일 준비) 이미
    자원을 쓰기 시작해서, 붙여 두면 그 상승이 '평소'에 섞여 델타가 줄어든다.
    """
    scenario = fault["scenario"]
    resource = SCENARIO_RESOURCE.get(scenario)
    ts_start, ts_end = float(fault["ts_start"]), float(fault["ts_end"] or 0)

    verdict = Verdict(
        fault_id=int(fault["id"]),
        scenario=scenario,
        resource=resource or "?",
        answer_pids=set(),
        ranked=[],
    )

    if scenario in NOT_ATTRIBUTABLE:
        verdict.skipped = "부하를 만들지 않는 라벨 (귀인 대상 아님)"
        return verdict
    if resource is None:
        # 기본값으로 때우지 않는다 — 위 주석 참조.
        verdict.skipped = f"채점할 자원을 모르는 시나리오 ({scenario}) — SCENARIO_RESOURCE 에 추가할 것"
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
    from ..config.loader import load_settings

    contributors = attribute(
        db, resource, before=before, after=after, limit=limit,
        settings=load_settings().incident,
    )
    verdict.ranked = [(c.name, c.share, set(c.pids)) for c in contributors]
    return verdict


def score_fault_product(db, fault: dict, *, limit: int = 8) -> Verdict:
    """같은 주입 구간을 **제품이 실제로 낸 사건**으로 채점한다.

    `score_fault` 와의 차이가 요점이다. 그쪽은 `SCENARIO_RESOURCE` 에서 자원을
    **입력으로 받는다** — "핸들 누수니까 handles 로 보라"고 알려주고 시작한다.
    제품에는 그런 입력이 없다. 병목 분류와 탐지기 주장으로 자원을 **추론**해야 한다.

    **라벨은 정답으로만 쓰고 입력으로 쓰지 않는다.** 그래야 채점이 제품이 하는 일을
    재고, 고친 것이 수치에 나타난다. 2026-07-30 에 함수 경로는 7/7 = 100% 였는데 제품은
    같은 구간에서 4건 모두 엉뚱한 프로세스를 지목했다 — 스코어보드가 제품이 하지 않는
    일을 재고 있었다.

    **저장된 제목이 아니라 현재 코드로 다시 분석한 결과를 본다.** 저장된 행은 그때
    코드의 산출물이라, 고친 뒤에도 옛 값이 남아 있으면 개선을 볼 수 없다.
    """
    # 순환 임포트를 피해 함수 안에서 가져온다.
    from ..config.loader import load_settings
    from ..decide.fusion import FusionSettings, analyze_incident

    base = score_fault(db, fault, limit=limit)
    verdict = Verdict(
        fault_id=base.fault_id,
        scenario=base.scenario,
        resource="",           # 추론 결과로 채운다
        answer_pids=base.answer_pids,
        ranked=[],
        skipped=base.skipped,
    )
    if verdict.skipped:
        return verdict

    ts_start, ts_end = float(fault["ts_start"]), float(fault["ts_end"])

    rows = db.query(
        "SELECT id, ts_start, ts_end FROM incidents "
        "WHERE ts_start <= ? AND COALESCE(ts_end, ts_start) >= ? ORDER BY ts_start",
        (ts_end, ts_start),
    )
    if not rows:
        # **탐지가 사건을 만들지 못한 것이지 귀인이 틀린 것이 아니다.** 둘을 한 수치에
        # 섞으면 Phase 8 을 따로 판정할 수 없다. 대신 보고에서 이 건수를 드러낸다.
        verdict.skipped = "사건이 만들어지지 않음 (탐지 실패 — 귀인 대상 아님)"
        return verdict

    # 겹치는 사건이 여럿이면 가장 오래 겹친 것을 본다. 사용자가 그 구간의 이야기로
    # 읽을 가능성이 가장 큰 사건이다.
    def overlap(row) -> float:
        lo = max(ts_start, float(row["ts_start"]))
        hi = min(ts_end, float(row["ts_end"] or row["ts_start"]))
        return max(0.0, hi - lo)

    # **재분석은 전역 지표를 읽는다.** `analyze_incident()` 의 병목 분류가 `metrics_raw`
    # 를 보는데, 그 구간이 보존 정리에 지워졌으면 병목이 "관측 없음"이 되고 자원이
    # 기본값(`cpu`)으로 돌아간다. 기여자도 비고, 결과는 무조건 미지목이다.
    #
    # **그것을 귀인 실패로 세면 탐지기가 아니라 보존 정책을 채점하는 것이다.**
    # `score_fault` 가 "구간에 프로세스 메트릭이 없음"을 빼는 것과 같은 이유다. 다만
    # 여기서만 필요하다 — 함수 경로는 자원을 라벨에서 받으므로 전역 지표를 읽지 않는다.
    #
    # **사건 유무를 먼저 본 뒤에 검사한다.** 사건이 없다는 사실은 `incidents` 가 보존
    # 정리 대상이 아니라 언제 확인해도 확정이고, "사용자가 아무것도 받지 못했다"는
    # 진짜 실패라 데이터가 지워졌다는 이유로 덮으면 안 된다.
    #
    # 2026-08-02 에 이 분류가 없어 07-30 배치 7건이 전부 `0%` 로 계산됐고, 제품 경로
    # 지목률이 0.0% 로 나왔다. 데이터의 절반이 없어 판정할 수 없었던 것이지 퇴행이
    # 아니었다. 더 나쁜 것은 **그 7건이 표본에 남아 이후 실행을 영구히 끌어내린다**는
    # 점이다 — 새 배치가 5/5 를 맞혀도 5/12 = 42% 라 DoD 85% 에 닿을 수 없다.
    if not _has_global_metrics(db, ts_start, ts_end):
        verdict.skipped = "구간에 전역 지표가 없음 (재분석 불능 — 보존 정리)"
        return verdict

    best = max(rows, key=overlap)
    # **채점도 제품과 같은 문턱을 쓴다.** 기본값으로 두면 사용자가 config 를 고쳤을 때
    # 채점 결과와 실제 사건이 갈린다 — 그러면 스코어보드가 제품을 재지 않는다.
    cfg = load_settings()
    fusion_settings = FusionSettings(
        bottleneck=cfg.bottleneck, incident=cfg.incident, autolabel=cfg.autolabel
    )
    analysis = analyze_incident(
        db,
        int(best["id"]),
        float(best["ts_end"] or best["ts_start"]),
        fusion_settings,
    )
    if analysis is None:
        verdict.skipped = "사건을 다시 분석할 수 없음"
        return verdict

    verdict.incident_id = int(best["id"])
    verdict.resource = analysis.resource
    verdict.title = analysis.title
    verdict.ranked = [
        (c.name, c.share, set(c.pids)) for c in analysis.contributors[:limit]
    ]
    return verdict


def score_all_product(
    db, *, scenarios: list[str] | None = None, hours: float | None = None
) -> list[Verdict]:
    return [score_fault_product(db, dict(f)) for f in _faults(db, scenarios, hours)]


def _faults(db, scenarios: list[str] | None, hours: float | None):
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
    return faults


def score_all(db, *, scenarios: list[str] | None = None, hours: float | None = None) -> list[Verdict]:
    return [score_fault(db, dict(f)) for f in _faults(db, scenarios, hours)]


def report_product(verdicts: list[Verdict]) -> str:
    """제품 경로 채점 결과. **사용자가 실제로 읽는 문장까지 보여 준다.**

    자원 열을 따로 두는 이유: 그것이 추론 결과이기 때문이다. 함수 경로에서는 라벨에서
    받은 값이라 볼 필요가 없지만, 여기서는 **무엇을 자원으로 골랐는가가 판정의 일부**다.
    """
    scored = [v for v in verdicts if not v.skipped]
    no_incident = [v for v in verdicts if v.skipped and "사건이 만들어지지 않음" in v.skipped]
    other_skipped = [v for v in verdicts if v.skipped and v not in no_incident]

    lines = [
        "",
        "제품 경로 채점 — 사건이 실제로 원인을 맞게 지목했는가",
        "  (자원을 라벨에서 받지 않고 병목 분류 + 탐지기 주장으로 추론한다)",
        "=" * 78,
    ]
    for v in scored:
        rank = v.hit_rank
        rank_text = f"{rank}위" if rank else "미지목"
        mark = "✅" if v.is_top1 else ("△" if rank else "❌")
        top = v.ranked[0] if v.ranked else None
        top_text = f"{top[0]} ({top[1]*100:.0f}%)" if top else "—"
        lines.append(
            f"{v.fault_id:>3}  사건 {v.incident_id:<4} 자원 {v.resource:<9} "
            f"{rank_text:>8} {mark}  1위 {top_text}"
        )
        if v.title:
            lines.append(f"       제목: {v.title}")

    if scored:
        top1 = sum(1 for v in scored if v.is_top1)
        lines += [
            "-" * 78,
            f"제품 1순위 지목률 : {top1}/{len(scored)} = {top1 / len(scored) * 100:.1f}%  (DoD 85%)",
        ]
    else:
        lines.append("채점 가능한 사건이 없다.")

    if no_incident:
        # **숨기지 않는다.** 귀인 대상은 아니지만, 사용자 입장에서는 아무것도 못 받은 것이다.
        ids = ", ".join(str(v.fault_id) for v in no_incident)
        lines += [
            "",
            f"사건 미생성 {len(no_incident)}건 (주입 #{ids}) — 탐지가 사건을 만들지 못했다.",
            "  귀인 비율에서는 빼지만 제품 관점에서는 실패다. 탐지 쪽(Phase 3·6)의 문제다.",
        ]
    if other_skipped:
        lines += ["", f"그 밖의 제외 {len(other_skipped)}건:"]
        for v in other_skipped:
            lines.append(f"  #{v.fault_id:<3} {v.scenario:<13} {v.skipped}")
    return "\n".join(lines)


def report(verdicts: list[Verdict]) -> str:
    scored = [v for v in verdicts if not v.skipped]
    skipped = [v for v in verdicts if v.skipped]

    lines = [
        "",
        "귀인 채점 (함수 경로) — 자원을 알려준 상태에서 원인을 찾는가",
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

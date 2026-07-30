"""신호 융합 — 원시 판정을 사람이 읽는 사건 하나로 접는다.

**신호와 사건은 다르다.** 룰이 30초 지속을 확인하고 발화하면 그 뒤로 조건이 유지되는
동안 계속 신호가 나온다. 5분짜리 문제 하나가 신호 수십 개다. 사용자에게 필요한 것은
"14:32~14:38 디스크 병목" 한 줄이지 같은 말 수십 번이 아니다.

**귀인은 사건을 닫을 때 계산한다.** 구간이 확정돼야 "변화점 전후"를 비교할 수 있다.
진행 중에 계산하면 아직 오르는 중인 프로세스를 원인에서 빠뜨린다.

**알림은 여기서 보내지 않는다.** severity 를 정하고 `notified` 를 남길 뿐이다.
발송 경로는 오탐률이 검증된 뒤에 붙인다(CLAUDE.md: 알림은 되돌릴 수 없다).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..detection.baseline import BaselineSet
from ..explain.attribution import attribute, lead_time
from ..explain.bottleneck import classify
from ..explain.report import build_incident, render
from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from ..storage.hot import Database
from .budget import NotificationBudget
from .suppression import apply_suppression

log = get_logger(__name__)

WATERMARK_KEY = "fusion_watermark"

# 심각도 순서. 합의가 있으면 한 단계 올린다.
SEVERITY_ORDER = ("info", "warning", "critical")


def _escalate(severity: str, steps: int = 1) -> str:
    try:
        index = SEVERITY_ORDER.index(severity)
    except ValueError:
        return severity
    return SEVERITY_ORDER[min(len(SEVERITY_ORDER) - 1, index + steps)]


@dataclass
class FusionSettings:
    """융합 파라미터. 임계값이 아니라 시간 구조라 config 가 아닌 여기 둔다."""

    # 신호가 이만큼 끊기면 사건이 끝난 것으로 본다. 룰 쿨다운(10분)보다 짧아야
    # 한 사건이 쿨다운 때문에 둘로 쪼개지지 않는다.
    gap_s: float = 120.0
    # 아직 안 쌓였을 수 있는 최근 구간은 건드리지 않는다.
    lag_s: float = 15.0
    # 귀인 비교 창: 사건 시작 전 이만큼을 "평소"로 본다.
    before_window_s: float = 180.0
    before_margin_s: float = 30.0


def open_incident(db: Database, signal: dict, severity: str) -> int:
    """사건을 열고 id 를 돌려준다. 진행 중이므로 `ts_end` 는 NULL 이다."""
    with db._lock:  # noqa: SLF001
        cursor = db.conn.execute(
            "INSERT INTO incidents (ts_start, severity, title, detectors, signal_count, peak_score) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                signal["ts"],
                severity,
                "분석 중",
                json.dumps([signal["detector"]]),
                signal["score"],
            ),
        )
        db.conn.commit()
        return int(cursor.lastrowid)


@dataclass(frozen=True)
class DetectorClaim:
    """탐지기가 **자기가 무엇을 봤는지** 직접 말한 것.

    전역 지표에서 다시 추론한 것이 아니라 탐지기가 신호에 실어 보낸 값이다.
    `procleak` 은 프로세스별 지표에서 발화하므로 자원(`handles`/`rss_mb`)과 범인
    프로세스를 이미 알고 있고, 완성된 설명 문장까지 갖고 있다.
    """

    resource: str
    process: str | None
    explain: str
    score: float
    ts: float = 0.0
    """이 주장을 실어 보낸 신호의 시각."""

    duration_s: float = 0.0
    """탐지기가 **관측한** 증가 지속 시간. 추정이 아니라 자기가 센 값이다.

    사건 경계를 늘리는 데 쓴다. `procleak` 의 추적 창(기본 900초)이 상한이라 이 값이
    사건을 무한정 늘릴 수는 없다.
    """


# `procleak` 의 지표 이름 → 귀인이 쓰는 자원 이름(`attribution.RESOURCE_COLUMNS`).
# 이름이 다른 것은 자원 축이 컬럼과 1:1 이 아니기 때문이다(`rss_mb` 컬럼 ↔ `rss` 자원).
_CLAIM_RESOURCE = {"handles": "handles", "rss_mb": "rss"}


def _trigger_rules(
    db: Database, incident_id: int
) -> tuple[list[str], list[str], DetectorClaim | None]:
    """이 사건에 묶인 신호들이 **어떤 룰**에서 나왔고 **어떤 지표**를 봤는지.

    `detectors` 에 `["rules"]` 만 남기면 아무것도 말해 주지 않는다 — 룰 엔진이라는
    사실은 이미 알고 있다. 알아야 할 것은 **어느 룰이** 울렸는가다.

    지표를 함께 뽑는 이유는 병목 분류가 그것을 필요로 하기 때문이다. 룰 이름은 사용자가
    바꿀 수 있어 분류의 입력으로 쓸 수 없지만, `evidence` 의 지표 이름은 룰 정의에서
    그대로 따라온다.

    셋째로 `DetectorClaim` 을 돌려준다. **이것이 없어서 제품이 원인을 틀리게 지목했다** —
    핸들 누수는 전역 지표에 드러나지 않아 병목이 `NONE` 이 되고, `NONE` 은 `cpu` 로
    매핑되어 "구간에 CPU 를 많이 쓴 프로세스"가 원인으로 발표됐다. 2026-07-30 주입 4건이
    전부 그렇게 엉뚱한 프로세스를 지목했다(claude·system·compattelrunner·l-connect-service).
    탐지기는 `metric: "handles"` 와 범인을 알고 있었고, 그 값을 읽고도 쓰지 않았다.

    여러 탐지기가 섞이면 **점수가 가장 높은 것**을 택한다. 규모가 큰 쪽이 사용자가 느낀
    것에 가깝다는 가정이고, 동점이면 먼저 온 신호가 이긴다(시간순 정렬이라 안정적이다).
    """
    rows = db.query(
        "SELECT s.ts, s.features, s.score FROM anomaly_signals s "
        "JOIN incident_signals i ON i.ts = s.ts AND i.detector = s.detector "
        "WHERE i.incident_id = ? ORDER BY s.ts",
        (incident_id,),
    )
    names: list[str] = []
    metrics: list[str] = []
    claim: DetectorClaim | None = None
    for row in rows:
        try:
            features = json.loads(row["features"] or "{}")
        except (TypeError, ValueError):
            continue
        for name in features.get("rules") or ([features["rule"]] if "rule" in features else []):
            if name not in names:
                names.append(name)
        for metric in features.get("evidence") or {}:
            if metric not in metrics:
                metrics.append(metric)

        resource = _CLAIM_RESOURCE.get(features.get("metric") or "")
        if resource is None:
            continue
        score = float(row["score"] or 0.0)
        if claim is None or score > claim.score:
            claim = DetectorClaim(
                resource=resource,
                process=features.get("process"),
                explain=features.get("explain") or "",
                score=score,
                ts=float(row["ts"]),
                duration_s=float(features.get("duration_s") or 0.0),
            )
    return names, metrics, claim


def _attach_signal(db: Database, incident_id: int, signal: dict) -> None:
    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "INSERT OR IGNORE INTO incident_signals (incident_id, ts, detector, score) "
            "VALUES (?, ?, ?, ?)",
            (incident_id, signal["ts"], signal["detector"], signal["score"]),
        )
        db.conn.commit()


@dataclass(frozen=True)
class IncidentAnalysis:
    """사건 하나의 "왜". **DB 를 건드리지 않고 계산한 결과다.**

    저장과 분리한 이유: 코드를 고친 뒤 **저장된 사건을 다시 분석해 전후를 비교**해야
    하는데, 계산이 저장과 붙어 있으면 그럴 수 없다. 도구에서 계산을 복사하는 것은
    답이 아니다 — 규칙이 두 곳에 있으면 조용히 갈린다(2026-07-30 에 디스크 문턱과
    `NONE` 의 `attributable` 에서 실제로 겪었다).
    """

    ts_start: float
    ts_end: float
    bottleneck: object | None
    contributors: list
    explanation: str
    title: str
    triggers: list[str]
    resource: str


def analyze_incident(
    db: Database, incident_id: int, ts_end: float, settings: FusionSettings | None = None
) -> IncidentAnalysis | None:
    """사건의 "왜" 를 계산한다. **읽기 전용.** 없는 사건이면 None."""
    settings = settings or FusionSettings()
    rows = db.query("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not rows:
        return None
    incident_row = dict(rows[0])
    ts_start = float(incident_row["ts_start"])

    # **사건의 경계를 신호 시각이 아니라 지표에서 다시 찾는다.**
    #
    # 탐지는 늘 늦다(지속 조건 + 베이스라인 창). 게다가 룰은 쿨다운 때문에 지속되는
    # 문제에도 신호를 한 번만 내므로, 신호 시각을 그대로 쓰면 **5분짜리 문제가
    # "0초"로 기록된다.** 실측에서 정확히 그랬고, 그러면 사용자는 순간 스파이크로
    # 읽으며 비교 창 길이가 0 이라 원인 후보가 통째로 비어 버린다.
    ts_start, ts_end = _refine_bounds(db, ts_start, ts_end)

    # 그 구간에서 가장 나빴던 시점을 병목 판정의 대상으로 삼는다.
    peak, baselines = _peak_and_baselines(db, ts_start, ts_end)
    if peak is None:
        # 원본이 이미 정리됐거나 수집이 죽어 있던 구간. 사건은 닫되 설명은 비운다 —
        # 지어내지 않는다.
        return IncidentAnalysis(
            ts_start=ts_start, ts_end=ts_end, bottleneck=None, contributors=[],
            explanation="", title="관측 없음", triggers=[], resource="cpu",
        )

    # 방아쇠를 병목 판정 **앞에서** 읽는다. 무엇이 울렸는지가 무엇에 막혔는지의 입력이다.
    triggers, trigger_metrics, claim = _trigger_rules(db, incident_id)
    bottleneck = classify(peak, baselines, trigger_metrics=trigger_metrics)

    # **병목을 모르겠으면 탐지기가 말한 자원을 쓴다.**
    #
    # 구체적 병목(CPU·IO·MEMORY)이 나왔으면 그것을 유지한다 — 전역 지표가 실제로 증상을
    # 보이는 경우이고, 사용자가 느낀 것은 그쪽이다. 그러나 `NONE` 은 "전역 지표에서
    # 아무것도 못 찾았다"는 뜻이고, 그 상태에서 CPU 로 분해하면 **구간에 CPU 를 많이 쓴
    # 무관한 프로세스**가 원인으로 발표된다. 핸들 누수가 정확히 그 경우다.
    resource = bottleneck.resource
    attributable = bottleneck.attributable
    if bottleneck.kind == "NONE" and claim is not None:
        resource = claim.resource
        attributable = True

        # **탐지기가 센 지속 시간으로 앞쪽 경계를 늘린다.**
        #
        # `_refine_bounds` 는 전역 지표만 본다(`_BOUND_METRICS`). 핸들 누수는 전역
        # 지표를 움직이지 않으므로 보정이 실패하고, 룰 쿨다운 때문에 신호가 몇 개뿐이라
        # **12분짜리 누수가 18초로 기록된다.** 2026-07-30 실측에서 정확히 그랬고, 그
        # 18초 창에서 귀인하면 누수 프로세스의 증가분도 18초분뿐이라 마침 그때 뜬
        # 다른 프로세스에 1위를 빼앗긴다(주입 4건 중 2건이 그랬다).
        #
        # `MAX_EXTEND_BEFORE_S` 를 여기 적용하지 않는 이유: 그 상한은 **지표에서 추정한**
        # 경계가 다른 사건을 삼키는 것을 막으려는 것이다. 여기 값은 탐지기가 자기 창에서
        # **직접 센 것**이라 추정이 아니고, `procleak` 의 추적 창(기본 900초)이 이미 상한이다.
        if claim.duration_s > 0:
            ts_start = min(ts_start, claim.ts - claim.duration_s)

    before = (
        ts_start - settings.before_margin_s - settings.before_window_s,
        ts_start - settings.before_margin_s,
    )
    contributors = attribute(db, resource, before=before, after=(ts_start, ts_end))
    for contributor in contributors:
        contributor.lead_s = lead_time(db, contributor, resource, ts_start)

    symptom = _symptom(peak, baselines)
    report = build_incident(
        ts_start, ts_end, bottleneck, contributors, symptom=symptom, triggers=triggers
    )
    explanation = render(report, resource)

    # **탐지기가 문장을 갖고 있으면 그것이 제목이다.**
    #
    # `병목 없음 — compattelrunner 72%` 는 두 번 틀렸다. 프로세스가 틀렸고(위에서 고쳤다),
    # "병목 없음"은 제목이 아니다. 탐지기는 이미
    # `python (PID 24504) 핸들 458 → 3,838개 (8.4배, 6분간 줄지 않음)` 를 만들어 놨다.
    # CLAUDE.md 는 탐지가 아니라 설명이 산출물이라고 말한다 — 그 문장이 산출물이다.
    #
    # 병목이 구체적으로 잡혔을 때는 기존 형식을 유지한다. 그때는 전역 증상이 있고,
    # `CPU 병목 — chrome 68%` 가 프로세스 하나의 내부 사정보다 사용자에게 가깝다.
    if bottleneck.kind == "NONE" and claim is not None and claim.explain:
        title = claim.explain
    else:
        # **귀인이 성립할 때만 제목에 프로세스를 넣는다.** GPU·발열은 프로세스별 사용량을
        # 알 수 없어 CPU 상위로 대신 분해하는데, 그것을 제목에 올리면 "발열 스로틀링 —
        # svchost 19%" 가 된다. 읽는 사람은 svchost 를 의심하지만 GPU 를 태운 것은 게임이다.
        title = bottleneck.label
        if contributors and attributable:
            title += f" — {contributors[0].name} {contributors[0].share * 100:.0f}%"
        elif bottleneck.evidence:
            title += f" — {bottleneck.evidence[0]}"

    return IncidentAnalysis(
        ts_start=ts_start, ts_end=ts_end, bottleneck=bottleneck, contributors=contributors,
        explanation=explanation, title=title, triggers=triggers, resource=resource,
    )


def close_incident(
    db: Database, incident_id: int, ts_end: float, settings: FusionSettings | None = None
) -> None:
    """사건을 닫으면서 귀인을 계산해 붙인다.

    여기가 Phase 8 과 Phase 9 가 만나는 지점이다. 탐지가 "언제"를 주고,
    귀인이 "왜"를 채운다. 계산은 `analyze_incident` 가 하고 여기서는 저장만 한다.
    """
    analysis = analyze_incident(db, incident_id, ts_end, settings)
    if analysis is None:
        return
    _finish(
        db, incident_id, analysis.ts_end, analysis.bottleneck, analysis.contributors,
        analysis.explanation, analysis.title,
        ts_start=analysis.ts_start, triggers=analysis.triggers,
    )


# 경계를 다시 찾을 때 볼 지표들. 어느 하나가 아니라 **전부** 본다.
_BOUND_METRICS = ("cpu_total", "mem_percent", "disk_resp_ms", "disk_queue", "ctx_switches_ps")

# 경계 보정의 상한.
#
# 보정의 목적은 **탐지 지연을 되돌리는 것**이지 사건을 병합하는 것이 아니다. 상한을
# 두지 않으면 오래 높은 지표 하나(컨텍스트 스위치 등)가 사건을 30분까지 늘려 서로 다른
# 사건을 삼킨다 — 실측에서 4분짜리가 23분 46초가 되며 뒤따르는 사건과 겹쳤다.
#
# 앞쪽이 짧은 이유: 룰의 지속 조건(30~60초)과 베이스라인 지연을 합쳐도 몇 분이면 충분하다.
# 뒤쪽이 긴 이유: 회복은 원인이 사라진 뒤에도 시간이 걸린다(캐시 재구성, 큐 배수).
MAX_EXTEND_BEFORE_S = 300.0
MAX_EXTEND_AFTER_S = 600.0


def _refine_bounds(db: Database, ts_start: float, ts_end: float) -> tuple[float, float]:
    """지표에서 사건의 실제 시작·끝을 다시 찾는다.

    **모든 후보 지표를 보고 가장 넓은 구간을 택한다.** 하나만 쓰면 그 지표가 아닌
    사건을 잘못 잰다 — 메모리 누수 구간을 CPU 로 재면 4초로 나온다(실측). 어느 지표로
    재야 하는지는 병목 판정이 알려주는데, 그 판정은 구간이 정해진 뒤에야 할 수 있어
    순환이다. 전부 보고 가장 오래 벗어난 것을 쓰면 순환 없이 답이 나온다.

    찾지 못하면 원래 값을 그대로 쓴다 — 추정에 실패했다고 없는 경계를 만들지 않는다.
    """
    from ..explain.changepoint import find_onset, find_recovery

    baselines = BaselineSet(window_s=1800.0, min_samples=60)
    for row in db.query(
        "SELECT * FROM metrics_raw WHERE ts >= ? AND ts < ? ORDER BY ts",
        (ts_start - 1800.0, ts_start),
    ):
        baselines.observe(
            row["ts"], {k: row[k] for k in row.keys() if k not in ("ts", "cpu_per_core")}
        )

    best = (ts_start, ts_end)
    for metric in _BOUND_METRICS:
        stats = baselines.stats(metric)
        if stats is None or stats.degenerate:
            continue

        before = [
            (r["ts"], r[metric])
            for r in db.query(
                f"SELECT ts, {metric} FROM metrics_raw WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (ts_start - MAX_EXTEND_BEFORE_S, ts_start),
            )
        ]
        after = [
            (r["ts"], r[metric])
            for r in db.query(
                f"SELECT ts, {metric} FROM metrics_raw WHERE ts >= ? AND ts <= ? ORDER BY ts",
                (ts_end, ts_end + MAX_EXTEND_AFTER_S),
            )
        ]
        onset = find_onset(before, stats, ts_start)
        recovery = find_recovery(after, stats, ts_end)
        if onset is None and recovery is None:
            continue

        candidate = (
            max(onset.ts if onset else ts_start, ts_start - MAX_EXTEND_BEFORE_S),
            min(recovery if recovery else ts_end, ts_end + MAX_EXTEND_AFTER_S),
        )
        if candidate[1] - candidate[0] > best[1] - best[0]:
            best = candidate

    return best


def _peak_and_baselines(db: Database, ts_start: float, ts_end: float):
    """구간의 최악 시점과, 그 이전 30분으로 만든 베이스라인."""
    rows = db.query(
        "SELECT * FROM metrics_raw WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (ts_start, ts_end),
    )
    if not rows:
        return None, BaselineSet()

    baselines = BaselineSet(window_s=1800.0, min_samples=60)
    for row in db.query(
        "SELECT * FROM metrics_raw WHERE ts >= ? AND ts < ? ORDER BY ts",
        (ts_start - 1800.0, ts_start),
    ):
        baselines.observe(
            row["ts"], {k: row[k] for k in row.keys() if k not in ("ts", "cpu_per_core")}
        )

    peak_row = max(rows, key=lambda r: r["cpu_total"] or 0.0)
    peak = {k: peak_row[k] for k in peak_row.keys() if k != "cpu_per_core"}

    # GPU 는 별도 테이블이라 따로 붙인다. 없으면 없는 대로 둔다.
    gpu = db.query(
        "SELECT util_percent, temp_c, throttle_reasons FROM gpu_metrics "
        "WHERE ts >= ? AND ts <= ? AND gpu_index = 0 ORDER BY util_percent DESC LIMIT 1",
        (ts_start, ts_end),
    )
    if gpu:
        peak["gpu_util"] = gpu[0]["util_percent"]
        peak["gpu_temp"] = gpu[0]["temp_c"]
        peak["gpu_throttle_reason"] = gpu[0]["throttle_reasons"]
    return peak, baselines


def _symptom(peak: dict, baselines: BaselineSet) -> str:
    """체감으로 말한다. 사용률이 아니라 '평소의 몇 배'가 사용자의 언어다."""
    stats = baselines.stats("cpu_total")
    value = peak.get("cpu_total")
    if stats and value is not None and not stats.degenerate:
        z = stats.z(value)
        return f"CPU {stats.median:.0f}% → {value:.0f}% (평소의 {z:.1f}σ)"
    return ""


def _finish(
    db: Database,
    incident_id: int,
    ts_end: float,
    bottleneck,
    contributors,
    explanation: str,
    title: str,
    *,
    ts_start: float | None = None,
    triggers: list[str] | None = None,
) -> None:
    payload = [
        {
            "name": c.name,
            "share": round(c.share, 4),
            "delta": round(c.delta, 3),
            "after": round(c.after, 3),
            "pids": sorted(c.pids),
            "lead_s": c.lead_s,
            "is_new": c.is_new,
        }
        for c in contributors
    ]
    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "UPDATE incidents SET ts_start = COALESCE(?, ts_start), ts_end = ?, "
            "bottleneck = ?, title = ?, explanation_md = ?, contributors = ?, "
            "evidence = ?, detectors = COALESCE(?, detectors) WHERE id = ?",
            (
                ts_start,
                ts_end,
                bottleneck.kind if bottleneck else None,
                title,
                explanation,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(bottleneck.evidence, ensure_ascii=False) if bottleneck else None,
                json.dumps(triggers, ensure_ascii=False) if triggers else None,
                incident_id,
            ),
        )
        db.conn.commit()
    log.info(
        "사건 종료",
        extra={"incident": incident_id, "title": title, "contributors": len(payload)},
    )


class Fusion(Component):
    """실시간 신호를 사건으로 접는 컴포넌트."""

    name = "fusion"

    def __init__(
        self,
        db: Database,
        settings: FusionSettings | None = None,
        budget: NotificationBudget | None = None,
    ) -> None:
        self.db = db
        self.settings = settings or FusionSettings()
        self.budget = budget or NotificationBudget()
        self.interval_s = 30.0

    # ------------------------------------------------------------ 워터마크

    def watermark(self) -> float:
        value = self.db.get_meta(WATERMARK_KEY)
        if value is not None:
            return float(value)

        # 첫 실행: 과거 전체를 사건으로 만들지 않는다. 지금부터 본다.
        #
        # **반드시 저장하고 돌려준다.** 저장하지 않으면 `run_once` 가 매번
        # `end = now - lag <= start = now` 로 0 을 반환하고, 다음 틱에서 다시 새 `now`
        # 를 받아 영원히 제자리가 된다. 실측에서 6분 동안 신호 3건이 쌓이는 사이
        # 융합이 한 번도 진행하지 못했다. 리플레이 테스트는 워터마크를 명시적으로
        # 넣고 시작해서 이 경로를 타지 않았다.
        now = time.time()
        self._set_watermark(now)
        return now

    def _set_watermark(self, ts: float) -> None:
        self.db.set_meta(WATERMARK_KEY, str(ts))

    # ------------------------------------------------------------ 융합

    def _open_incident(self) -> dict | None:
        rows = self.db.query(
            "SELECT * FROM incidents WHERE ts_end IS NULL ORDER BY ts_start DESC LIMIT 1"
        )
        return dict(rows[0]) if rows else None

    def run_once(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        start = self.watermark()
        end = now - self.settings.lag_s
        if end <= start:
            return 0

        signals = [
            dict(row)
            for row in self.db.query(
                "SELECT ts, detector, score, severity FROM anomaly_signals "
                "WHERE run_id IS NULL AND ts > ? AND ts <= ? ORDER BY ts",
                (start, end),
            )
        ]

        created = 0
        current = self._open_incident()
        last_ts = None
        if current:
            row = self.db.query(
                "SELECT MAX(ts) AS hi FROM incident_signals WHERE incident_id = ?",
                (current["id"],),
            )
            last_ts = row[0]["hi"] if row and row[0]["hi"] else current["ts_start"]

        for signal in signals:
            if current is not None and last_ts is not None:
                if signal["ts"] - last_ts > self.settings.gap_s:
                    self._close(current["id"], last_ts)
                    current = None

            if current is None:
                incident_id = open_incident(self.db, signal, signal["severity"] or "info")
                current = {"id": incident_id, "ts_start": signal["ts"]}
                created += 1
            else:
                self._merge(current["id"], signal)

            _attach_signal(self.db, current["id"], signal)
            last_ts = signal["ts"]

        # 신호가 끊긴 지 오래면 닫는다. 새 신호가 없어도 사건은 끝나야 한다.
        if current is not None and last_ts is not None and end - last_ts > self.settings.gap_s:
            self._close(current["id"], last_ts)

        self._set_watermark(end)
        return created

    def _close(self, incident_id: int, ts_end: float) -> None:
        """사건을 닫고 억제·알림 판단까지 한 번에 한다.

        순서가 중요하다 — 억제 여부가 알림 판단의 입력이다.
        """
        close_incident(self.db, incident_id, ts_end, self.settings)
        apply_suppression(self.db, incident_id)

        rows = self.db.query("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if not rows:
            return
        decision = self.budget.decide(self.db, dict(rows[0]))
        self.budget.record(self.db, incident_id, decision)
        if decision.notify:
            # 발송은 여기서 하지 않는다. 오탐률이 검증된 뒤에 붙인다.
            log.info("알림 대상", extra={"incident": incident_id})
        else:
            log.debug("알림 생략", extra={"incident": incident_id, "reason": decision.reason})

    def _merge(self, incident_id: int, signal: dict) -> None:
        """진행 중인 사건에 신호를 더한다.

        **탐지기 합의는 심각도를 올린다.** 서로 다른 방식이 같은 시각에 이상이라고
        하면 우연일 가능성이 줄기 때문이다. 같은 탐지기가 여러 번 발화한 것은
        합의가 아니라 지속이므로 세지 않는다.
        """
        rows = self.db.query("SELECT * FROM incidents WHERE id = ?", (incident_id,))
        if not rows:
            return
        row = dict(rows[0])
        detectors = set(json.loads(row["detectors"] or "[]"))
        detectors.add(signal["detector"])

        severity = row["severity"]
        if signal["severity"] and SEVERITY_ORDER.index(signal["severity"]) > SEVERITY_ORDER.index(
            severity
        ):
            severity = signal["severity"]
        if len(detectors) > 1:
            severity = _escalate(severity)

        with self.db._lock:  # noqa: SLF001
            self.db.conn.execute(
                "UPDATE incidents SET detectors = ?, signal_count = signal_count + 1, "
                "peak_score = MAX(COALESCE(peak_score, 0), ?), severity = ? WHERE id = ?",
                (
                    json.dumps(sorted(detectors)),
                    signal["score"] or 0.0,
                    severity,
                    incident_id,
                ),
            )
            self.db.conn.commit()

    def tick(self) -> None:
        self.run_once()


if __name__ == "__main__":  # 스모크: python -m argus.decide.fusion
    from ..logging_setup import setup

    setup(level="INFO")
    with Database() as db:
        fusion = Fusion(db)
        before = db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"]
        created = fusion.run_once()
        after = db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"]
        print(f"  워터마크 : {fusion.watermark()}")
        print(f"  새 사건  : {created} (누적 {before} -> {after})")

        for row in db.query("SELECT * FROM incidents ORDER BY ts_start DESC LIMIT 3"):
            stamp = time.strftime("%H:%M:%S", time.localtime(row["ts_start"]))
            state = "진행 중" if row["ts_end"] is None else "종료"
            print(f"    [{row['id']}] {stamp} {row['severity']:<8} {state:<6} {row['title']}")

        signals = db.query(
            "SELECT COUNT(*) AS c FROM anomaly_signals WHERE run_id IS NULL"
        )[0]["c"]
        print(f"  실시간 신호 누적: {signals}건")
    print("[OK] decide.fusion")

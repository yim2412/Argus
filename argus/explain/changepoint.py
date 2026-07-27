"""변화점 — 이상이 *언제 시작됐는지* 찾는다.

탐지는 늘 늦다. 룰은 지속 조건(`for: 30s`)을 요구하고 베이스라인은 창을 채워야 하므로,
"이상하다"고 말하는 시각은 실제로 나빠지기 시작한 시각보다 수십 초에서 수 분 뒤다.
그 시각을 그대로 쓰면 **기여도 분해가 엉뚱한 구간을 비교하게 된다** — 이미 모두가
바빠진 뒤라서 누가 먼저였는지 사라진다.

그래서 신호 시각에서 **거슬러 올라가** 지표가 평소 범위를 벗어나기 시작한 지점을 찾는다.

`ruptures`·BOCPD 를 쓰지 않는 이유: 그 도구들은 "구간 어딘가에 변화가 있는가"를 모를 때
쓴다. 우리는 이미 이상이 있다는 것과 대략 언제인지를 안다. 필요한 것은 경계를 다듬는
일이고, 그건 이미 가진 중앙값/MAD 로 충분하다. 의존성 하나가 답을 더 낫게 만들지
못한다면 넣지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..detection.baseline import Stats
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Onset:
    """이상 시작점."""

    ts: float
    metric: str
    value: float
    z: float | None
    lead_s: float
    """신호 시각보다 얼마나 앞섰나(초). 클수록 탐지가 늦었다는 뜻이다."""


def find_onset(
    samples: list[tuple[float, float | None]],
    stats: Stats,
    signal_ts: float,
    *,
    k: float = 2.0,
    min_run: int = 3,
    max_gap: int = 15,
) -> Onset | None:
    """신호 시각에서 역방향으로 훑어 이상이 시작된 지점을 찾는다.

    `min_run` 은 연속으로 임계를 넘어야 시작으로 인정하는 표본 수다. 하나만 튄 것은
    시작점이 아니라 잡음이다.

    `max_gap` 이 핵심이다. **서서히 나빠지는 구간은 임계를 여러 번 들락날락한다** —
    램프 부하가 정확히 그렇다. 임계 미만 표본 하나에 즉시 끊으면 시작점이 실제보다
    한참 뒤로 잡히고, 그러면 "원인 프로세스가 결과보다 늦게 올랐다"는 말이 안 되는
    결론이 나온다(실측에서 주입 프로세스가 34초 *후행*으로 표시됐다). 그래서 임계
    미만이 `max_gap` 개 **연속으로** 나와야 구간이 끝난 것으로 본다.

    `samples` 는 (ts, value) 오름차순. `stats.degenerate` 면 z 판정이 불가능하므로
    None 을 돌려준다(그런 지표는 절대 임계값의 영역이다).
    """
    if stats.degenerate or not samples:
        return None

    threshold = stats.threshold(k)
    if threshold is None:
        return None

    ordered = [(ts, v) for ts, v in samples if v is not None and ts <= signal_ts]
    if len(ordered) < min_run:
        return None

    onset_index: int | None = None
    above = 0
    gap = 0
    for index in range(len(ordered) - 1, -1, -1):
        _, value = ordered[index]
        if value >= threshold:
            above += 1
            gap = 0
            onset_index = index
        else:
            gap += 1
            if gap >= max_gap:
                # 정상 구간에 확실히 도달했다. 여기서 멈춘다.
                break

    if onset_index is None or above < min_run:
        return None

    ts, value = ordered[onset_index]
    return Onset(
        ts=ts,
        metric=stats.metric,
        value=value,
        z=stats.z(value),
        lead_s=round(signal_ts - ts, 1),
    )


def find_onset_from_db(
    db,
    metric: str,
    stats: Stats,
    signal_ts: float,
    *,
    lookback_s: float = 900.0,
    k: float = 2.0,
) -> Onset | None:
    """DB 의 `metrics_raw` 에서 직접 찾는다."""
    if metric not in _ALLOWED_METRICS:
        raise ValueError(f"허용되지 않은 메트릭: {metric}")
    rows = db.query(
        f"SELECT ts, {metric} AS value FROM metrics_raw WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (signal_ts - lookback_s, signal_ts),
    )
    return find_onset([(r["ts"], r["value"]) for r in rows], stats, signal_ts, k=k)


# SQL 에 이름을 직접 넣으므로 화이트리스트로 막는다. 값이 아니라 컬럼이라 바인딩이 안 된다.
_ALLOWED_METRICS = {
    "cpu_total",
    "cpu_max_core",
    "cpu_perf_percent",
    "mem_percent",
    "mem_used_mb",
    "swap_used_mb",
    "disk_read_bps",
    "disk_write_bps",
    "disk_queue",
    "disk_resp_ms",
    "net_rx_bps",
    "net_tx_bps",
    "ctx_switches_ps",
}

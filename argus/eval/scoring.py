"""채점 — 탐지 결과를 정답 라벨과 맞춰 숫자로 만든다.

**채점 단위는 틱이 아니라 알람이다.** 이게 이 파일의 가장 중요한 결정이다.

틱 단위로 세면 30분짜리 주입 구간에서 매 초 발화한 탐지기가 TP 1,800 점을 얻는다.
그러면 (a) 긴 주입 하나가 점수 전체를 지배하고, (b) "일단 계속 발화하는" 탐지기가
재현율 만점을 받으며, (c) 짧지만 중요한 이상은 반올림으로 사라진다.

사용자가 실제로 겪는 단위는 **알람 횟수**다. 5분간 매 초 울린 것은 짜증나는 알람
'하나'지 300개가 아니다. CLAUDE.md 의 "오탐 3번이면 사용자는 알림을 끈다"도 같은 단위다.
그래서 연속 발화는 `ALARM_MERGE_S` 안에서 하나로 묶고, 주입 구간당 최대 1 TP 를 준다.

**지연(latency)을 F1 과 함께 본다.** 30분짜리 누수를 29분 만에 잡은 탐지기와 2분 만에
잡은 탐지기는 F1 이 같다. 실사용 가치는 완전히 다르다.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..detection.base import Detection
from ..storage.hot import Database
from .replay import Window

# 이 간격 안의 연속 발화는 알람 하나로 묶는다. 사용자 체감 단위에 맞춘 값이고,
# Phase 9 의 알림 쿨다운과 같은 자리에 있어야 한다.
ALARM_MERGE_S = 60.0

# 주입이 끝난 직후의 발화는 오탐으로 치지 않는다. 메모리 누수는 주입을 멈춰도
# 회수까지 시간이 걸리고, 그 여진을 오탐으로 세면 탐지기가 부당하게 벌점을 받는다.
DEFAULT_TAIL_GRACE_S = 90.0

# 정상 구간이 이보다 짧으면 오탐률을 숫자로 내지 않는다. 10분 관측에서 오탐 1건을
# 6건/시간으로 환산하는 것은 산술이지 측정이 아니다.
MIN_NORMAL_HOURS = 0.5


@dataclass(frozen=True)
class Episode:
    """정답 구간 하나 = 결함 주입 한 번."""

    id: int
    scenario: str
    ts_start: float
    ts_end: float
    pid: int | None = None
    ramp: bool = False

    @property
    def duration_s(self) -> float:
        return max(0.0, self.ts_end - self.ts_start)


@dataclass
class Alarm:
    """묶인 발화 하나."""

    ts_first: float
    ts_last: float
    peak_score: float
    count: int


@dataclass
class ScoreResult:
    detector: str
    window: Window
    scenarios: list[str]
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    latencies_s: list[float] = field(default_factory=list)
    normal_hours: float = 0.0
    missed: list[str] = field(default_factory=list)
    alarms: int = 0

    @property
    def precision(self) -> float:
        hit = self.true_positive + self.false_positive
        return self.true_positive / hit if hit else 0.0

    @property
    def recall(self) -> float:
        actual = self.true_positive + self.false_negative
        return self.true_positive / actual if actual else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def median_latency_s(self) -> float | None:
        return statistics.median(self.latencies_s) if self.latencies_s else None

    @property
    def fp_per_hour(self) -> float:
        """정상 구간 오탐률. 표본이 부족하면 NaN — 0 으로 보고하지 않는다.

        정상 구간이 90초뿐인데 오탐 2건이 나오면 산술적으로는 80건/시간이지만,
        그 숫자는 아무것도 뜻하지 않는다. 0 으로 보고하면 "오탐 없음"으로 읽히고,
        80 으로 보고하면 근거 없는 공포를 만든다. 둘 다 틀리므로 모른다고 말한다.
        """
        if self.normal_hours < MIN_NORMAL_HOURS:
            return float("nan")
        return self.false_positive / self.normal_hours

    @property
    def fp_rate_reliable(self) -> bool:
        return self.normal_hours >= MIN_NORMAL_HOURS

    def to_row(self, *, params: dict | None = None, notes: str = "") -> tuple:
        latency = self.median_latency_s
        fph = self.fp_per_hour
        return (
            time.time(),
            self.detector,
            self.window.start,
            self.window.end,
            json.dumps(self.scenarios, ensure_ascii=False),
            self.true_positive,
            self.false_positive,
            self.false_negative,
            round(self.precision * 100, 2),
            round(self.recall * 100, 2),
            round(self.f1, 4),
            None if latency is None else round(latency, 2),
            None if fph != fph else round(fph, 3),  # NaN 은 자기 자신과 다르다
            json.dumps(params or {}, ensure_ascii=False, default=str),
            notes,
        )


EVAL_RUN_COLUMNS = (
    "ts", "detector", "window_start", "window_end", "scenarios",
    "true_positive", "false_positive", "false_negative",
    "precision_pct", "recall_pct", "f1", "detect_latency_s", "fp_per_hour",
    "params", "notes",
)


# ------------------------------------------------------------------ 정답 로드


def load_episodes(
    db: Database,
    window: Window | None = None,
    scenarios: Sequence[str] | None = None,
) -> list[Episode]:
    """정답 구간을 읽는다.

    `completed=0` 인 행은 제외한다 — 주입기가 죽어서 실제로 언제까지 부하가 걸렸는지
    모르는 구간이다. 모르는 것을 정답으로 쓰면 채점 전체가 오염된다.
    """
    sql = "SELECT * FROM fault_injections WHERE completed=1 AND ts_end IS NOT NULL"
    params: list[object] = []
    if window is not None:
        sql += " AND ts_end >= ? AND ts_start <= ?"
        params += [window.start, window.end]
    if scenarios:
        sql += f" AND scenario IN ({','.join('?' * len(scenarios))})"
        params += list(scenarios)
    sql += " ORDER BY ts_start"

    return [
        Episode(
            id=row["id"],
            scenario=row["scenario"],
            ts_start=row["ts_start"],
            ts_end=row["ts_end"],
            pid=row["pid"],
            ramp=bool(row["ramp"]),
        )
        for row in db.query(sql, params)
    ]


def filter_covered(
    episodes: Sequence[Episode],
    observations: Sequence,
    *,
    min_observations: int = 3,
    min_coverage: float = 0.5,
) -> tuple[list[Episode], list[tuple[Episode, str]]]:
    """관측이 실제로 존재하는 정답 구간만 남긴다.

    **없으면 조용히 틀린다.** 수집기가 죽어 있던 동안 결함을 주입하면 정답 라벨은
    남지만 메트릭은 없다. 그 구간을 채점에 넣으면 모든 탐지기가 FN 을 먹는데,
    이건 탐지기가 못 잡은 게 아니라 볼 것이 없었던 것이다. 실제로 2026-07-27 에
    수집기가 죽은 줄 모르고 주입해서 이 상황을 만들었다.

    `suspect` 관측은 커버리지로 치지 않는다 — 탐지기가 건너뛰도록 되어 있는 구간이라
    거기에 데이터가 있어도 탐지 기회는 없었던 것과 같다.

    반환: (채점할 구간, [(제외된 구간, 사유)])
    """
    kept: list[Episode] = []
    dropped: list[tuple[Episode, str]] = []
    for episode in episodes:
        inside = [o for o in observations if episode.ts_start <= o.ts <= episode.ts_end]
        usable = [o for o in inside if not o.suspect]
        if len(usable) < min_observations:
            dropped.append((episode, f"관측 {len(usable)}틱 (최소 {min_observations})"))
            continue
        # 구간 전체에 걸쳐 있는지 — 앞머리만 있고 끊긴 경우를 잡는다
        span = max(o.ts for o in usable) - min(o.ts for o in usable)
        coverage = span / episode.duration_s if episode.duration_s > 0 else 1.0
        if coverage < min_coverage:
            dropped.append((episode, f"구간의 {coverage * 100:.0f}%만 관측됨"))
            continue
        kept.append(episode)
    return kept, dropped


# ------------------------------------------------------------------ 알람 묶기


def cluster_alarms(detections: Iterable[Detection], merge_s: float = ALARM_MERGE_S) -> list[Alarm]:
    """연속 발화를 알람 단위로 묶는다."""
    alarms: list[Alarm] = []
    for d in sorted(detections, key=lambda x: x.ts):
        if alarms and d.ts - alarms[-1].ts_last <= merge_s:
            current = alarms[-1]
            current.ts_last = d.ts
            current.peak_score = max(current.peak_score, d.score)
            current.count += 1
        else:
            alarms.append(Alarm(ts_first=d.ts, ts_last=d.ts, peak_score=d.score, count=1))
    return alarms


# ------------------------------------------------------------------ 채점


def _subtract(segment: tuple[float, float], blocks: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """구간에서 블록들을 뺀 나머지."""
    parts = [segment]
    for lo, hi in blocks:
        nxt: list[tuple[float, float]] = []
        for a, b in parts:
            if hi <= a or lo >= b:
                nxt.append((a, b))
                continue
            if a < lo:
                nxt.append((a, lo))
            if hi < b:
                nxt.append((hi, b))
        parts = nxt
    return parts


def score(
    detector_name: str,
    detections: Iterable[Detection],
    episodes: Sequence[Episode],
    window: Window,
    *,
    merge_s: float = ALARM_MERGE_S,
    tail_grace_s: float = DEFAULT_TAIL_GRACE_S,
    observations: Sequence | None = None,
) -> ScoreResult:
    """탐지 결과를 정답과 맞춘다.

    - **TP/FN 은 구간 단위**: 주입 구간마다 "잡았는가"를 독립적으로 묻는다. 한 알람이
      두 구간에 걸쳐 있으면 둘 다 잡은 것이다. 사용자의 질문이 "이 문제를 잡았나"이지
      "몇 번 울렸나"가 아니기 때문이다.
    - **FP 는 알림 단위**: 정상 구간에 걸친 알람 시간을 `merge_s` 로 나눠 센다.
      `merge_s` 는 알림 쿨다운과 같은 값이므로, 정상 구간에서 10분간 계속 발화한
      탐지기는 사용자에게 10번 울린 것과 같다. 이렇게 세지 않으면 **영원히 발화하는
      탐지기가 FP 1건으로 만점을 받는다** — 실제로 `always` 기준선이 그걸 증명했다.

    TP 와 FP 의 단위가 다른 것(구간 vs 알림)은 의도한 것이다. 재현율은 "놓친 문제가
    있나", 정밀도는 "쓸데없이 몇 번 울렸나"를 물어야 하고, 그 둘은 원래 다른 단위다.

    - 지연: 주입 시작 → 그 구간의 첫 알람
    """
    alarms = cluster_alarms(detections, merge_s)
    result = ScoreResult(
        detector=detector_name,
        window=window,
        scenarios=sorted({e.scenario for e in episodes}),
        alarms=len(alarms),
    )

    # 정답 구간(여진 유예 포함). 이 안의 발화는 오탐이 아니다.
    blocks = sorted((e.ts_start, e.ts_end + tail_grace_s) for e in episodes)

    # --- TP / FN / 지연 : 구간마다 독립적으로
    for episode in episodes:
        first: float | None = None
        for alarm in alarms:
            if alarm.ts_last >= episode.ts_start and alarm.ts_first <= episode.ts_end + tail_grace_s:
                start = max(alarm.ts_first, episode.ts_start)
                if first is None or start < first:
                    first = start
        if first is None:
            result.false_negative += 1
            result.missed.append(f"{episode.scenario}{'(ramp)' if episode.ramp else ''}")
        else:
            result.true_positive += 1
            result.latencies_s.append(first - episode.ts_start)

    # --- FP : 정상 구간에 남은 알람 시간을 쿨다운 단위로
    for alarm in alarms:
        for a, b in _subtract((alarm.ts_first, alarm.ts_last), blocks):
            # 순간 발화(길이 0)도 알림 1건이다
            result.false_positive += max(1, int((b - a) // merge_s))

    # --- 오탐률의 분모: 실제로 관측된 정상 시간
    # 창 길이를 쓰면 안 된다. 수집기가 죽어 있던 구간까지 "정상이었다"로 세어
    # 오탐률이 실제보다 낮게 나온다(2026-07-27 에 5.7분을 그렇게 셌다).
    if observations is not None:
        ticks = [o.ts for o in observations if not o.suspect
                 and not any(a <= o.ts <= b for a, b in blocks)]
        normal_s = _observed_seconds(ticks)
    else:
        injected = sum(min(b, window.end) - max(a, window.start)
                       for a, b in blocks if b >= window.start and a <= window.end)
        normal_s = max(0.0, window.duration_s - max(0.0, injected))
    result.normal_hours = normal_s / 3600.0
    return result


def _observed_seconds(times: Sequence[float]) -> float:
    """관측 틱이 실제로 덮은 시간. 틱 사이가 벌어진 곳은 덮지 않은 것으로 본다."""
    if len(times) < 2:
        return 0.0
    ordered = sorted(times)
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    tick_s = statistics.median(gaps)
    ceiling = tick_s * 3  # 이보다 벌어지면 그 사이는 관측하지 않은 것
    return sum(g for g in gaps if g <= ceiling)


def persist(db: Database, result: ScoreResult, *, params: dict | None = None, notes: str = "") -> None:
    """`eval_runs` 에 남긴다. 회귀 감시의 근거 — 어제보다 나빠졌는지 알아야 한다."""
    db.insert_many("eval_runs", EVAL_RUN_COLUMNS, [result.to_row(params=params, notes=notes)])


def format_report(results: Sequence[ScoreResult]) -> str:
    """사람이 읽는 리포트. Phase 2 DoD 가 요구하는 출력이다."""
    if not results:
        return "채점할 결과가 없다."

    lines = []
    head = f"{'탐지기':<20}{'TP':>4}{'FP':>4}{'FN':>4}{'정밀도':>9}{'재현율':>9}{'F1':>8}{'지연':>10}{'오탐/시간':>11}"
    lines.append(head)
    lines.append("-" * len(head))
    for r in sorted(results, key=lambda x: x.f1, reverse=True):
        latency = r.median_latency_s
        fph = r.fp_per_hour
        lines.append(
            f"{r.detector:<20}{r.true_positive:>4}{r.false_positive:>4}{r.false_negative:>4}"
            f"{r.precision * 100:>8.1f}%{r.recall * 100:>8.1f}%{r.f1:>8.3f}"
            f"{'—' if latency is None else f'{latency:.0f}s':>10}"
            f"{'—' if fph != fph else f'{fph:.2f}':>11}"
        )

    lines.append("")
    unreliable = [r for r in results if not r.fp_rate_reliable]
    if unreliable:
        lines.append(
            f"⚠ 오탐률 미측정 — 정상 구간이 {unreliable[0].normal_hours * 60:.0f}분뿐이다"
            f" (최소 {MIN_NORMAL_HOURS * 60:.0f}분 필요). 더 긴 구간을 재생할 것."
        )
        lines.append("")
    for r in sorted(results, key=lambda x: x.f1, reverse=True):
        latency = r.median_latency_s
        if r.true_positive:
            got = f"{r.true_positive}건 탐지" + (f", 중앙값 {latency:.0f}초 만에" if latency is not None else "")
        else:
            got = "한 건도 탐지하지 못함"
        miss = f" 놓친 것: {', '.join(r.missed)}" if r.missed else ""
        lines.append(f"· {r.detector}: {got}.{miss}")
    return "\n".join(lines)


if __name__ == "__main__":  # 스모크: python -m argus.eval.scoring
    from ..detection.base import Detection as D

    window = Window(1000.0, 1000.0 + 3600)
    episodes = [
        Episode(id=1, scenario="memory_leak", ts_start=1500.0, ts_end=1800.0),
        Episode(id=2, scenario="cpu_spin", ts_start=3000.0, ts_end=3200.0),
    ]

    # 완벽한 탐지기: 두 구간 모두, 주입 30초 뒤에 발화. 정상 구간에는 조용.
    perfect = [D(ts=t, detector="perfect", score=0.9)
               for t in list(range(1530, 1800, 5)) + list(range(3030, 3200, 5))]
    # 시끄러운 탐지기: 전 구간에서 5분마다 발화
    noisy = [D(ts=float(t), detector="noisy", score=0.9) for t in range(1000, 4600, 300)]
    # 침묵하는 탐지기
    silent: list[D] = []
    # 무조건 발화: 재현율 100% 를 공짜로 받는 바보. 이게 최고점을 받으면 채점이 틀린 것이다.
    always = [D(ts=float(t), detector="always", score=1.0) for t in range(1000, 4600)]

    results = [
        score("perfect", perfect, episodes, window),
        score("noisy", noisy, episodes, window),
        score("always", always, episodes, window),
        score("silent", silent, episodes, window),
    ]
    print(format_report(results))

    by_name = {r.detector: r for r in results}
    problems = []
    if by_name["perfect"].f1 != 1.0:
        problems.append(f"완벽한 탐지기의 F1 이 1.0 이 아니다: {by_name['perfect'].f1}")
    if by_name["perfect"].median_latency_s != 30.0:
        problems.append(f"지연 계산이 틀렸다: {by_name['perfect'].median_latency_s} (30 이어야)")
    if by_name["silent"].recall != 0.0 or by_name["silent"].false_negative != 2:
        problems.append("침묵 탐지기가 FN 2건으로 잡히지 않았다")
    if by_name["noisy"].false_positive < 5:
        problems.append(f"시끄러운 탐지기의 FP 가 너무 적다: {by_name['noisy'].false_positive}")
    if by_name["noisy"].f1 >= by_name["perfect"].f1:
        problems.append("시끄러운 탐지기가 완벽한 탐지기보다 높거나 같게 채점됐다")
    # 무조건 발화는 재현율 100% 를 받아야 하지만(그게 사실이므로) F1 에서는 져야 한다
    if by_name["always"].recall != 1.0:
        problems.append(f"무조건 발화의 재현율이 100% 가 아니다: {by_name['always'].recall}")
    if by_name["always"].f1 >= by_name["perfect"].f1:
        problems.append("무조건 발화하는 탐지기가 완벽한 탐지기와 같거나 높게 채점됐다 — 채점이 무의미하다")
    if by_name["always"].false_positive < 10:
        problems.append(f"무조건 발화의 FP 가 너무 적다: {by_name['always'].false_positive} "
                        "(정상 구간 내내 울렸으므로 쿨다운 단위로 많아야 한다)")
    # 알람 묶기: 5초 간격 연속 발화가 구간당 하나로 묶여야 한다
    if len(cluster_alarms(perfect)) != 2:
        problems.append(f"연속 발화가 알람 2개로 묶이지 않았다: {len(cluster_alarms(perfect))}")

    # 커버리지 필터: 데이터가 없는 구간은 채점에서 빠져야 한다
    from ..detection.base import Observation as O

    observed = [O(ts=float(t)) for t in range(1500, 1801, 10)]          # 1번 구간만 관측
    observed += [O(ts=float(t), suspect=True) for t in range(3000, 3201, 10)]  # 2번은 신뢰 불가
    kept, dropped = filter_covered(episodes, observed)
    print(f"\n  커버리지 필터: 채점 {len(kept)}건 / 제외 {len(dropped)}건 "
          + "".join(f"({e.scenario}: {why})" for e, why in dropped))
    if [e.id for e in kept] != [1]:
        problems.append(f"관측 있는 구간만 남지 않았다: {[e.id for e in kept]}")
    if len(dropped) != 1:
        problems.append(f"관측 없는 구간이 제외되지 않았다: {len(dropped)}건 제외")

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] eval.scoring")

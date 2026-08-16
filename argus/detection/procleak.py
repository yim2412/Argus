"""프로세스 누수 탐지 — 지문 없이, 자기 과거만으로 판정한다.

**왜 지문(Phase 6-B) 없이 되는가.** 누수는 "남과 비교"가 아니라 "자기 과거와 비교"다.
크롬의 핸들이 평소 몇 개인지 몰라도, *지금 이 인스턴스가* 10분간 한 번도 안 줄고
6배로 불었다면 그건 누수다. 3일치 지문도, 웜 병합 조회도 필요 없다. 그래서 이쪽이
먼저다 — 무거운 지문을 짓기 전에 **이길 상대**를 만들어 둔다(PLAN 정렬 원칙 4).

**시스템 룰(`rules.py`)로는 닿지 않는다.** 룰 엔진은 `obs.metrics`(시스템 전역)만
본다. 핸들 누수는 프로세스별 지표라 전역 메트릭에 거의 드러나지 않는다 — 한 프로세스가
핸들 6000개를 쥐고 있어도 CPU·메모리·디스크는 평온하다. `handle_leak` 이 계획서에서
계속 미탐으로 남아 있던 이유가 이것이다.

**절대 임계값을 두지 않는다.** 판정은 전부 "자기 시작값 대비 배수"다. 예외는 핸들의
최소 증가폭 하나인데, 핸들 수는 RAM 이나 코어 수에 비례하지 않으므로 절대값이 맞다
(`tools/fault_injector.py` 의 `HandleLeak.expected_effect` 와 같은 판단). RSS 는
비례하므로 **이 PC 의 RAM 비율**로 표현한다 — 4GB PC 와 64GB PC 에서 같은 뜻이 되게.

**오탐이 미탐보다 비싸다.** 지속 조건(`min_duration_s`)과 프로세스별 쿨다운을 강제한다.
게임 로딩 중 RSS 가 1분간 치솟는 것은 누수가 아니다.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..config.loader import SeveritySettings
from ..decide.severity import leak_risk
from ..logging_setup import get_logger
from .base import BaseDetector, Detection, Observation, ProcessView

log = get_logger(__name__)


@dataclass(frozen=True)
class MetricRule:
    """지표 하나의 누수 판정 기준."""

    attr: str            # ProcessView 의 필드명
    label: str           # 사람에게 보일 이름
    unit: str
    growth_ratio: float  # 창 시작 대비 몇 배가 되어야 하는가
    min_delta: float     # 최소 절대 증가량 (배수만 보면 3→9 같은 잡음이 걸린다)
    monotonic_ratio: float  # 표본 간 "줄지 않음" 비율. 등락하는 정상 프로세스를 거른다


# 기본값. 실제 값은 config 에서 온다 — 임계값을 코드에 박지 않는다(설계 규칙 3).
DEFAULT_RULES = (
    MetricRule("handles", "핸들", "개", growth_ratio=3.0, min_delta=500.0, monotonic_ratio=0.85),
    MetricRule("rss_mb", "메모리", "MB", growth_ratio=3.0, min_delta=512.0, monotonic_ratio=0.9),
)

# **그룹 축** — 같은 프로그램의 프로세스를 이름으로 묶어 합쳐 본다.
#
# 위 `DEFAULT_RULES` 는 `(pid, name, metric)` 로 추적하므로 **누수가 여러 프로세스에
# 흩어지면 각자는 문턱 아래**가 된다. 2026-08-16 실주입에서 총 11.8GB 를 8개로 나눴더니
# 60분 내내 아무 신호도 나지 않았다(라벨 `#65`·`#66`). 같은 양을 한 프로세스로 넣으면
# 5~9분에 잡힌다 — 분산 하나만으로 무력화된다.
#
# **값은 "현재값의 합"이 아니라 "각 PID 증가분의 합"이다.** 합을 그대로 쓰면 새 PID 가
# 들어올 때마다 그 프로세스의 기본 보유량이 계단으로 얹혀 배수 조건을 인위로 넘긴다
# (크롬 탭 하나만 열어도). `explain/attribution._window_growth` 가 같은 문제를 겪고
# **자기 첫 관측값을 기준으로 삼는 것**으로 풀었으므로 그 방식을 그대로 쓴다.
#
# **배수(`growth_ratio`)를 보지 않는다.** 증가분 합은 0 에서 시작하므로 `last/first`
# 가 누수 초기에 발산한다. 배수는 PID별 축이 이미 보고 있고, 그룹 축이 더할 것은
# "흩어져 있어 각자는 작지만 합치면 크다"뿐이다. 그래서 **증가량만** 본다.
#
# **문턱은 여기에 없다.** `group_rules_from()` 이 PID별 문턱의 배수로 만든다 —
# 절대값을 여기 박아 두면 `min_delta_ram_ratio` 가 PID별만 하드웨어에 맞춰 움직여
# 두 문턱이 갈린다. 2026-08-17 에 그래서 그룹 문턱이 PID별의 1.17배가 됐고,
# 실주입 `#65`·`#66` 이 둘 다 미탐이었다.
GROUP_ATTRS = ("rss_mb",)
_GROUP_LABELS = {"rss_mb": ("메모리(합계)", "MB")}
DEFAULT_GROUP_MULTIPLE = 0.8


def group_rules_from(rules: Iterable[MetricRule], *,
                     multiple: float) -> tuple[MetricRule, ...]:
    """PID별 룰에서 그룹 룰을 만든다. **문턱은 언제나 PID별의 배수다.**

    `monotonic_ratio` 도 PID별 것을 그대로 물려받는다 — 같은 값을 두 곳에 두면
    한쪽만 고쳐져 갈린다.

    `GROUP_ATTRS` 에 없는 지표는 그룹으로 보지 않는다 — 핸들은 프로세스마다 용도가
    달라 합계에 의미가 없다.
    """
    out = []
    for rule in rules:
        if rule.attr not in GROUP_ATTRS:
            continue
        label, unit = _GROUP_LABELS[rule.attr]
        out.append(MetricRule(
            rule.attr, label, unit,
            growth_ratio=1.0,                       # 그룹은 배수를 보지 않는다(위 주석)
            min_delta=rule.min_delta * multiple,
            monotonic_ratio=rule.monotonic_ratio,
        ))
    return tuple(out)


DEFAULT_WINDOW_S = 900.0
DEFAULT_MIN_DURATION_S = 300.0
DEFAULT_MIN_SAMPLES = 20
DEFAULT_COOLDOWN_S = 1800.0
# **프로세스 수가 아니라 트랙 수다** (프로세스 × 지표). 실측한 이 PC 만 해도 프로세스가
# 200종이라 지표 2개면 400 을 그대로 채운다. 상한에 닿아도 오래된 것을 버리고 새 것을
# 받지만(`_make_room`), 상한이 빠듯하면 축출이 잦아져 긴 관측이 끊긴다.
DEFAULT_MAX_TRACKED = 800
# 창 최대치의 이 비율 아래로 떨어지면 추적을 리셋한다. 정상적인 해제이거나,
# PID 가 재사용되어 **다른 프로그램의 값이 이어 붙은** 것이다. 후자를 놓치면
# 프로세스가 죽고 다시 뜬 것을 "무한히 자라는 누수"로 본다.
DEFAULT_DROP_RESET_RATIO = 0.5
# 판정·정리 주기. 누수는 분 단위 현상이라 초당 판정할 이유가 없고, 판정 한 번이
# 트랙 수백 개의 중앙값 계산이라 매 틱 돌리면 관측자가 병목이 된다.
DEFAULT_EVAL_INTERVAL_S = 30.0


@dataclass
class _Track:
    """프로세스 하나 × 지표 하나의 시계열."""

    samples: deque[tuple[float, float]] = field(default_factory=deque)
    last_fired_ts: float | None = None

    def add(self, ts: float, value: float, *, window_s: float, drop_ratio: float) -> None:
        if self.samples:
            peak = max(v for _, v in self.samples)
            # 크게 떨어졌다 = 누수가 아니다. 여기서 리셋하지 않으면 골짜기를 사이에 둔
            # 두 봉우리가 하나의 긴 상승으로 보인다.
            if peak > 0 and value < peak * drop_ratio:
                self.samples.clear()
        self.samples.append((ts, value))
        cutoff = ts - window_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    @property
    def span_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1][0] - self.samples[0][0]


@dataclass(frozen=True)
class LeakVerdict:
    """판정 결과. 발화하지 않아도 이유를 담아 돌려준다(디버깅·테스트용)."""

    leaking: bool
    first: float = 0.0
    last: float = 0.0
    ratio: float = 1.0
    delta: float = 0.0
    monotonic: float = 0.0
    duration_s: float = 0.0
    reason: str = ""


def judge(track: _Track, rule: MetricRule, *, min_duration_s: float, min_samples: int) -> LeakVerdict:
    """한 시계열이 누수인지 판정한다. 순수 함수 — 테스트가 여기를 직접 찌른다."""
    n = len(track.samples)
    if n < min_samples:
        return LeakVerdict(False, reason=f"표본 부족 ({n}/{min_samples})")
    duration = track.span_s
    if duration < min_duration_s:
        return LeakVerdict(False, duration_s=duration, reason=f"지속 부족 ({duration:.0f}s)")

    values = [v for _, v in track.samples]
    # 양 끝점 하나씩만 보면 잡음 하나에 판정이 뒤집힌다. 앞/뒤 1/4 의 중앙값을 쓴다.
    quarter = max(1, n // 4)
    first = statistics.median(values[:quarter])
    last = statistics.median(values[-quarter:])

    delta = last - first
    ratio = last / first if first > 0 else float("inf") if last > 0 else 1.0
    # "줄지 않은" 표본 비율. 누수는 계단식으로 오르므로 '증가'가 아니라 '비감소'로 센다.
    non_decreasing = sum(1 for a, b in zip(values, values[1:]) if b >= a) / (n - 1)

    verdict = LeakVerdict(
        leaking=False,
        first=first,
        last=last,
        ratio=ratio,
        delta=delta,
        monotonic=non_decreasing,
        duration_s=duration,
    )
    if delta < rule.min_delta:
        return _no(verdict, f"증가량 부족 ({delta:.0f} < {rule.min_delta:.0f})")
    if ratio < rule.growth_ratio:
        return _no(verdict, f"배수 부족 ({ratio:.2f} < {rule.growth_ratio})")
    if non_decreasing < rule.monotonic_ratio:
        return _no(verdict, f"등락함 (비감소 {non_decreasing:.2f} < {rule.monotonic_ratio})")

    return LeakVerdict(True, first, last, ratio, delta, non_decreasing, duration, "누수")


def _may_grow(track: _Track, growth_ratio: float) -> bool:
    """정식 판정을 돌려볼 가치가 있는가. **놓치는 쪽으로 틀리면 안 되는 필터다.**

    `judge` 의 `first` 는 앞 1/4 중앙값이라 최솟값 이상이고, `last` 는 뒤 1/4 중앙값이라
    최댓값 이하다. 따라서 `last/first ≤ 최대/최소` 이고, 이 상계가 문턱에 못 미치면
    정식 판정도 반드시 탈락한다 — 걸러도 탐지 결과가 달라지지 않는다.
    """
    if len(track.samples) < 2:
        return False
    lo = hi = track.samples[0][1]
    for _, v in track.samples:
        if v < lo:
            lo = v
        elif v > hi:
            hi = v
    if lo <= 0:
        return hi > 0
    return hi / lo >= growth_ratio


def _no(v: LeakVerdict, reason: str) -> LeakVerdict:
    return LeakVerdict(False, v.first, v.last, v.ratio, v.delta, v.monotonic, v.duration_s, reason)


class ProcessLeakDetector(BaseDetector):
    """프로세스별 자원이 줄지 않고 계속 자라는 것을 잡는다."""

    name = "procleak"

    def __init__(
        self,
        *,
        rules: Iterable[MetricRule] = DEFAULT_RULES,
        window_s: float = DEFAULT_WINDOW_S,
        min_duration_s: float = DEFAULT_MIN_DURATION_S,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        max_tracked: int = DEFAULT_MAX_TRACKED,
        drop_reset_ratio: float = DEFAULT_DROP_RESET_RATIO,
        eval_interval_s: float = DEFAULT_EVAL_INTERVAL_S,
        severity: SeveritySettings | None = None,
        group_rules: Iterable[MetricRule] | None = None,
    ) -> None:
        # 워밍업은 창 길이가 대신한다 — 지속 조건을 못 채우면 어차피 발화하지 않는다.
        super().__init__(warmup_s=0.0)
        self.rules = tuple(rules)
        # 그룹 룰을 안 주면 PID별 룰에서 유도한다. 두 문턱이 갈리지 않게 하는 것이
        # 이 축의 기본값이고, 끄려면 빈 튜플을 준다.
        self.group_rules = (
            tuple(group_rules) if group_rules is not None
            else group_rules_from(self.rules, multiple=DEFAULT_GROUP_MULTIPLE)
        )
        self._group_attrs = frozenset(r.attr for r in self.group_rules)
        self.window_s = window_s
        self.min_duration_s = min_duration_s
        self.min_samples = min_samples
        self.cooldown_s = cooldown_s
        self.max_tracked = max_tracked
        self.drop_reset_ratio = drop_reset_ratio
        self.eval_interval_s = eval_interval_s
        # 등급 문턱. 기본값으로 두면 단독 실행 스모크가 config 없이도 돈다.
        self.severity = severity or SeveritySettings()
        # 프로세스 지문(Phase 6-B). 비어 있으면 억제 없이 6-A 그대로 동작한다.
        self.fingerprints: dict[tuple[str, str], Any] = {}
        self._tracks: dict[tuple[int, str, str], _Track] = {}
        # 그룹 축(`GROUP_RULES`). 키는 `(group_key, metric)` — PID 가 없다.
        self._group_tracks: dict[tuple[str, str], _Track] = {}
        self._last_eval_ts: float | None = None
        self._last_maint_ts: float | None = None
        self.suppressed = 0

    def reset(self) -> None:
        super().reset()
        self._tracks.clear()
        self._group_tracks.clear()
        self._last_eval_ts = None
        self._last_maint_ts = None
        self.suppressed = 0
        # 지문은 상태가 아니라 학습 결과다. reset 으로 버리면 리플레이를 두 번 돌릴 때
        # 결과가 달라진다(결정론 위반).

    def warm(self, db, now: float) -> int:
        """지문을 읽어 둔다. `DetectionComponent` 가 기동 시 불러 준다.

        지문이 없어도 탐지는 그대로 돈다 — 억제만 없을 뿐이다.
        """
        from .fingerprint import load

        self.fingerprints = load(db)
        return len(self.fingerprints)

    def _due(self, attr: str, ts: float) -> bool:
        """`ts` 기준으로 주기가 됐는가. **벽시계를 쓰지 않는다**(리플레이 계약)."""
        last = getattr(self, attr)
        if last is None or ts - last >= self.eval_interval_s:
            setattr(self, attr, ts)
            return True
        return False

    # ------------------------------------------------------------------ 학습

    def learn(self, obs: Observation) -> None:
        # **정리를 먼저 한다.** 나중에 하면 죽은 프로세스가 자리를 붙들고 있는 채로
        # 새 프로세스를 거절하게 된다. 다만 **매 틱 할 일은 아니다** — 트랙 전체를
        # 훑는 작업이라 1초마다 돌리면 관측자가 무거워진다(설계 규칙 1).
        if self._due("_last_maint_ts", obs.ts):
            self._evict(obs.ts)
            self._enforce_limit()

        for proc in obs.processes:
            for rule in self.rules:
                value = getattr(proc, rule.attr, None)
                if value is None:
                    continue
                key = (proc.pid, proc.name, rule.attr)
                track = self._tracks.get(key)
                if track is None:
                    track = _Track()
                    self._tracks[key] = track
                track.add(
                    obs.ts,
                    float(value),
                    window_s=self.window_s,
                    drop_ratio=self.drop_reset_ratio,
                )

        self._learn_groups(obs)

    def _learn_groups(self, obs: Observation) -> None:
        """그룹 축 갱신 — 같은 프로그램의 **증가분을 합친다**.

        PID별 트랙이 이미 각자의 첫 관측값을 갖고 있으므로 `현재값 - 첫값` 을 더하면
        된다. 새 PID 는 그 차가 0 이라 **합계에 계단을 만들지 않는다**(위 `GROUP_RULES`
        주석 참조).
        """
        from ..explain.attribution import group_key

        totals: dict[tuple[str, str], float] = {}
        for (pid, name, attr), track in self._tracks.items():
            if attr not in self._group_attrs or not track.samples:
                continue
            grown = track.samples[-1][1] - track.samples[0][1]
            if grown <= 0:
                continue        # 줄어든 프로세스가 남의 증가를 상쇄하면 안 된다
            key = (group_key(name, pid), attr)
            totals[key] = totals.get(key, 0.0) + grown

        for key, value in totals.items():
            track = self._group_tracks.get(key)
            if track is None:
                track = _Track()
                self._group_tracks[key] = track
            track.add(obs.ts, value, window_s=self.window_s,
                      drop_ratio=self.drop_reset_ratio)

        # PID별과 달리 그룹은 관측이 끊겨도 키가 사라지지 않으므로 직접 정리한다.
        stale = [k for k, t in self._group_tracks.items()
                 if not t.samples or t.samples[-1][0] < obs.ts - self.window_s]
        for key in stale:
            del self._group_tracks[key]

    def _evict(self, now: float) -> None:
        """창 밖으로 완전히 나간 트랙을 버린다. 죽은 프로세스가 메모리에 쌓이지 않게."""
        stale = [k for k, t in self._tracks.items() if not t.samples or t.samples[-1][0] < now - self.window_s]
        for key in stale:
            del self._tracks[key]

    def _enforce_limit(self) -> None:
        """상한 초과분을 **틱 경계에서 한 번에** 정리한다. 버릴 것은 가장 평평한 트랙이다.

        세 가지를 동시에 지켜야 해서 이 모양이 됐다. 셋 다 실측으로 하나씩 드러났다.

        1. **자리가 없다고 새 프로세스를 거절하면 안 된다.** 프로세스가 200종인 PC 는
           기동 직후 상한이 차 버리고, 그 뒤에 뜨는 프로세스는 영영 관측되지 않는다.
           누수는 대개 새로 뜬 프로세스에서 생기므로 봐야 할 것을 정확히 못 보게 된다.
           2026-07-29 결함주입에서 이것 때문에 주입 프로세스를 통째로 놓쳤다.
        2. **버릴 것은 자라지 않는 트랙이다.** "가장 오래 갱신 안 된 것"(LRU)을 버리면,
           대부분의 프로세스가 같은 틱에 함께 갱신되는 탓에 순서가 사실상 삽입 순이 되어
           **누수 중인 트랙까지 밀려난다.** 평평한 트랙은 어차피 발화하지 않는다.
        3. **축출은 순회 도중에 하면 안 된다.** 프로세스를 훑으면서 자리가 없을 때마다
           축출하면, 쫓겨난 프로세스가 다음 순번에서 자리를 되찾으며 그다음 것을 쫓아내는
           **도미노**가 돈다. 실측에서 한 틱에 축출이 201회 일어나며 전체 트랙이 매 틱
           리셋됐다 — 상한에 딱 닿기만 해도 탐지기가 통째로, 그것도 조용히 무력화된다.
           그래서 틱이 시작할 때 한 번만 정리하고, 그 틱 동안은 잠시 초과를 허용한다.
        """
        over = len(self._tracks) - self.max_tracked
        if over <= 0:
            return

        def flatness(key) -> tuple[float, float]:
            track = self._tracks[key]
            if not track.samples:
                return (0.0, 0.0)
            born = track.samples[0][0]
            if len(track.samples) < 2:
                return (0.0, born)
            values = [v for _, v in track.samples]
            first, last = values[0], values[-1]
            growth = (last - first) / first if first > 0 else (last - first)
            # 동률이면 **먼저 생긴 것**을 버린다. "오래 갱신되지 않은 것"으로 가르면,
            # 한 틱 안에서 프로세스를 순서대로 훑는 동안 아직 차례가 오지 않은 트랙이
            # 전부 '오래된 것'으로 보인다. 그러면 순번이 마지막인 트랙(= 그 틱에 새로
            # 등장한 프로세스)이 매번 희생되어 표본을 영영 못 쌓는다.
            return (max(0.0, growth), born)

        for key in sorted(self._tracks, key=flatness)[:over]:
            del self._tracks[key]

    # ------------------------------------------------------------------ 판정

    def _fingerprint_for(self, name: str, rule: MetricRule):
        """이 (프로그램, 지표) 의 지문. 없으면 None.

        억제 판정과 등급 판정이 **같은 지문**을 봐야 한다. 조회를 두 곳에 적으면
        한쪽만 고쳤을 때 "억제는 안 하는데 등급은 낮게 주는" 식으로 조용히 갈린다.
        """
        from .fingerprint import STAT_FOR

        stat = STAT_FOR.get(rule.attr)
        return self.fingerprints.get((name, stat)) if stat else None

    def _is_normal_for(self, name: str, rule: MetricRule, verdict: LeakVerdict) -> bool:
        """이 프로그램에게는 평소 수준인가 (Phase 6-B 지문 억제).

        6-A 는 자기 과거만 보므로 "평소에도 핸들을 4천 개씩 쓰는 프로그램"과 "평소
        200개인데 4천 개가 된 프로그램"을 구분하지 못한다. 실측에서 medal(게임 녹화)이
        핸들 383 → 1,395 로 늘어 발화했는데 medal 의 평소 p99 는 12,466 이었다.

        **억제는 한쪽으로만 작동한다.** 지문이 없으면(신규·희귀 프로세스) 아무것도 하지
        않는다 — 모르는 것을 막는 방향으로는 틀지 않는다. 같은 실측에서 주입 프로세스는
        평소 p99(2,768)를 훌쩍 넘겨 그대로 발화했다.
        """
        fp = self._fingerprint_for(name, rule)
        if fp is None:
            return False
        if not fp.within_normal(verdict.last):
            return False
        stat = fp.stat

        # 조용히 삼키지 않는다. 무엇을 왜 막았는지 남긴다(설계 규칙 4).
        #
        # **키 이름이 `process` 면 안 된다.** `LogRecord` 가 이미 그 속성을 쓰고 있어
        # (프로세스 ID) `extra` 로 덮어쓰면 `KeyError` 가 난다. INFO 에서는 `log.debug` 가
        # 조기 반환해 드러나지 않지만, `log_level: DEBUG` 로 바꾸는 순간 억제가 일어날 때
        # **탐지 스레드가 죽는다.** 억제를 보이게 만들려고 넣은 코드가 보이게 하면 터졌다
        # (2026-07-30, 리플레이 조사 중 실제로 터졌다).
        log.debug(
            "지문 억제 — 이 프로그램에게는 평소 수준",
            extra={"proc_name": name, "metric": rule.attr, "reached": round(verdict.last, 1),
                   "normal_p99": round(fp.p99, 1), "stat": stat},
        )
        self.suppressed += 1
        return True

    def evaluate(self, obs: Observation) -> Detection | None:
        # **매 틱 판정하지 않는다.** 누수는 분 단위 현상이라 초당 판정할 이유가 없는데,
        # 판정 한 번은 추적 중인 트랙 전부(수백 개)에 대해 표본 전체의 중앙값을 내는
        # 일이다. 1초마다 돌렸더니 24시간 리플레이가 20분 넘게 CPU 를 먹고도 끝나지
        # 않았다 — 실시간에서도 CPU 예산 2% 를 지킬 수 없다는 뜻이다(설계 규칙 1).
        if not self._due("_last_eval_ts", obs.ts):
            return None

        best: tuple[float, dict[str, Any]] | None = None
        by_attr = {r.attr: r for r in self.rules}
        # 한 틱에 여러 프로세스가 걸릴 수 있다. 가장 심한 하나만 신호로 낸다 —
        # 같은 순간에 신호 열 개를 쏟으면 융합 단계(Phase 9)가 사건 하나를 못 만든다.
        for (pid, name, attr), track in self._tracks.items():
            rule = by_attr.get(attr)
            if rule is None:
                continue
            if track.last_fired_ts is not None and obs.ts - track.last_fired_ts < self.cooldown_s:
                continue
            # 값싼 사전 배제. 중앙값을 내기 전에 O(n) 한 번으로 명백히 아닌 것을 거른다.
            # 앞 1/4 중앙값은 최솟값 이상이고 뒤 1/4 중앙값은 최댓값 이하이므로,
            # `최대/최소` 가 배수 문턱에 못 미치면 정식 판정도 반드시 탈락한다.
            if not _may_grow(track, rule.growth_ratio):
                continue
            verdict = judge(
                track, rule, min_duration_s=self.min_duration_s, min_samples=self.min_samples
            )
            if not verdict.leaking:
                continue
            if self._is_normal_for(name, rule, verdict):
                continue
            score = min(1.0, verdict.ratio / (rule.growth_ratio * 2.0))
            if best is None or score > best[0]:
                best = (score, {"pid": pid, "name": name, "rule": rule, "verdict": verdict, "track": track})

        best = self._evaluate_groups(obs, best)

        if best is None:
            return None


        score, hit = best
        rule: MetricRule = hit["rule"]
        v: LeakVerdict = hit["verdict"]
        hit["track"].last_fired_ts = obs.ts

        if hit.get("group"):
            # 그룹은 배수를 말하지 않는다(증가분 합이라 초기에 발산한다). 대신
            # **몇 개로 흩어져 있는지**를 말한다 — 그게 이 축이 잡은 이유다.
            explain = (
                f"{hit['name']} {hit['count']}개 프로세스 합계 {rule.label} "
                f"+{v.delta:,.0f}{rule.unit} "
                f"({v.duration_s / 60:.0f}분간 줄지 않음, 개별로는 문턱 미달)"
            )
        else:
            explain = (
                f"{hit['name']} (PID {hit['pid']}) {rule.label} "
                f"{v.first:,.0f} → {v.last:,.0f}{rule.unit} "
                f"({v.ratio:.1f}배, {v.duration_s / 60:.0f}분간 줄지 않음)"
            )

        # 등급은 **자기 p99 대비 위치**로 정한다. 지금까지는 warning 고정이라 누수
        # 규모와 무관했다 — 실측에서 평소 상한의 3.5배로 자라는 주입과, 평소의 절반도
        # 안 되는 medal 이 같은 등급이었다. 배수(`ratio`)를 쓰지 않는 이유는 그것이
        # 심각도와 무관했기 때문이다: 가장 높은 배수(27.8)가 정상 프로세스였다.
        fp = self._fingerprint_for(hit["name"], rule)
        severity, risk_reason = leak_risk(
            last=v.last,
            fingerprint_p99=fp.p99 if fp is not None else None,
            monotonic=v.monotonic,
            settings=self.severity,
        )
        return self.detect(
            obs,
            score,
            severity=severity,
            severity_reason=risk_reason,
            rule=f"{rule.label} 누수",
            process=hit["name"],
            pid=hit["pid"],
            metric=rule.attr,
            first=round(v.first, 1),
            last=round(v.last, 1),
            ratio=round(v.ratio, 2),
            delta=round(v.delta, 1),
            monotonic=round(v.monotonic, 3),
            duration_s=round(v.duration_s, 1),
            explain=explain,
        )

    def _evaluate_groups(self, obs: Observation, best):
        """그룹 축 판정. PID별 결과(`best`)와 견주어 더 높은 쪽을 돌려준다."""
        from ..explain.attribution import group_key

        by_attr = {r.attr: r for r in self.group_rules}
        for (gkey, attr), track in self._group_tracks.items():
            rule = by_attr.get(attr)
            if rule is None:
                continue
            if track.last_fired_ts is not None and obs.ts - track.last_fired_ts < self.cooldown_s:
                continue
            verdict = judge(
                track, rule, min_duration_s=self.min_duration_s, min_samples=self.min_samples
            )
            if not verdict.leaking:
                continue

            # 이 그룹에 속한 PID 들과, 그중 가장 많이 자란 것(대표). 소비자가 `pid` 를
            # 기대하므로 비워 두지 않는다.
            members = [
                (t.samples[-1][1] - t.samples[0][1], pid, name)
                for (pid, name, a), t in self._tracks.items()
                if a == attr and t.samples and group_key(name, pid) == gkey
            ]
            if len(members) < 2:
                # 프로세스가 하나뿐이면 PID별 축이 이미 보고 있다. 같은 일을 두 번
                # 신고하면 융합 단계가 사건을 둘로 만든다.
                continue
            members.sort(reverse=True)
            _, top_pid, top_name = members[0]

            # 점수는 배수가 아니라 증가량으로 낸다(그룹은 배수를 보지 않는다).
            score = min(1.0, verdict.delta / (rule.min_delta * 2.0))
            if best is None or score > best[0]:
                best = (score, {
                    "pid": top_pid, "name": top_name, "rule": rule,
                    "verdict": verdict, "track": track,
                    "group": True, "count": len(members),
                })
        return best


# 지표별 표시 정보. 임계값은 config 에서 오고, 여기 있는 것은 이름표뿐이다.
_LABELS = {"handles": ("핸들", "개"), "rss_mb": ("메모리", "MB")}


def rules_from_settings(settings: Any = None, profile: Any = None) -> tuple[MetricRule, ...]:
    """config + 머신 프로필에서 판정 기준을 만든다.

    `min_delta_ram_ratio` 가 있으면 절대값 대신 **이 PC 의 RAM 비율**을 쓴다. 512MB
    증가는 4GB PC 에서 심각하고 64GB PC 에서는 브라우저 탭 몇 개다 — 절대값을 박으면
    둘 중 한쪽에서 반드시 틀린다(설계 규칙 2).
    """
    from ..config.loader import load_settings
    from ..machine.calibration import load_profile

    settings = settings if settings is not None else load_settings().process_leak
    profile = profile if profile is not None else load_profile()

    total_mb = 0.0
    if profile is not None:
        try:
            total_mb = float(profile.memory.get("total_gb") or 0.0) * 1024.0
        except (AttributeError, TypeError, ValueError):
            total_mb = 0.0

    out = []
    for attr, (label, unit) in _LABELS.items():
        cfg = getattr(settings, attr, None)
        if cfg is None:
            continue
        min_delta = float(cfg.min_delta)
        ratio = getattr(cfg, "min_delta_ram_ratio", None)
        if ratio and total_mb > 0:
            # 프로필이 없으면(캘리브레이션 전) 절대값으로 물러선다 — 조용히 0 이 되면
            # 문턱이 사라져 오탐이 쏟아진다.
            min_delta = max(128.0, total_mb * float(ratio))
        out.append(
            MetricRule(
                attr, label, unit,
                growth_ratio=float(cfg.growth_ratio),
                min_delta=min_delta,
                monotonic_ratio=float(cfg.monotonic_ratio),
            )
        )
    return tuple(out) or DEFAULT_RULES


def build() -> ProcessLeakDetector:
    """레지스트리용 생성자. 설정을 못 읽어도 기본값으로 돈다."""
    from ..config.loader import load_settings

    try:
        cfg = load_settings().process_leak
        rules = rules_from_settings(cfg)
        group = cfg.group
        return ProcessLeakDetector(
            rules=rules,
            group_rules=(
                group_rules_from(rules, multiple=group.min_delta_multiple)
                if group.enabled else ()
            ),
            window_s=cfg.window_s,
            min_duration_s=cfg.min_duration_s,
            min_samples=cfg.min_samples,
            cooldown_s=cfg.cooldown_s,
            max_tracked=cfg.max_tracked,
            drop_reset_ratio=cfg.drop_reset_ratio,
            eval_interval_s=cfg.eval_interval_s,
            severity=load_settings().severity,
        )
    except Exception as exc:  # 설정 오류가 탐지기 로드를 막으면 안 된다
        log.warning("process_leak 설정을 읽지 못해 기본값을 쓴다", extra={"error": str(exc)})
        return ProcessLeakDetector()


if __name__ == "__main__":  # 스모크: python -m argus.detection.procleak
    def stream(values, *, pid=999, name="leaky", attr="handles", step=1.0, start=1000.0):
        out = []
        for i, v in enumerate(values):
            kwargs = {attr: v}
            out.append(
                Observation(ts=start + i * step, processes=[ProcessView(pid=pid, name=name, **kwargs)])
            )
        return out

    from .base import run_detector

    # 1) 진짜 누수: 400개에서 6400개까지 계단식으로 오른다 (10분, 1초 간격)
    leak = stream([400 + i * 10 for i in range(600)])
    fired = run_detector(ProcessLeakDetector(), leak)
    print(f"  누수 스트림 600틱 → 신호 {len(fired)}건")
    if fired:
        print(f"    {fired[0].features['explain']}  score={fired[0].score:.2f}")

    # 2) 정상 등락: 같은 범위를 오가지만 자라지 않는다. 여기서 울면 오탐이다.
    import random
    random.seed(11)
    noisy = stream([500 + random.randint(-150, 150) for _ in range(600)])
    false_alarms = run_detector(ProcessLeakDetector(), noisy)
    print(f"  정상 등락 600틱 → 신호 {len(false_alarms)}건 (0 이어야 정상)")

    # 3~6) **조건을 정확히 하나씩만 위반하는 케이스.**
    # 여러 조건을 동시에 어기는 케이스만 두면, 조건 하나를 지워도 나머지가 막아 줘서
    # 그 조건이 검증되지 않는다. 실제로 2026-07-29 첫 판에 네 개가 그 상태였다.

    # 3) 배수만 부족: 5000 → 6200. 단조 증가이고 증가량(900)도 충분하지만 1.2배다.
    #    이미 핸들을 많이 쥔 프로그램이 조금 더 쥐는 것은 누수가 아니다.
    big_base = stream([5000 + i * 2 for i in range(600)])
    ratio_only = run_detector(ProcessLeakDetector(), big_base)
    print(f"  배수만 부족 (5000→6200) → 신호 {len(ratio_only)}건 (0 이어야 정상)")

    # 4) 증가량만 부족: 10 → 100. 10배지만 핸들 90개는 누수라 부를 양이 아니다.
    tiny = stream([10 + i * 0.15 for i in range(600)])
    delta_only = run_detector(ProcessLeakDetector(), tiny)
    print(f"  증가량만 부족 (10→100) → 신호 {len(delta_only)}건 (0 이어야 정상)")

    # 5) 단조성만 부족: 400 → 4000 이지만 매 틱 크게 오르내린다.
    #    누수는 계단식으로 오른다. 등락하며 오르는 것은 그 프로그램이 일하는 중이다.
    # 진폭은 급락 리셋 문턱(peak 의 50%) 안쪽으로 둔다. 더 크게 흔들면 리셋이 먼저
    # 걸려 단조성 판정까지 가지도 못하고, 그러면 이 케이스는 단조성을 검증하지 않는다.
    sawtooth = stream([(400 + i * 6) * (1.2 if i % 2 else 0.8) for i in range(600)])
    mono_only = run_detector(ProcessLeakDetector(), sawtooth)
    print(f"  단조성만 부족 (톱니 400→4000) → 신호 {len(mono_only)}건 (0 이어야 정상)")

    # 4) PID 재사용. 10분간 3000까지 간 프로세스가 죽고, 같은 PID 로 뜬 다른 프로세스가
    #    3분 만에 100 → 3100 으로 오른다. **뒤엣것은 3분짜리라 발화하면 안 된다.**
    #    리셋이 없으면 앞 프로세스의 이력이 이어 붙어 "13분간 오르는 중"으로 보인다.
    reused = stream(
        [100 + i * 10 for i in range(300)]        # 앞 프로세스: 100 → 3090
        + [100 + i * 17 for i in range(180)]      # 재사용된 PID: 100 → 3143 (3분)
    )
    # 앞 프로세스가 정당하게 발화하는 구간은 빼고, 재사용 이후만 본다.
    reuse_hits = [d for d in run_detector(ProcessLeakDetector(), reused) if d.ts >= 1000.0 + 300]
    print(f"  PID 재사용 후 3분 급증 → 신호 {len(reuse_hits)}건 (0 이어야 정상)")

    # 6) 지속만 부족: 2분 만에 400 → 4000. 배수·증가량·단조성 모두 충분하다.
    #    프로그램을 켜면 자원은 원래 급히 는다. 짧은 급증은 누수가 아니다.
    short = stream([400 + i * 30 for i in range(120)])
    short_hits = run_detector(ProcessLeakDetector(), short)
    print(f"  지속만 부족 (2분에 400→4000) → 신호 {len(short_hits)}건 (0 이어야 정상)")

    # 6) 쿨다운: 30분 창에서 두 번 이상 울면 안 된다.
    long_leak = stream([400 + i * 10 for i in range(1800)])
    repeats = run_detector(ProcessLeakDetector(cooldown_s=1800.0), long_leak)
    print(f"  30분 지속 누수 → 신호 {len(repeats)}건 (쿨다운으로 소수여야 정상)")

    problems = []
    if not fired:
        problems.append("명백한 핸들 누수를 잡지 못했다")
    if false_alarms:
        problems.append(f"정상 등락에서 {len(false_alarms)}건 오탐 — 알림을 끄게 만든다")
    if ratio_only:
        problems.append(f"배수 조건이 검증되지 않는다: 5000→6200 에서 {len(ratio_only)}건")
    if delta_only:
        problems.append(f"증가량 조건이 검증되지 않는다: 10→100 에서 {len(delta_only)}건")
    if mono_only:
        problems.append(f"단조성 조건이 검증되지 않는다: 톱니파에서 {len(mono_only)}건")
    if short_hits:
        problems.append(f"지속 조건이 검증되지 않는다: 2분 급증에서 {len(short_hits)}건")
    if reuse_hits:
        problems.append(f"급락 리셋이 없다: PID 재사용 뒤 3분 급증에서 {len(reuse_hits)}건")
    if len(repeats) > 2:
        problems.append(f"쿨다운이 듣지 않는다: 30분에 {len(repeats)}건")
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.procleak")

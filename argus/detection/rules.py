"""룰 엔진 — 베이스라인 위에서 "평소와 다름"을 문장으로 바꾼다.

**모든 룰은 지속 조건(`for`)과 재발화 억제(`cooldown`)를 가진다.** 선택이 아니라 강제다.
순간 스파이크는 이상이 아니다 — CPU 가 1초 100% 인 것은 프로그램을 켤 때마다 일어난다.
그걸 알리면 사용자는 사흘 안에 알림을 끄고, 그 순간 제품은 죽는다.

**임계값은 상대값으로 쓴다.** `value` 에 숫자 대신 식을 넣을 수 있고, 그 안에서
`median`·`sigma`·`baseline` 을 참조한다. `median + 4 * sigma` 는 이 PC 의 평소 대비
4σ 라는 뜻이라 HDD 든 NVMe 든 같은 의미를 갖는다.

    - name: 디스크 병목
      when:
        all:
          - {metric: disk_resp_ms, op: ">", value: "median + 5 * sigma"}
          - {metric: disk_queue,   op: ">", value: 2}
      for: 30s
      cooldown: 10m
      severity: warning
      explain: "디스크 응답 {disk_resp_ms}ms (평소 {disk_resp_ms.median}ms)"

**값을 모르면 발화하지 않는다.** GPU 가 없는 PC 에서 `gpu_temp` 는 0도가 아니라
모르는 값이고, 모르는 것을 근거로 알리면 그게 곧 오탐이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

import yaml

from ..logging_setup import get_logger
from ..paths import resource_path
from .base import (
    SEVERITY_ORDER,
    SEVERITY_WARNING,
    BaseDetector,
    Detection,
    Observation,
)
from .baseline import BaselineSet, Stats
from .expr import ExprError, compile_expr, evaluate

log = get_logger(__name__)

_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """같은 경고를 매 틱 쏟지 않는다. 룰 평가는 초당 여러 번 돈다."""
    if message not in _warned:
        _warned.add(message)
        log.warning(message)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}

# z 가 이 값이면 점수 1.0 으로 본다. 8σ 는 정규분포에서 사실상 불가능한 편차다.
_SCORE_SATURATION_Z = 8.0


def parse_duration(value: Any, *, field_name: str) -> float:
    """`30s` / `10m` / `1.5h` / 숫자(초) → 초."""
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION.match(str(value))
    if not match:
        raise RuleError(f"{field_name} 형식을 알 수 없습니다: {value!r} (예: 30s, 10m)")
    return float(match.group(1)) * _UNITS[(match.group(2) or "s").lower()]


class RuleError(ValueError):
    """룰 파일이 잘못됐다. 로드 시점에 터뜨려 런타임에 조용히 죽지 않게 한다."""


@dataclass
class Condition:
    metric: str
    op: str
    value: Any                     # 숫자 또는 표현식 문자열
    _compiled: Any = field(default=None, repr=False)

    _OPS = {">", ">=", "<", "<=", "==", "!=", "contains", "not_contains"}

    def __post_init__(self) -> None:
        if self.op not in self._OPS:
            raise RuleError(f"알 수 없는 연산자: {self.op} (가능: {', '.join(sorted(self._OPS))})")
        if isinstance(self.value, str) and self.op not in ("contains", "not_contains"):
            try:
                self._compiled = compile_expr(self.value)
            except ExprError as exc:
                raise RuleError(f"조건 표현식 오류 ({self.metric}): {exc}") from exc

    def evaluate(self, obs: Observation, baselines: BaselineSet) -> bool | None:
        """참/거짓, 또는 판정 불가면 None."""
        actual = obs.metrics.get(self.metric)
        if actual is None:
            return None

        if self.op in ("contains", "not_contains"):
            has = str(self.value).lower() in str(actual).lower()
            return has if self.op == "contains" else not has

        stats = baselines.stats(self.metric, obs.foreground_program)
        if self._compiled is not None:
            if stats is None:
                return None      # 베이스라인이 아직 없으면 상대 임계값을 못 만든다
            try:
                threshold = evaluate(self._compiled, _variables(stats, obs))
            except ExprError as e:
                # **조용히 통과시키지 않는다.** 대개 머신 프로파일이 없어 `cores`·
                # `ram_mb` 를 못 채운 경우다(규칙 4: 없으면 드러내 놓고 비활성화).
                # 이 룰만 판정 불가로 두고 나머지는 계속 돈다.
                _warn_once(f"룰 표현식을 평가할 수 없다: {self.value!r} — {e}")
                return None
            if threshold is None:
                return None      # 산포가 없는 메트릭 등 — 판정하지 않는다
        else:
            threshold = float(self.value)

        try:
            actual = float(actual)
        except (TypeError, ValueError):
            return None

        if self.op == ">":
            return actual > threshold
        if self.op == ">=":
            return actual >= threshold
        if self.op == "<":
            return actual < threshold
        if self.op == "<=":
            return actual <= threshold
        if self.op == "==":
            return actual == threshold
        return actual != threshold


@lru_cache(maxsize=1)
def machine_variables() -> dict[str, float]:
    """이 PC 의 능력값. 룰이 절대값 대신 이것으로 문턱을 표현한다(규칙 2).

    `ctx_switches_ps > 50000` 은 12코어 기준으로 정한 값이라 4코어 PC 에서는 과하고,
    `swap_used_mb > 512` 는 RAM 4GB 에서 흔한 값이다. **실측에서 이 PC(12코어)의
    컨텍스트 스위치 중앙값이 64,566 이었다 — 문턱 50,000 이 정상값 아래에 있었다.**
    코어당·RAM 대비로 쓰면 어느 기계에서든 같은 뜻이 된다.

    프로파일은 90일 재사용이라 실행 중 바뀌지 않으므로 캐시한다. 없으면 빈 값을
    돌려주고, 그것을 참조하는 룰은 평가되지 않는다(조용히 통과시키지 않는다).
    """
    try:
        from ..machine.calibration import ensure_profile

        profile = ensure_profile()
    except Exception:  # 프로파일이 아직 없거나 읽을 수 없는 환경(테스트·리플레이)
        return {}

    cores = float(profile.cpu.get("logical") or 0)
    ram_gb = float(profile.memory.get("total_gb") or 0)
    swap_gb = float(profile.memory.get("swap_total_gb") or 0)
    out: dict[str, float] = {}
    if cores > 0:
        out["cores"] = cores
        out["physical_cores"] = float(profile.cpu.get("physical") or cores)
    if ram_gb > 0:
        out["ram_gb"] = ram_gb
        out["ram_mb"] = ram_gb * 1024.0
    if swap_gb > 0:
        out["swap_total_mb"] = swap_gb * 1024.0
    return out


def _variables(stats: Stats, obs: Observation) -> dict[str, Any]:
    """표현식에서 쓸 수 있는 이름들."""
    variables: dict[str, Any] = {
        "median": stats.median,
        "baseline": stats.median,   # 읽기 좋은 별칭
        "mad": stats.mad,
        "sigma": stats.sigma,
        "min": stats.minimum,
        "max": stats.maximum,
        **machine_variables(),
    }
    # 다른 메트릭의 현재 값도 참조할 수 있게 한다 (예: cpu_total 대비 판단)
    for name, value in obs.metrics.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            variables.setdefault(name, float(value))
    return variables


@dataclass
class Rule:
    name: str
    conditions: list[Condition]
    mode: str = "all"                     # all | any
    for_s: float = 30.0
    cooldown_s: float = 600.0
    severity: str = SEVERITY_WARNING
    explain: str = ""
    regime_scope: list[str] = field(default_factory=lambda: ["ANY"])

    def __post_init__(self) -> None:
        if self.mode not in ("all", "any"):
            raise RuleError(f"when 은 all 또는 any 여야 합니다: {self.mode}")
        if self.severity not in SEVERITY_ORDER:
            raise RuleError(f"알 수 없는 severity: {self.severity}")
        if not self.conditions:
            raise RuleError(f"조건이 없는 룰입니다: {self.name}")

    @property
    def metrics(self) -> list[str]:
        return [c.metric for c in self.conditions]

    def evaluate(self, obs: Observation, baselines: BaselineSet) -> bool | None:
        results = [c.evaluate(obs, baselines) for c in self.conditions]
        if self.mode == "all":
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        # any: 하나라도 확실히 참이면 참
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False


def relative_metrics(rules: Sequence[Rule]) -> set[str]:
    """베이스라인을 **실제로 쓰는** 메트릭. 조건부 축을 여기에만 둔다.

    절대 조건(`disk_queue > 2` 처럼 단위가 하드웨어 독립적인 것)은 프로그램별로
    나눠 봐야 쓰이지 않는데, 나누면 메모리는 프로그램 수만큼 곱해진다.
    실측(2026-08-04)에서 전체 9개 중 상대 조건은 6개였다.
    """
    return {
        c.metric
        for rule in rules
        for c in rule.conditions
        if c._compiled is not None  # noqa: SLF001 — 표현식이 컴파일된 것이 곧 상대 조건이다
    }


def load_rules(path: Any = None) -> list[Rule]:
    """룰 파일을 읽고 **전부 검증한다.**

    잘못된 룰 하나가 로드 시점에 터지게 하는 것이 의도다. 런타임에 처음 평가될 때
    터지면 몇 시간 뒤 조용히 탐지가 멈춘 것을 나중에야 알게 된다.
    """
    source = path if path is not None else resource_path("config/rules.yaml")
    try:
        with open(source, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        log.warning("룰 파일이 없다 — 룰 탐지 비활성", extra={"path": str(source)})
        return []

    rules: list[Rule] = []
    for index, entry in enumerate(data.get("rules") or []):
        name = entry.get("name") or f"rule[{index}]"
        try:
            when = entry.get("when") or {}
            mode = "all" if "all" in when else "any" if "any" in when else None
            if mode is None:
                raise RuleError("when 아래에 all 또는 any 가 필요합니다")
            conditions = [Condition(**c) for c in when[mode]]

            scope = entry.get("regime_scope") or ["ANY"]
            if [s for s in scope if s != "ANY"]:
                # 레짐은 Phase 4 다. 받아는 두되 무시하고, 무시했다고 말한다.
                log.info(
                    "regime_scope 는 Phase 4 에서 적용된다 — 지금은 ANY 로 취급",
                    extra={"rule": name, "scope": scope},
                )

            rules.append(Rule(
                name=name,
                conditions=conditions,
                mode=mode,
                for_s=parse_duration(entry.get("for", "30s"), field_name="for"),
                cooldown_s=parse_duration(entry.get("cooldown", "10m"), field_name="cooldown"),
                severity=entry.get("severity", SEVERITY_WARNING),
                explain=entry.get("explain", ""),
                regime_scope=scope,
            ))
        except (RuleError, TypeError) as exc:
            raise RuleError(f"룰 '{name}' 을(를) 읽을 수 없습니다: {exc}") from exc

    log.info("룰 로드", extra={"count": len(rules), "path": str(source)})
    return rules


def build() -> "RuleEngine":
    """레지스트리용 생성자. 설정을 못 읽어도 기본값으로 돈다.

    **2026-08-04 까지 레지스트리에 `RuleEngine` 클래스가 그대로 등록돼 있어
    `detection.*` 설정이 룰 엔진에 전달된 적이 없다.** 코드 기본값과 YAML 기본값이
    우연히 같아서(1800초/60표본) 아무 신호도 없었다 — `baseline_window_s` 를 고쳐도
    판정이 안 바뀌는 상태였다(규칙 3 위반). `procleak` 은 처음부터 이 형태였다.
    """
    from ..config.loader import load_settings

    try:
        cfg = load_settings().detection
        return RuleEngine(
            window_s=cfg.baseline_window_s,
            min_samples=cfg.min_samples,
            per_program=cfg.per_program,
            program_window_s=cfg.program_window_s,
            program_min_interval_s=cfg.program_min_interval_s,
            program_min_samples=cfg.program_min_samples,
            max_programs=cfg.max_programs,
        )
    except Exception as exc:  # 설정 오류가 탐지기 로드를 막으면 안 된다
        log.warning("detection 설정을 읽지 못해 기본값을 쓴다", extra={"error": str(exc)})
        return RuleEngine()


class RuleEngine(BaseDetector):
    """룰 전체를 하나의 탐지기로 본다.

    룰마다 별도 탐지기로 만들면 스코어보드에 20줄이 생기고 비교가 불가능해진다.
    사용자에게도 "룰 엔진이 잡았다" 하나면 된다 — 어느 룰인지는 `features` 에 있다.
    """

    name = "rules"

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        *,
        window_s: float = 1800.0,
        min_samples: int = 60,
        warmup_s: float = 0.0,
        per_program: bool = False,
        program_window_s: float = 3600.0,
        program_min_interval_s: float = 5.0,
        program_min_samples: int = 60,
        max_programs: int = 16,
    ) -> None:
        super().__init__(warmup_s=warmup_s)
        self.rules = list(rules) if rules is not None else load_rules()
        self.baselines = BaselineSet(
            window_s=window_s,
            min_samples=min_samples,
            per_program=per_program,
            # **목록을 코드에 박지 않고 룰에서 뽑는다**(규칙 3). 상대 조건을 쓰는
            # 메트릭만 조건부화하면 되고, 룰이 바뀌면 이 목록도 따라가야 한다.
            program_metrics=relative_metrics(self.rules),
            program_window_s=program_window_s,
            program_min_interval_s=program_min_interval_s,
            program_min_samples=program_min_samples,
            max_programs=max_programs,
        )
        self._since: dict[str, float] = {}      # 룰 이름 → 조건이 참이 된 시각
        self._last_fired: dict[str, float] = {}

    def reset(self) -> None:
        super().reset()
        self.baselines.reset()
        self._since.clear()
        self._last_fired.clear()

    def observe(self, obs: Observation) -> Detection | None:
        # GPU 지표는 `obs.gpus` 에 장치별 리스트로 온다. 룰에서 `gpu_temp_c` 처럼
        # 평평한 이름으로 쓰게 펼쳐 준다. 규칙은 `Observation` 이 갖고 있다 —
        # 베이스라인 워밍도 같은 규칙을 써야 한다.
        return super().observe(obs.flatten_gpus())

    def learn(self, obs: Observation) -> None:
        self.baselines.observe(obs.ts, obs.metrics, obs.foreground_program)

    def evaluate(self, obs: Observation) -> Detection | None:
        if not self.baselines.ready:
            return None      # 부트스트랩 — 베이스라인이 서기 전에는 판정하지 않는다

        fired: list[tuple[Rule, float]] = []
        for rule in self.rules:
            state = rule.evaluate(obs, self.baselines)
            if state is not True:
                # 거짓이든 판정 불가든 지속 시계를 끊는다. 판정 불가 구간을 참으로
                # 이어 주면 "30초 지속"이 실제로는 관측되지 않은 30초가 된다.
                self._since.pop(rule.name, None)
                continue

            since = self._since.setdefault(rule.name, obs.ts)
            if obs.ts - since < rule.for_s:
                continue
            last = self._last_fired.get(rule.name)
            if last is not None and obs.ts - last < rule.cooldown_s:
                continue
            self._last_fired[rule.name] = obs.ts
            fired.append((rule, obs.ts - since))

        if not fired:
            return None

        # 가장 심각한 룰을 대표로 삼는다.
        rule, sustained = max(fired, key=lambda item: SEVERITY_ORDER[item[0].severity])
        return self.detect(
            obs,
            score=self._score(rule, obs),
            severity=rule.severity,
            rule=rule.name,
            rules=[r.name for r, _ in fired],
            sustained_s=round(sustained, 1),
            explain=self._explain(rule, obs),
            **self._evidence(rule, obs),
        )

    # ------------------------------------------------------------------ 설명

    def _score(self, rule: Rule, obs: Observation) -> float:
        """룰이 건드린 메트릭 중 가장 크게 벗어난 정도를 0~1 로."""
        best = 0.0
        for metric in rule.metrics:
            stats = self.baselines.stats(metric)
            if stats is None:
                continue
            z = stats.z(obs.metrics.get(metric))
            if z is not None:
                best = max(best, abs(z))
        if best == 0.0:
            # z 를 못 구하는 룰(절대 임계값·문자열 조건)도 발화는 유효하다.
            return 0.5
        return min(1.0, best / _SCORE_SATURATION_Z)

    def _evidence(self, rule: Rule, obs: Observation) -> dict[str, Any]:
        """왜 그렇게 판단했는지. 탐지가 아니라 설명이 산출물이다(CLAUDE.md)."""
        evidence: dict[str, Any] = {}
        for metric in dict.fromkeys(rule.metrics):
            value = obs.metrics.get(metric)
            stats = self.baselines.stats(metric)
            evidence[metric] = {
                "value": round(value, 3) if isinstance(value, (int, float)) else value,
                "median": round(stats.median, 3) if stats else None,
                "sigma": round(stats.sigma, 3) if stats else None,
                "z": round(stats.z(value), 2) if stats and stats.z(value) is not None else None,
            }
        return {"evidence": evidence}

    def _explain(self, rule: Rule, obs: Observation) -> str:
        """`explain` 의 자리표시자를 실제 값으로 채운다.

        `{disk_resp_ms}` → 현재 값, `{disk_resp_ms.median}` → 그 메트릭의 평소 값.
        """
        if not rule.explain:
            return rule.name

        def replace(match: re.Match) -> str:
            token = match.group(1)
            metric, _, attribute = token.partition(".")
            if attribute:
                stats = self.baselines.stats(metric)
                if stats is None:
                    return "?"
                value = getattr(stats, attribute, None)
                return "?" if value is None else f"{value:.4g}"
            value = obs.metrics.get(metric)
            return "?" if value is None else (f"{value:.4g}" if isinstance(value, (int, float)) else str(value))

        return re.sub(r"\{([A-Za-z_][\w.]*)\}", replace, rule.explain)

    def warm(self, db, now: float) -> int:
        """DB 에서 베이스라인을 채운다. 재시작 후 즉시 탐지 가능하게 만든다."""
        return self.baselines.warm_from_db(db, now)


def rules_from_observations(observations: Iterable[Observation]) -> None:  # pragma: no cover
    """(자리표시자) Phase 4 에서 레짐별 룰 자동 생성을 검토한다."""
    raise NotImplementedError


if __name__ == "__main__":  # 스모크: python -m argus.detection.rules
    from .base import run_detector

    rules = [
        Rule(
            name="CPU 과부하",
            conditions=[Condition(metric="cpu_total", op=">", value="median + 4 * sigma")],
            for_s=30.0,
            cooldown_s=120.0,
            explain="CPU {cpu_total}% (평소 {cpu_total.median}%)",
        )
    ]
    engine = RuleEngine(rules, window_s=600.0, min_samples=30)

    # 평소 20% 로 200초, 그 뒤 90% 로 120초
    stream = [Observation(ts=1000.0 + i, metrics={"cpu_total": 20.0 + (i % 3)}) for i in range(200)]
    stream += [Observation(ts=1200.0 + i, metrics={"cpu_total": 90.0}) for i in range(120)]
    results = run_detector(engine, stream)

    print(f"  발화 {len(results)}건")
    for d in results:
        print(f"    ts={d.ts:.0f}  score={d.score:.2f}  {d.severity}  {d.features['explain']}")
        print(f"      근거: {d.features['evidence']}")

    problems = []
    if not results:
        problems.append("지속된 90% CPU 를 탐지하지 못했다")
    else:
        # 1200 에 시작 → 30초 지속 후 1230 에 첫 발화
        if results[0].ts != 1230.0:
            problems.append(f"지속 조건이 틀렸다: 첫 발화 {results[0].ts} (1230 이어야)")
        # 쿨다운 120초 → 120틱 구간에서 최대 2번
        if len(results) > 2:
            problems.append(f"쿨다운이 안 걸렸다: {len(results)}건 (최대 2건이어야)")
        if "평소" not in results[0].features["explain"]:
            problems.append("explain 치환이 안 됐다")

    # 순간 스파이크는 무시되어야 한다
    spike = [Observation(ts=2000.0 + i, metrics={"cpu_total": 20.0}) for i in range(200)]
    spike += [Observation(ts=2200.0 + i, metrics={"cpu_total": 99.0}) for i in range(5)]
    spike += [Observation(ts=2205.0 + i, metrics={"cpu_total": 20.0}) for i in range(60)]
    if run_detector(RuleEngine(rules, window_s=600.0, min_samples=30), spike):
        problems.append("5초짜리 순간 스파이크에 발화했다 — 지속 조건이 무의미하다")

    # 값을 모르는 메트릭에는 발화하지 않아야 한다
    unknown = [Observation(ts=3000.0 + i, metrics={"cpu_total": None}) for i in range(200)]
    if run_detector(RuleEngine(rules, window_s=600.0, min_samples=30), unknown):
        problems.append("값이 없는 메트릭에 발화했다")

    # 룰 파일 검증이 로드 시점에 터져야 한다
    for bad, why in [
        ({"metric": "x", "op": "~=", "value": 1}, "알 수 없는 연산자"),
        ({"metric": "x", "op": ">", "value": "__import__('os')"}, "위험한 표현식"),
    ]:
        try:
            Condition(**bad)
            problems.append(f"{why} 가 로드 시점에 거부되지 않았다")
        except RuleError:
            pass

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.rules")

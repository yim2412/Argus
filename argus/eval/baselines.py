"""기준선 탐지기 — Phase 3 이후가 넘어야 할 바닥.

**이 탐지기들은 좋으라고 만든 게 아니다.** 일부러 순진하게 만들었다.

스코어보드에 숫자만 있으면 그 숫자가 좋은지 알 수 없다. F1 0.62 는 잘한 건가?
비교 대상이 없으면 답할 수 없다. 그래서 "아무 생각 없이 만들면 이 정도"를 먼저 박아
두고, 새 탐지기는 이걸 넘어야 채택한다(CLAUDE.md: 수치 없이 모델을 추가하지 않는다).

특히 `always` 가 중요하다. 무조건 발화하는 탐지기는 재현율 100% 를 받는다. 재현율만
보고 모델을 고르면 이 바보를 고르게 된다는 것을 스코어보드에 계속 보여 주기 위한
장치다.

`fixed_threshold` 는 CLAUDE.md 설계 규칙 2("절대값 임계값을 박지 않는다")를 **일부러**
어긴다. 그것이 왜 나쁜지 — HDD 사용자에게, 코어가 적은 PC 에게 왜 오탐이 쏟아지는지 —
를 수치로 보여 주는 것이 이 기준선의 존재 이유다. 실제 탐지기는 Phase 3 에서
`machine_profile` 기준의 상대값으로 만든다.
"""

from __future__ import annotations

from typing import Callable

from ..detection.base import BaseDetector, Detection, Observation, SEVERITY_WARNING

# 이 기준선들이 쓰는 값. 순진함을 드러내는 게 목적이라 config 로 빼지 않는다 —
# 실제 탐지기의 튜닝 상수만 config 에 둔다(CLAUDE.md 설계 규칙 3).
NAIVE_CPU_PCT = 85.0
NAIVE_MEM_PCT = 85.0
NAIVE_SUSTAIN_S = 30.0


class AlwaysDetector(BaseDetector):
    """항상 발화. 재현율 100%, 정밀도 바닥.

    재현율만으로 탐지기를 고르면 안 된다는 것을 스코어보드에 상설 전시한다.
    """

    name = "always"

    def evaluate(self, obs: Observation) -> Detection | None:
        return self.detect(obs, score=1.0, reason="무조건 발화(기준선)")


class FixedThresholdDetector(BaseDetector):
    """절대값 임계값 + 지속 조건.

    지속 조건(`sustain_s`)은 순진한 기준선에도 넣는다. 이게 없으면 순간 스파이크마다
    울려서 비교 자체가 성립하지 않고(FP 수백 건), "임계값이 절대값이라 나쁘다"는 진짜
    논점이 "지속 조건이 없어서 나쁘다"에 묻힌다.
    """

    def __init__(
        self,
        name: str,
        metric: str,
        threshold: float,
        *,
        sustain_s: float = NAIVE_SUSTAIN_S,
        warmup_s: float = 0.0,
    ) -> None:
        super().__init__(warmup_s=warmup_s)
        self.name = name
        self.metric = metric
        self.threshold = threshold
        self.sustain_s = sustain_s
        self._above_since: float | None = None

    def reset(self) -> None:
        super().reset()
        self._above_since = None

    def evaluate(self, obs: Observation) -> Detection | None:
        value = obs.metric(self.metric)
        if value is None:
            return None
        if value < self.threshold:
            self._above_since = None
            return None

        if self._above_since is None:
            self._above_since = obs.ts
        if obs.ts - self._above_since < self.sustain_s:
            return None

        # 임계값을 얼마나 넘었는지를 0~1 로. 임계값의 1.5배에서 1.0 에 닿는다.
        headroom = max(1e-6, self.threshold * 0.5)
        score = min(1.0, (value - self.threshold) / headroom)
        return self.detect(
            obs,
            score=max(0.05, score),
            severity=SEVERITY_WARNING,
            metric=self.metric,
            value=round(value, 2),
            threshold=self.threshold,
            sustained_s=round(obs.ts - self._above_since, 1),
        )


def cpu_baseline() -> FixedThresholdDetector:
    return FixedThresholdDetector("fixed_cpu", "cpu_total", NAIVE_CPU_PCT)


def mem_baseline() -> FixedThresholdDetector:
    return FixedThresholdDetector("fixed_mem", "mem_percent", NAIVE_MEM_PCT)


# 이름 → 생성자. CLI 의 `--detector` 가 여기서 찾는다.
# Phase 3 부터 실제 탐지기가 이 표에 등록되고, 기준선은 비교용으로 남는다.
REGISTRY: dict[str, Callable[[], BaseDetector]] = {
    "always": AlwaysDetector,
    "fixed_cpu": cpu_baseline,
    "fixed_mem": mem_baseline,
}


def build(name: str) -> BaseDetector:
    if name not in REGISTRY:
        raise KeyError(f"알 수 없는 탐지기: {name} (가능: {', '.join(sorted(REGISTRY))})")
    return REGISTRY[name]()


if __name__ == "__main__":  # 스모크: python -m argus.eval.baselines
    from ..detection.base import run_detector

    # 앞 60초 조용, 그 뒤 120초 동안 CPU 95%
    stream = [
        Observation(ts=1000.0 + i, metrics={"cpu_total": 10.0 if i < 60 else 95.0, "mem_percent": 40.0})
        for i in range(180)
    ]

    problems = []
    for name in sorted(REGISTRY):
        detector = build(name)
        results = run_detector(detector, stream)
        first = f", 첫 발화 ts={results[0].ts:.0f}" if results else ""
        print(f"  {name:<12} 발화 {len(results):>4}건{first}")

        again = run_detector(build(name), stream)
        if [(d.ts, d.score) for d in results] != [(d.ts, d.score) for d in again]:
            problems.append(f"{name}: 두 번 실행 결과가 다르다")

    cpu = run_detector(build("fixed_cpu"), stream)
    if not cpu:
        problems.append("fixed_cpu 가 지속된 95% CPU 를 탐지하지 못했다")
    elif cpu[0].ts != 1090.0:
        # 60초 시점에 넘고 30초 지속 후 발화해야 한다
        problems.append(f"fixed_cpu 의 지속 조건이 틀렸다: 첫 발화 {cpu[0].ts} (1090 이어야)")
    if run_detector(build("fixed_mem"), stream):
        problems.append("fixed_mem 이 40% 메모리에서 발화했다")
    if len(run_detector(build("always"), stream)) != len(stream):
        problems.append("always 가 모든 틱에서 발화하지 않았다")

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] eval.baselines")

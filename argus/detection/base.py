"""탐지기 인터페이스.

이 파일은 Phase 3 이후 모든 탐지기가 지켜야 할 계약이다. 지금 정해 두는 이유는
리플레이 하네스가 채점할 대상이 바로 이 인터페이스이기 때문이다.

**가장 중요한 규칙: 탐지기는 `time.time()` 을 부르지 않는다.**

지금 시각을 직접 읽는 탐지기는 리플레이가 불가능하다. 저장된 어제 데이터를 100배속으로
재생하는데 탐지기가 "지금"을 오늘로 알면, 시간 기반 판단(창 길이·쿨다운·베이스라인 나이)이
전부 틀어진다. 그래서 시각은 `Observation.ts` 로만 들어오고, 탐지기는 그것만 본다.

이 규칙을 어기면 조용히 틀린다 — 실시간에서는 잘 도는 것처럼 보이고 리플레이에서만
이상해진다. `tests/test_detector_contract.py` 가 기계적으로 잡는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

# 심각도. 알림 정책(Phase 9)이 이 값으로 에스컬레이션을 결정한다.
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}


@dataclass(slots=True)
class ProcessView:
    """한 시점의 프로세스 하나. 귀인(Phase 8)의 재료."""

    pid: int
    name: str
    cpu_percent: float | None = None
    rss_mb: float | None = None
    io_read_bps: float | None = None
    io_write_bps: float | None = None
    handles: int | None = None
    threads: int | None = None
    foreground: bool = False


@dataclass(slots=True)
class Observation:
    """탐지기에 들어가는 한 틱.

    `ts` 는 **관측 시각**이지 처리 시각이 아니다. 리플레이에서는 과거 시각이 들어온다.
    """

    ts: float
    metrics: dict[str, Any] = field(default_factory=dict)
    processes: list[ProcessView] = field(default_factory=list)
    gpus: list[dict[str, Any]] = field(default_factory=list)
    # 이 관측이 신뢰할 수 없는 구간인가 (절전 복귀 직후 등).
    # 베이스라인 학습과 탐지 모두 이 구간을 건너뛰어야 한다.
    suspect: bool = False

    def flatten_gpus(self) -> "Observation":
        """`gpus[0]` 의 항목을 `gpu_*` 이름으로 `metrics` 에 펼친 사본.

        **한 곳에만 둔다.** 이 규칙이 룰 엔진에만 있고 베이스라인 워밍에는 없어서,
        GPU 지표만 재시작마다 백지에서 다시 배웠다(2026-07-30 확인). 부하 중에
        재시작하면 부하 상태가 "평소"로 학습돼 실제 스로틀이 z ≈ 0 으로 묻힌다 —
        실측에서 중앙값이 69도에서 85도로 옮겨가 83도가 평소보다 *낮은* 값이 됐다.

        다중 GPU 는 0번(주 장치)만 본다. 두 번째는 대개 내장 그래픽이고 병목의
        주체가 아니다.
        """
        if not self.gpus:
            return self
        merged = dict(self.metrics)
        for key, value in self.gpus[0].items():
            if key not in ("ts", "gpu_index"):
                merged.setdefault(f"gpu_{key}", value)
        return Observation(
            ts=self.ts, metrics=merged, processes=self.processes,
            gpus=self.gpus, suspect=self.suspect,
        )

    def metric(self, key: str, default: float | None = None) -> float | None:
        value = self.metrics.get(key)
        return default if value is None else value

    @property
    def foreground_program(self) -> str | None:
        """지금 사용자가 보고 있는 프로그램 이름. 없으면 None.

        **규칙을 한 곳에 둔다.** 조건부 베이스라인이 학습할 때와 판정할 때 다른
        규칙을 쓰면, 학습된 적 없는 이름으로 조회해 전역으로만 떨어진다 — 예외도
        안 나고 조건부 축이 있으나 마나가 된다. `flatten_gpus` 를 한 곳에 둔 이유와 같다.

        이름은 소문자로 맞춘다. 수집 경로에 따라 대소문자가 갈리면 같은 프로그램이
        둘로 세어져 표본이 절반씩 나뉜다.
        """
        for view in self.processes:
            if view.foreground and view.name:
                return view.name.lower()
        return None

    def top_by(self, key: str, limit: int = 5) -> list[ProcessView]:
        """지정한 지표 기준 상위 프로세스. 귀인 후보 뽑기용."""
        def sort_key(p: ProcessView) -> float:
            value = getattr(p, key, None)
            return value if value is not None else 0.0

        return sorted(self.processes, key=sort_key, reverse=True)[:limit]


@dataclass(slots=True)
class Detection:
    """탐지기 한 번의 판정.

    `score` 는 0~1 로 정규화한다. 정규화하지 않으면 탐지기끼리 비교할 수도, 앙상블로
    묶을 수도 없다. 원시 점수를 쓰고 싶으면 `features` 에 넣는다.
    """

    ts: float
    detector: str
    score: float
    severity: str = SEVERITY_WARNING
    features: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score 는 0~1 이어야 합니다 (받은 값: {self.score})")
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"알 수 없는 severity: {self.severity}")

    def to_row(self, run_id: int | None = None) -> tuple:
        """`anomaly_signals` 한 행."""
        return (
            self.ts,
            self.detector,
            round(self.score, 4),
            self.severity,
            json.dumps(self.features, ensure_ascii=False, default=str),
            run_id,
        )


SIGNAL_COLUMNS = ("ts", "detector", "score", "severity", "features", "run_id")


@runtime_checkable
class Detector(Protocol):
    """탐지기 계약.

    구현체는 상태를 가져도 되지만, 그 상태는 `reset()` 으로 완전히 초기화되어야 한다.
    리플레이를 두 번 돌리면 같은 결과가 나와야 하기 때문이다(결정론).
    """

    name: str

    def reset(self) -> None:
        """모든 내부 상태를 버린다. 리플레이 재현성의 전제다."""
        ...

    def observe(self, obs: Observation) -> Detection | None:
        """한 틱을 보고 판정한다. 이상이 아니면 None."""
        ...


class BaseDetector:
    """`Detector` 를 구현하는 편의 기반 클래스.

    부트스트랩 처리를 공통으로 담는다 — 베이스라인이 설 때까지는 아무것도 탐지하지
    않아야 한다. 시작 직후의 판정은 근거가 없고, 오탐만 만든다.
    """

    name = "base"

    def __init__(self, *, warmup_s: float = 0.0) -> None:
        self.warmup_s = warmup_s
        self._first_ts: float | None = None
        self._seen = 0

    def reset(self) -> None:
        self._first_ts = None
        self._seen = 0

    @property
    def warmed_up(self) -> bool:
        return self._first_ts is not None and self._elapsed >= self.warmup_s

    @property
    def _elapsed(self) -> float:
        return 0.0 if self._first_ts is None else self._last_ts - self._first_ts

    def observe(self, obs: Observation) -> Detection | None:
        if self._first_ts is None:
            self._first_ts = obs.ts
        self._last_ts = obs.ts
        self._seen += 1

        # 신뢰할 수 없는 구간(절전 복귀 등)은 학습도 탐지도 하지 않는다.
        if obs.suspect:
            return None

        self.learn(obs)
        if self._elapsed < self.warmup_s:
            return None
        return self.evaluate(obs)

    # ------------------------------------------------------------------ 하위 구현

    def learn(self, obs: Observation) -> None:
        """관측을 상태에 반영한다. 워밍업 중에도 불린다."""

    def evaluate(self, obs: Observation) -> Detection | None:
        """판정한다. 워밍업이 끝난 뒤에만 불린다."""
        raise NotImplementedError

    # ------------------------------------------------------------------ 도우미

    def detect(
        self,
        obs: Observation,
        score: float,
        *,
        severity: str = SEVERITY_WARNING,
        **features: Any,
    ) -> Detection:
        return Detection(
            ts=obs.ts,
            detector=self.name,
            score=max(0.0, min(1.0, score)),
            severity=severity,
            features=features,
        )


def run_detector(detector: Detector, observations: Iterable[Observation]) -> list[Detection]:
    """관측 스트림 전체를 흘려 판정 목록을 얻는다.

    리플레이와 실시간이 같은 함수를 쓰게 해서, 평가한 것과 실제로 도는 것이 다른
    상황을 막는다.
    """
    detector.reset()
    out: list[Detection] = []
    for obs in observations:
        detection = detector.observe(obs)
        if detection is not None:
            out.append(detection)
    return out


if __name__ == "__main__":  # 스모크: python -m argus.detection.base
    class Dummy(BaseDetector):
        name = "dummy"

        def evaluate(self, obs: Observation) -> Detection | None:
            cpu = obs.metric("cpu_total", 0.0) or 0.0
            if cpu > 50:
                return self.detect(obs, score=min(1.0, cpu / 100), cpu_total=cpu)
            return None

    stream = [
        Observation(ts=1000.0 + i, metrics={"cpu_total": 10.0 if i < 5 else 80.0})
        for i in range(10)
    ]
    detector = Dummy(warmup_s=2.0)
    results = run_detector(detector, stream)

    print(f"  관측 {len(stream)}틱 → 판정 {len(results)}건")
    for d in results[:3]:
        print(f"    ts={d.ts:.0f}  score={d.score:.2f}  {d.severity}  {d.features}")

    # 결정론 확인 — 두 번 돌려 같은 결과가 나와야 리플레이가 의미를 가진다
    again = run_detector(detector, stream)
    same = [(d.ts, d.score) for d in results] == [(d.ts, d.score) for d in again]
    print(f"  재현성(두 번 실행 결과 동일): {same}")

    # suspect 구간은 건너뛰어야 한다
    suspect_stream = [Observation(ts=2000.0 + i, metrics={"cpu_total": 99.0}, suspect=True) for i in range(5)]
    detector2 = Dummy(warmup_s=0.0)
    skipped = run_detector(detector2, suspect_stream)
    print(f"  suspect 구간 판정: {len(skipped)}건 (0 이어야 정상)")

    problems = []
    if not results:
        problems.append("높은 CPU 를 탐지하지 못했다")
    if not same:
        problems.append("두 번 실행 결과가 다르다 — 리플레이가 무의미해진다")
    if skipped:
        problems.append("신뢰할 수 없는 구간에서 판정했다")
    try:
        Detection(ts=0, detector="x", score=1.5)
        problems.append("범위를 벗어난 score 가 허용됐다")
    except ValueError:
        pass
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.base")

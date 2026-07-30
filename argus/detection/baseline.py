"""롤링 베이스라인 — "평소"가 무엇인지 정한다.

절대 임계값은 배포 프로그램에서 쓸 수 없다. "디스크 응답 50ms 이상은 병목"은 HDD
사용자에게는 평소이고, "CPU 85% 이상"은 12코어 PC 에서는 좀처럼 닿지 않는다.
그래서 **이 PC 의 평소 대비 얼마나 벗어났는가**로 말한다.

**평균/표준편차 대신 중앙값/MAD 를 쓴다.** 이상 구간이 베이스라인에 섞여 들어가기
때문이다. CPU 가 5분간 100% 였다면 평균은 그만큼 올라가고, 그 오염된 평균을 기준으로
다음 이상을 판정하면 점점 둔감해진다. 중앙값은 표본의 절반이 망가져도 버틴다.

**재시작해도 처음부터 다시 배우지 않는다.** 계획서는 부트스트랩 2시간을 두었지만,
그러면 PC 를 켤 때마다 2시간 동안 아무것도 못 잡는다. 사용자는 PC 를 매일 끈다.
시작할 때 `metrics_raw` 에서 최근 구간을 읽어 즉시 채운다 — 데이터는 이미 DB 에 있다.
진짜 부트스트랩이 필요한 것은 **설치 직후 한 번뿐이다.**
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..logging_setup import get_logger

log = get_logger(__name__)

# 중앙값 ± k·MAD 를 표준편차로 환산하는 상수. 정규분포에서 MAD × 1.4826 ≈ σ 다.
MAD_TO_SIGMA = 1.4826

# 이만큼은 모여야 중앙값을 신뢰한다. 표본 3개의 중앙값은 중앙값이 아니라 우연이다.
DEFAULT_MIN_SAMPLES = 60


@dataclass(frozen=True)
class Stats:
    """한 메트릭의 현재 기준.

    `sigma` 는 MAD 에서 환산한 산포이고 **0 이 될 수 있다.** 값이 내내 똑같은
    메트릭(대부분 0 인 `disk_queue`, 유휴 상태의 `swap_used_mb` 등)이 그렇다.

    이때 바닥값을 억지로 깔면 안 된다. σ 를 아주 작게 잡으면 0 → 1 같은 정상적인
    작은 변동이 z 수십으로 튀어 **오탐 폭풍**이 된다. CLAUDE.md 는 오탐 3번이면
    사용자가 알림을 끈다고 본다 — 미탐보다 비싸다.

    그래서 산포가 없으면 **판정하지 않는다**(`z()` 가 None). 이런 메트릭은 룰에서
    절대 임계값으로 다뤄야 하고, `disk_queue > 2` 처럼 단위 자체가 하드웨어에
    독립적인 지표가 여기 해당한다. `degenerate` 로 그 사실을 드러낸다.
    """

    metric: str
    median: float
    mad: float
    sigma: float
    samples: int
    minimum: float
    maximum: float

    @property
    def degenerate(self) -> bool:
        """산포가 없어 z 로 판정할 수 없는 상태."""
        return self.sigma <= 0

    def z(self, value: float | None) -> float | None:
        """Modified Z-score. 값을 모르거나 산포가 없으면 None."""
        if value is None or self.degenerate:
            return None
        return (value - self.median) / self.sigma

    def threshold(self, k: float) -> float | None:
        """`median + k·σ`. 산포가 없으면 상대 임계값이 성립하지 않으므로 None."""
        if self.degenerate:
            return None
        return self.median + k * self.sigma


class MetricBaseline:
    """메트릭 하나의 롤링 창."""

    def __init__(
        self,
        metric: str,
        *,
        window_s: float,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        sigma_floor: float = 0.0,
        sigma_floor_ratio: float = 0.05,
    ) -> None:
        self.metric = metric
        self.window_s = window_s
        self.min_samples = min_samples
        # 산포의 바닥. 절대 바닥과 "중앙값의 몇 %" 중 큰 쪽을 쓴다.
        # 절대값만 쓰면 스케일이 다른 메트릭(응답 0.1ms vs 대역폭 1e8)에 하나로 못 맞춘다.
        self.sigma_floor = sigma_floor
        self.sigma_floor_ratio = sigma_floor_ratio
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, ts: float, value: float | None) -> None:
        if value is None:
            return
        self._samples.append((ts, float(value)))
        self._trim(ts)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def ready(self) -> bool:
        return len(self._samples) >= self.min_samples

    def stats(self) -> Stats | None:
        if not self.ready:
            return None
        values = [v for _, v in self._samples]
        median = statistics.median(values)
        mad = statistics.median([abs(v - median) for v in values])
        sigma = max(
            mad * MAD_TO_SIGMA,
            self.sigma_floor,
            abs(median) * self.sigma_floor_ratio,
        )
        return Stats(
            metric=self.metric,
            median=median,
            mad=mad,
            sigma=sigma,
            samples=len(values),
            minimum=min(values),
            maximum=max(values),
        )


class BaselineSet:
    """전 메트릭의 베이스라인 묶음."""

    def __init__(
        self,
        *,
        window_s: float = 1800.0,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        sigma_floors: Mapping[str, float] | None = None,
    ) -> None:
        self.window_s = window_s
        self.min_samples = min_samples
        self.sigma_floors = dict(sigma_floors or {})
        self._metrics: dict[str, MetricBaseline] = {}

    def reset(self) -> None:
        self._metrics.clear()

    def _get(self, metric: str) -> MetricBaseline:
        baseline = self._metrics.get(metric)
        if baseline is None:
            baseline = MetricBaseline(
                metric,
                window_s=self.window_s,
                min_samples=self.min_samples,
                sigma_floor=self.sigma_floors.get(metric, 0.0),
            )
            self._metrics[metric] = baseline
        return baseline

    def observe(self, ts: float, metrics: Mapping[str, object]) -> None:
        for name, value in metrics.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._get(name).add(ts, float(value))

    def stats(self, metric: str) -> Stats | None:
        baseline = self._metrics.get(metric)
        return baseline.stats() if baseline is not None else None

    @property
    def ready(self) -> bool:
        """하나라도 준비됐는가. 전부를 요구하면 GPU 없는 PC 에서 영영 준비되지 않는다."""
        return any(b.ready for b in self._metrics.values())

    def readiness(self) -> dict[str, int]:
        return {name: len(b._samples) for name, b in self._metrics.items()}  # noqa: SLF001

    # ------------------------------------------------------------------ 워밍

    def warm_from(self, observations: Iterable) -> int:
        """저장된 관측으로 창을 채운다.

        `suspect` 구간은 건너뛴다 — 절전 복귀 직후의 값으로 "평소"를 배우면 평소가
        아닌 것을 평소로 알게 된다.
        """
        count = 0
        for obs in observations:
            if getattr(obs, "suspect", False):
                continue
            # **GPU 지표도 함께 채운다.** 리플레이 스트림은 `obs.gpus` 를 주는데 여기서
            # `obs.metrics` 만 넣던 탓에 GPU 만 매 기동마다 백지에서 다시 배웠다 —
            # 이 모듈이 "재시작해도 처음부터 다시 배우지 않는다"고 말하는데 GPU 는
            # 예외였다. 펼치는 규칙은 `Observation.flatten_gpus` 한 곳에 있다.
            flat = obs.flatten_gpus() if hasattr(obs, "flatten_gpus") else obs
            self.observe(flat.ts, flat.metrics)
            count += 1
        return count

    def warm_from_db(self, db, now: float) -> int:
        """DB 의 최근 구간을 읽어 채운다. 재시작 직후 즉시 탐지 가능하게 만든다."""
        from ..eval.replay import Replayer, Window

        window = Window(now - self.window_s, now)
        try:
            return self.warm_from(Replayer(db).stream(window))
        except Exception as exc:  # 워밍 실패가 기동을 막으면 안 된다
            log.warning("베이스라인 워밍 실패 — 실시간으로 학습한다", extra={"error": str(exc)})
            return 0


if __name__ == "__main__":  # 스모크: python -m argus.detection.baseline
    import random

    random.seed(7)
    baseline = BaselineSet(window_s=600.0, min_samples=30)

    # 평소: 평균 20 근처에서 흔들린다
    ts = 1000.0
    for i in range(300):
        baseline.observe(ts + i, {"cpu_total": 20 + random.gauss(0, 3), "disk_queue": 0.0})

    stats = baseline.stats("cpu_total")
    print(f"  cpu_total: 중앙값 {stats.median:.1f}  MAD {stats.mad:.2f}  σ {stats.sigma:.2f}  표본 {stats.samples}")
    print(f"    z(20)={stats.z(20):.2f}  z(50)={stats.z(50):.2f}  임계(k=4)={stats.threshold(4):.1f}")

    flat = baseline.stats("disk_queue")
    print(f"  disk_queue(내내 0): σ {flat.sigma:.4f}  degenerate={flat.degenerate}  "
          f"z(1.0)={flat.z(1.0)}  임계={flat.threshold(4)}")

    # 오염 저항: 표본의 30% 를 100 으로 망가뜨려도 중앙값은 버텨야 한다
    polluted = BaselineSet(window_s=600.0, min_samples=30)
    for i in range(300):
        value = 100.0 if i % 10 < 3 else 20 + random.gauss(0, 3)
        polluted.observe(ts + i, {"cpu_total": value})
    dirty = polluted.stats("cpu_total")
    print(f"  30% 오염 후 중앙값: {dirty.median:.1f} (20 근처여야 정상)")

    # 창 밖 표본은 빠져야 한다
    old = BaselineSet(window_s=100.0, min_samples=10)
    for i in range(200):
        old.observe(ts + i, {"cpu_total": float(i)})
    kept = old.stats("cpu_total")
    print(f"  창 100초 유지: 표본 {kept.samples}개, 범위 {kept.minimum:.0f}~{kept.maximum:.0f}")

    problems = []
    if not 18 <= stats.median <= 22:
        problems.append(f"정상 데이터의 중앙값이 20 근처가 아니다: {stats.median}")
    if stats.z(50) is None or stats.z(50) < 5:
        problems.append(f"명백한 이상(50)의 z 가 너무 작다: {stats.z(50)}")
    # 산포가 없는 메트릭은 z 로 판정하지 않아야 한다. 억지로 판정하면 오탐이 쏟아진다.
    if not flat.degenerate:
        problems.append("값이 일정한 메트릭이 degenerate 로 표시되지 않았다")
    if flat.z(1.0) is not None or flat.threshold(4) is not None:
        problems.append("산포가 없는데 z/임계값을 냈다 — 작은 변동이 전부 이상이 된다")
    if stats.degenerate:
        problems.append("정상적으로 흔들리는 메트릭이 degenerate 로 잡혔다")
    if not 18 <= dirty.median <= 24:
        problems.append(f"30% 오염에 중앙값이 끌려갔다: {dirty.median} (로버스트하지 않다)")
    if kept.samples > 105:
        problems.append(f"창 밖 표본이 남아 있다: {kept.samples}개")
    if BaselineSet(window_s=600.0, min_samples=30).stats("cpu_total") is not None:
        problems.append("표본이 없는데 통계를 냈다")

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.baseline")

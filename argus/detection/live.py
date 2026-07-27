"""실시간 탐지 — 리플레이와 **같은 경로**로 돈다.

수집 스레드에서 관측을 직접 조립해 탐지기에 넣는 구조를 만들지 않았다. 그러면
"평가할 때 쓰는 관측"과 "실제로 도는 관측"이 두 벌이 되고, 둘이 조금씩 달라지는
순간 스코어보드 숫자가 거짓말이 된다. 어느 쪽이 진짜인지 알 수 없게 된다.

대신 **저장된 것을 조금 늦게 따라 읽는다.** 실시간 탐지기는 결국 "미래를 따라잡지
못하는 리플레이어"다. 같은 `Replayer`, 같은 `Observation`, 같은 `run_detector` 를
쓰므로 평가와 운영이 갈라질 수 없다.

대가는 지연이다. 몇 초 늦게 본다. 룰의 지속 조건이 30초 이상이라 문제가 되지 않고,
탐지 지연 예산(분 단위)에 비하면 무시할 수 있다.

**알림은 보내지 않는다.** 여기서는 `anomaly_signals` 에 기록만 한다. 사용자에게
무엇을 언제 알릴지는 Phase 9 의 문제이고, 오탐률이 검증되기 전에 알림을 붙이면
되돌릴 수 없는 실수가 된다.
"""

from __future__ import annotations

import time

from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from ..storage.hot import Database
from .base import SIGNAL_COLUMNS, Detector
from .registry import build
from .replay_source import ObservationTail

log = get_logger(__name__)

# 아직 안 쌓였을 수 있는 최근 구간은 건드리지 않는다. 수집기와 writer 가 배치로
# 쓰므로, 방금 시각까지 읽으면 뒤늦게 도착한 행을 영영 건너뛴다.
DEFAULT_LAG_S = 5.0


class DetectionComponent(Component):
    """룰 엔진을 실시간으로 돌린다."""

    name = "detection"

    def __init__(
        self,
        db: Database,
        *,
        detector_name: str = "rules",
        interval_s: float = 10.0,
        lag_s: float = DEFAULT_LAG_S,
        warm_window_s: float = 1800.0,
    ) -> None:
        self.interval_s = interval_s
        self.db = db
        self.detector_name = detector_name
        self.lag_s = lag_s
        self.warm_window_s = warm_window_s
        self.detector: Detector | None = None
        self._tail: ObservationTail | None = None
        self._signals = 0

    def setup(self) -> None:
        self.detector = build(self.detector_name)
        self.detector.reset()

        # 저장된 최근 구간으로 베이스라인을 채운다. 이게 없으면 재시작할 때마다
        # 30분씩 아무것도 못 잡는다 — 사용자는 PC 를 매일 끈다.
        warmed = 0
        warm = getattr(self.detector, "warm", None)
        if callable(warm):
            warmed = warm(self.db, time.time())

        self._tail = ObservationTail(self.db, lag_s=self.lag_s)
        log.info(
            "실시간 탐지 시작",
            extra={"detector": self.detector_name, "warmed_ticks": warmed,
                   "interval_s": self.interval_s},
        )

    def tick(self) -> None:
        if self.detector is None or self._tail is None:
            return

        observations = self._tail.next_batch()
        if not observations:
            return

        rows = []
        for obs in observations:
            detection = self.detector.observe(obs)
            if detection is not None:
                rows.append(detection.to_row(run_id=None))   # NULL = 실시간
                log.info(
                    "이상 탐지",
                    extra={
                        "detector": detection.detector,
                        "score": round(detection.score, 3),
                        "severity": detection.severity,
                        "rule": detection.features.get("rule"),
                        "explain": detection.features.get("explain"),
                    },
                )

        if rows:
            self.db.insert_many("anomaly_signals", SIGNAL_COLUMNS, rows)
            self._signals += len(rows)

    def on_time_gap(self, gap_s: float) -> None:
        """절전 복귀. 공백 뒤의 값으로 판정하지 않도록 상태를 버린다.

        지속 조건 시계를 그대로 두면 "3시간 동안 조건이 참이었다"가 되어 자고 일어난
        직후 알림이 터진다. 실제로는 그 3시간을 보지 못했다.
        """
        if self.detector is not None:
            self.detector.reset()
        if self._tail is not None:
            self._tail.skip_to_now()
        log.info("시간 공백 — 탐지 상태 초기화", extra={"gap_s": round(gap_s, 1)})

    def teardown(self) -> None:
        log.info("실시간 탐지 종료", extra={"signals": self._signals})


if __name__ == "__main__":  # 스모크: python -m argus.detection.live
    with Database() as db:
        component = DetectionComponent(db, interval_s=1.0)
        component.setup()
        print(f"  탐지기: {component.detector_name}")
        baselines = getattr(component.detector, "baselines", None)
        if baselines is not None:
            ready = [name for name, count in baselines.readiness().items() if count >= 60]
            print(f"  베이스라인 준비된 메트릭 {len(ready)}개")

        before = component._signals
        for _ in range(3):
            component.tick()
            time.sleep(1.0)
        print(f"  3틱 처리 — 기록된 신호 {component._signals - before}건")
        component.teardown()

    problems = []
    if component.detector is None:
        problems.append("탐지기가 만들어지지 않았다")
    if baselines is None or not baselines.ready:
        problems.append("베이스라인 워밍이 되지 않았다 — 재시작 직후 탐지 불가")
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.live")

"""냉각 열화 — **같은 부하에서 예전보다 뜨거운가.**

절대 온도는 사용자가 조치할 수 없는 정보다. `GPU 93도` 를 들으면 할 수 있는 일이 없다 —
게임을 하면 뜨겁고, 그 온도는 팬·클럭이 균형을 잡은 결과이지 고장이 아니다. 실측에서
이 PC 의 부하 구간 온도는 **6일 내내 정확히 93.0도**였다(사용률 89~98%, 전력 219~228W).

조치할 수 있는 신호는 하나다 — **같은 부하에서 온도가 올라가고 있다.** 먼지가 쌓이거나
서멀이 마르면 그렇게 된다. 그때 할 일이 분명하다(청소·재도포·팬 커브).

**하드웨어를 가정하지 않는다(규칙 2).** 문턱이 절대 온도가 아니라 "자기 과거 대비 상승"
이므로, 노트북(평소 87도)이든 데스크탑(평소 70도)이든 같은 코드가 그대로 맞는다.
2026-08-02 에 GPU 온도 룰이 정상 동작을 하루 9.3회 알리던 것이 바로 절대 문턱 때문이었다.

**부하를 맞춰 비교하는 것이 핵심이다.** 유휴 시간을 섞으면 "요즘 게임을 많이 했다"가
"냉각이 나빠졌다"로 둔갑한다. 그래서 `gpu_util_mean >= min_gpu_util` 인 1분 버킷만 보고,
그런 버킷이 하루에 `min_busy_minutes` 미만인 날은 표본에서 통째로 뺀다.

읽는 곳은 롤업(`metrics_1m` + 웜 Parquet)이다. 원본은 24시간 뒤 지워지므로 며칠을
비교할 수 없고, 롤업에는 `gpu_util_mean`·`gpu_temp_max` 가 처음부터 들어 있다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from statistics import median

from ..config.loader import ThermalDriftSettings
from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from ..storage import history
from .base import SEVERITY_WARNING, SIGNAL_COLUMNS, Detection

log = get_logger(__name__)


@dataclass(frozen=True)
class Drift:
    """냉각 열화 판정 결과."""

    baseline_c: float
    """과거 구간의 부하 시 온도 중앙값."""
    recent_c: float
    """최근 구간의 같은 값."""
    rise_c: float
    """상승폭. 양수면 뜨거워졌다."""
    baseline_days: int
    recent_days: int

    @property
    def explain(self) -> str:
        return (
            f"GPU 부하 시 온도가 {self.baseline_c:.0f}도 → {self.recent_c:.0f}도 "
            f"({self.rise_c:+.1f}도, 최근 {self.recent_days}일 vs 이전 {self.baseline_days}일) "
            f"— 먼지·서멀·팬 커브를 확인할 만하다"
        )


def daily_load_temp(settings: ThermalDriftSettings) -> dict[str, float]:
    """날짜 → 부하 구간의 GPU 온도 중앙값. 부하가 적었던 날은 뺀다.

    **중앙값을 저장소에서 뽑지 않고 파이썬에서 낸다.** DuckDB 에는 `median()` 이 있지만
    SQLite 에는 없어서, 두 계층의 집계 방식이 갈리면 웜/핫 경계에서 값이 튄다.
    하루치 버킷은 많아야 1,440개라 올려도 부담이 없다.
    """
    rows = history._by_day(  # noqa: SLF001 — 웜/핫 병합 규칙을 재구현하지 않는다
        "metrics",
        "SELECT strftime(to_timestamp(ts_min), '%Y-%m-%d'), gpu_temp_max FROM warm "
        "WHERE gpu_util_mean >= ? AND gpu_temp_max IS NOT NULL",
        "SELECT strftime('%Y-%m-%d', ts_min, 'unixepoch', 'localtime'), gpu_temp_max "
        "FROM metrics_1m WHERE gpu_util_mean >= ? AND gpu_temp_max IS NOT NULL",
        [settings.min_gpu_util],
    )

    by_day: dict[str, list[float]] = {}
    for day, temp in rows:
        by_day.setdefault(str(day), []).append(float(temp))

    return {
        day: median(temps)
        for day, temps in by_day.items()
        if len(temps) >= settings.min_busy_minutes
    }


def assess(daily: dict[str, float], settings: ThermalDriftSettings) -> Drift | None:
    """최근과 과거를 비교한다. 표본이 모자라거나 상승이 문턱 아래면 `None`.

    **판정하지 못하는 것과 정상인 것을 구분한다.** 표본이 없으면 조용히 통과시키지 말고
    아무 말도 하지 않아야 한다 — 데이터가 없는데 "정상"이라고 하면 그것도 거짓말이다.
    """
    if len(daily) < settings.min_days:
        return None

    days = sorted(daily)
    recent_days = days[-settings.recent_days:]
    baseline_days = days[: -settings.recent_days][-settings.baseline_days:]
    if len(recent_days) < settings.recent_days or not baseline_days:
        return None

    baseline_c = median(daily[d] for d in baseline_days)
    recent_c = median(daily[d] for d in recent_days)
    rise = recent_c - baseline_c
    if rise < settings.rise_c:
        return None

    return Drift(
        baseline_c=baseline_c,
        recent_c=recent_c,
        rise_c=rise,
        baseline_days=len(baseline_days),
        recent_days=len(recent_days),
    )


def evaluate(settings: ThermalDriftSettings) -> Drift | None:
    """저장소를 읽어 판정까지. 컴포넌트와 스모크가 함께 쓴다."""
    return assess(daily_load_temp(settings), settings)


class ThermalDriftMonitor(Component):
    """냉각 열화를 주기적으로 확인하는 컴포넌트.

    **판정 주기가 길다(기본 6시간).** 하루 단위 비교라 자주 볼 이유가 없고, 웜
    (Parquet)까지 훑는 작업이라 싸지도 않다. `FingerprintBuilder` 와 같은 성격이다.

    한 번 알린 열화는 **하루 동안 다시 알리지 않는다.** 냉각은 사용자가 청소하기
    전까지 계속 나쁜 상태이므로, 억제가 없으면 6시간마다 같은 말을 반복한다 —
    GPU 온도 룰이 하루 12.7회를 만든 것과 같은 실패다.
    """

    name = "thermal_drift"

    def __init__(self, db, settings: ThermalDriftSettings | None = None) -> None:
        self.db = db
        self.settings = settings or ThermalDriftSettings()
        self.interval_s = self.settings.interval_s
        self._last_signal_ts = 0.0

    def setup(self) -> None:
        log.info(
            "냉각 열화 감시 시작",
            extra={
                "min_gpu_util": self.settings.min_gpu_util,
                "rise_c": self.settings.rise_c,
                "min_days": self.settings.min_days,
            },
        )

    def tick(self) -> None:
        try:
            verdict = evaluate(self.settings)
        except Exception:
            # 웜 파티션이 아직 없거나 DuckDB 가 없는 환경. 나머지 탐지를 막지 않는다.
            log.exception("냉각 열화 판정 실패")
            return
        if verdict is None:
            return

        now = time.time()
        if now - self._last_signal_ts < _RESIGNAL_S:
            return

        signal = Detection(
            ts=now,
            detector=self.name,
            # 상승폭이 클수록 확신이 크다. 문턱의 3배에서 포화시킨다 — 온도는 몇 도
            # 단위로 움직이는 지표라 그 위를 더 나눠 봐야 의미가 없다.
            score=min(1.0, verdict.rise_c / (self.settings.rise_c * 3.0)),
            severity=SEVERITY_WARNING,
            features={
                "rule": "냉각 열화",
                "rules": ["냉각 열화"],
                "metric": "gpu_temp_c",
                "baseline_c": round(verdict.baseline_c, 1),
                "recent_c": round(verdict.recent_c, 1),
                "rise_c": round(verdict.rise_c, 1),
                "explain": verdict.explain,
            },
        )
        self.db.insert_many("anomaly_signals", SIGNAL_COLUMNS, [signal.to_row()])
        self._last_signal_ts = now
        log.warning("냉각 열화", extra={"rise_c": round(verdict.rise_c, 1)})


# 같은 열화를 다시 알리기까지의 최소 간격. 냉각은 청소하기 전까지 계속 나쁘다.
_RESIGNAL_S = 86400.0


if __name__ == "__main__":  # 스모크: python -m argus.detection.thermal
    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="WARNING")
    cfg = load_settings().thermal_drift
    daily = daily_load_temp(cfg)

    print(f"  부하 기준 : gpu_util_mean >= {cfg.min_gpu_util:.0f}% · "
          f"하루 {cfg.min_busy_minutes}분 이상")
    if not daily:
        print("[FAIL] 부하 구간 표본이 없다 — GPU 를 쓰는 작업을 한 뒤 다시 볼 것")
        raise SystemExit(1)

    for day in sorted(daily):
        print(f"    {day}   {daily[day]:.1f}도")

    verdict = assess(daily, cfg)
    if verdict is None:
        need = cfg.min_days - len(daily)
        if need > 0:
            print(f"  판정      : 표본 {len(daily)}일 — {cfg.min_days}일 필요 (앞으로 {need}일)")
        else:
            print(f"  판정      : 상승 없음 (문턱 {cfg.rise_c:+.1f}도)")
    else:
        print(f"  판정      : {verdict.explain}")
    print("[OK] thermal")

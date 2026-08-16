"""느린 메모리 누수를 **제품 리플레이 경로**에 태워 룰이 잡는지 본다.

Phase 5(다차원 이상탐지)·7(시퀀스 모델)은 같은 하나의 선행 조건에 막혀 있다 —
*"이길 상대가 없다: 룰이 못 잡는 시나리오가 없다"*. 그 상대가 실재하는지를
60분을 태우기 전에 여기서 먼저 묻는다.

**왜 별도 도구인가.** 2026-08-16 오전에 같은 질문을 스크래치 스크립트로 한 번 쟀는데,
그때는 `MetricBaseline` 만 실물이고 룰 엔진 전체(expr 평가·`for` 지속·`cooldown`·
`LoadGate`·프로그램별 베이스라인)를 안 거쳤다. **내 산수가 아니라 제품이 답해야 한다.**
그래서 여기서는 `registry.build()` 로 만든 **설정 배선 그대로의 탐지기**에 관측을 먹인다.

**입력은 지어낸 노이즈가 아니라 실제 `metrics_raw` 시계열이다.** 합성 노이즈로는
"이 PC 의 메모리가 매우 평탄하다"는 성질(σ 가 하한에 붙어 있다)이 재현되지 않고,
그 성질이 바로 판정을 가르는 값이다.

사용법:

    .venv\\Scripts\\python.exe tools\\ramp_replay.py                  # 60분 램프
    .venv\\Scripts\\python.exe tools\\ramp_replay.py --minutes 5,10,20,30,60

여러 길이를 주면 **같은 총 증가량을 다른 속도로** 올린다. 5분이 발화하고 60분이
미탐이면 그건 "룰이 죽었다"가 아니라 "느려서 못 잡는다"의 증거다 — 대조군이 없으면
둘을 구분할 수 없다.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.detection.base import Observation  # noqa: E402
from argus.detection.registry import build  # noqa: E402
from argus.eval.replay import Replayer, Window  # noqa: E402
from argus.storage.hot import Database  # noqa: E402

# 실측 재현값. `fault_injector memory_leak --ramp` 가 이 PC 에서 실제로 만드는 증가폭이다
# (가용 메모리 보호로 13035 → 11883MB 하향된 뒤의 값). 다른 PC 에서는 달라진다.
DEFAULT_DELTA_PP = 18.2

# 램프 전에 베이스라인을 세울 구간. `detection.baseline_window_s`(30분)보다 길어야
# 램프 시작 시점의 베이스라인이 온전히 정상 구간으로만 채워진다.
DEFAULT_WARM_MIN = 40.0

# 진단용으로 격차를 재는 룰. 판정 자체는 제품이 하고, 이 값은 "얼마나 모자랐나"만 말한다.
TARGET_METRIC = "mem_percent"

# **이 룰의 발화만 성공으로 센다.** 입력이 실제 시계열이라 램프와 무관한 룰이 같은
# 구간에서 얼마든지 발화한다 — 2026-08-16 에 게임이 돌던 구간을 창으로 잡았더니
# 「CPU 과부하」가 떴고, 룰 종류를 안 가리던 첫 판에서 그것이 "60분 램프 발화"로
# 집계됐다(근접도는 -1.24%p 로 문턱 미달인데도). 램프가 만든 것이 아닌 발화를
# 성공으로 세면 이 도구는 **정확히 반대 결론**을 낸다.
TARGET_RULE = "메모리 이상 증가"


@dataclass
class Outcome:
    minutes: float
    fired: list[tuple[float, str]]          # 대상 룰 발화 (램프 시작 후 경과 초, 룰 이름)
    other: list[tuple[float, str]]          # 램프와 무관한 룰 — 성공으로 세지 않는다
    closest_pp: float | None                # 문턱 대비 최대 근접도 (+면 넘김)
    closest_at_s: float | None
    ticks: int
    ramp_ticks: int


def _apply_ramp(obs: Observation, frac: float, delta_pp: float) -> Observation:
    """`mem_percent` 를 선형으로 끌어올린 사본. 원본은 건드리지 않는다."""
    metrics = dict(obs.metrics)
    base = metrics.get(TARGET_METRIC)
    if not isinstance(base, (int, float)):
        return obs
    added = delta_pp * frac
    metrics[TARGET_METRIC] = float(base) + added

    # 판정에 쓰이지는 않지만(explain 전용) 같이 움직여야 앞뒤가 맞는다. 사용률이
    # 올랐는데 여유 메모리가 그대로면 나중에 이 출력을 보는 사람이 헷갈린다.
    avail = metrics.get("mem_avail_mb")
    total = metrics.get("mem_total_mb")
    if isinstance(avail, (int, float)) and isinstance(total, (int, float)):
        metrics["mem_avail_mb"] = max(0.0, float(avail) - float(total) * added / 100.0)

    return replace(obs, metrics=metrics)


def _gap_pp(engine, obs: Observation) -> float | None:
    """「메모리 이상 증가」의 두 조건 중 **더 빡빡한 쪽**과의 격차(%p).

    룰은 `median + 5σ` AND `median + 5%p` 라 둘 중 높은 쪽이 실질 문턱이다.
    `Stats.threshold(k)` 를 그대로 쓴다 — 손으로 다시 만들면 그게 또 '내 산수'다.
    """
    stats = engine.baselines.stats(TARGET_METRIC, obs.foreground_program)
    if stats is None:
        return None
    value = obs.metrics.get(TARGET_METRIC)
    if not isinstance(value, (int, float)):
        return None
    sigma_thr = stats.threshold(5.0)
    if sigma_thr is None:
        return None
    threshold = max(sigma_thr, stats.median + 5.0)
    return float(value) - threshold


def run_one(observations: list[Observation], minutes: float, delta_pp: float,
            warm_s: float, detector: str) -> Outcome:
    """한 가지 램프 속도로 제품 탐지기를 돌린다.

    `per_program` 오버라이드는 호출자가 `ARGUS_DETECTION__PER_PROGRAM` 으로 건다.
    `build()` 를 흉내 내 `RuleEngine` 을 직접 조립하면 배선이 두 벌이 되고, 그게
    2026-08-04 에 `detection.*` 가 통째로 무시되던 버그를 만든 형태다.
    """
    engine = build(detector)
    engine.reset()

    start_ts = observations[0].ts
    ramp_start = start_ts + warm_s
    ramp_end = ramp_start + minutes * 60.0

    fired: list[tuple[float, str]] = []
    other: list[tuple[float, str]] = []
    closest_pp: float | None = None
    closest_at_s: float | None = None
    ramp_ticks = 0

    for obs in observations:
        if obs.ts < ramp_start:
            fed = obs                                   # 워밍 구간 — 원본 그대로
        else:
            frac = min(1.0, (obs.ts - ramp_start) / (minutes * 60.0))
            fed = _apply_ramp(obs, frac, delta_pp)
            ramp_ticks += 1

        detection = engine.observe(fed)

        if obs.ts >= ramp_start:
            # 격차는 룰 엔진이 본 것과 같은 관측(`flatten_gpus` 후)이어야 하나,
            # mem_percent 는 GPU 펼치기와 무관하므로 그대로 쓴다.
            gap = _gap_pp(engine, fed)
            if gap is not None and (closest_pp is None or gap > closest_pp):
                closest_pp = gap
                closest_at_s = obs.ts - ramp_start

        if detection is not None and obs.ts >= ramp_start:
            # **`rule` 이 아니라 `rules` 를 본다.** 한 틱에 여러 룰이 발화하면
            # `rules.py:430` 이 심각도가 가장 높은 것 하나만 대표로 세운다.
            # 「메모리 이상 증가」는 `info` 라, 게임이 도는 구간에서는 「CPU 과부하」에
            # 대표 자리를 뺏겨 **발화했는데도 미탐으로 집계된다**(2026-08-16 실측).
            names = detection.features.get("rules")
            if not isinstance(names, (list, tuple)):
                names = [detection.features.get("rule") or detection.detector]
            names = [str(n) for n in names]
            at = obs.ts - ramp_start
            if TARGET_RULE in names:
                fired.append((at, TARGET_RULE))
            for name in names:
                if name != TARGET_RULE:
                    other.append((at, name))

        if obs.ts > ramp_end:
            break

    return Outcome(
        minutes=minutes,
        fired=fired,
        other=other,
        closest_pp=closest_pp,
        closest_at_s=closest_at_s,
        ticks=len(observations),
        ramp_ticks=ramp_ticks,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", default="60",
                        help="램프 길이(분). 쉼표로 여러 개 (기본: 60)")
    parser.add_argument("--delta", type=float, default=DEFAULT_DELTA_PP,
                        help=f"총 증가폭 %%p (기본: {DEFAULT_DELTA_PP})")
    parser.add_argument("--warm-minutes", type=float, default=DEFAULT_WARM_MIN,
                        help=f"램프 전 베이스라인 구간(분) (기본: {DEFAULT_WARM_MIN})")
    parser.add_argument("--detector", default="rules",
                        help="탐지기 이름 (기본: rules)")
    parser.add_argument("--per-program", choices=("config", "on", "off", "both"),
                        default="both",
                        help="프로그램별 베이스라인. 이 PC 는 켜져 있고 배포 기본값은 "
                             "꺼짐이라 판정이 갈린다 (기본: both — 둘 다 잰다)")
    parser.add_argument("--end", type=float, default=None,
                        help="창의 끝 시각(epoch). 기본은 저장된 데이터의 마지막")
    args = parser.parse_args()

    lengths = [float(x) for x in args.minutes.split(",") if x.strip()]
    if not lengths:
        print("[FAIL] --minutes 가 비어 있다")
        return 1

    warm_s = args.warm_minutes * 60.0
    need_s = warm_s + max(lengths) * 60.0

    with Database() as db:
        replayer = Replayer(db)
        available = replayer.available_window()
        if available is None:
            print("[FAIL] metrics_raw 가 비어 있다")
            return 1

        end = args.end if args.end is not None else available.end
        window = Window(end - need_s, end)
        if window.start < available.start:
            print(f"[FAIL] 데이터가 부족하다 — {need_s / 60:.0f}분이 필요한데 "
                  f"{(available.end - available.start) / 60:.0f}분뿐이다")
            return 1

        print(f"창: {window}")
        observations = list(replayer.stream(window))

    if not observations:
        print("[FAIL] 그 구간에서 관측을 하나도 읽지 못했다")
        return 1

    have = observations[-1].ts - observations[0].ts
    print(f"관측 {len(observations)}개 · 실제 길이 {have / 60:.1f}분 "
          f"(워밍 {args.warm_minutes:.0f}분 + 램프 최대 {max(lengths):.0f}분)")
    print(f"램프 +{args.delta}%p · 탐지기 {args.detector!r} (설정 배선 그대로)")
    print()

    modes: list[tuple[str, str | None]]
    if args.per_program == "both":
        modes = [("per_program=off (배포 기본값)", "false"),
                 ("per_program=on (이 PC 설정)", "true")]
    elif args.per_program == "config":
        modes = [("설정값 그대로", None)]
    else:
        modes = [(f"per_program={args.per_program}", str(args.per_program == "on").lower())]

    verdicts: dict[str, tuple[bool, bool]] = {}     # 모드 → (느린 램프 발화, 빠른 램프 발화)

    for label, override in modes:
        if override is None:
            os.environ.pop("ARGUS_DETECTION__PER_PROGRAM", None)
        else:
            os.environ["ARGUS_DETECTION__PER_PROGRAM"] = override

        outcomes = [run_one(observations, m, args.delta, warm_s, args.detector)
                    for m in sorted(lengths)]

        print(f"── {label}")
        print(f"{'램프':>8} {'판정':>6}  {'최대 근접도':>12}  내용")
        print("-" * 72)
        for out in outcomes:
            if out.fired:
                at, rule = out.fired[0]
                verdict, detail = "[발화]", f"{rule} — 램프 시작 {at / 60:.1f}분 뒤"
            else:
                verdict = "[미탐]"
                if out.closest_pp is not None and out.closest_pp > 0:
                    # 문턱을 넘고도 안 나온 이유는 `for: 90s` 일 수도 쿨다운일 수도
                    # 있다. 제품 내부를 안 보고 하나로 단정하지 않는다.
                    detail = "문턱은 넘었으나 발화 없음 — 지속·쿨다운 확인 필요"
                else:
                    detail = "한 번도 문턱을 못 넘음"
            gap = f"{out.closest_pp:+.2f}%p" if out.closest_pp is not None else "판정불가"
            print(f"{out.minutes:>6.0f}분 {verdict:>6}  {gap:>12}  {detail}")
            if out.other:
                names = sorted({r for _, r in out.other})
                print(f"{'':>8} {'':>6}  {'':>12}  └ 램프와 무관한 발화(집계 제외): "
                      f"{', '.join(names)}")
        print()

        slow = [o for o in outcomes if o.minutes >= 60]
        fast = [o for o in outcomes if o.minutes <= 10]
        verdicts[label] = (any(o.fired for o in slow) if slow else False,
                           any(o.fired for o in fast) if fast else False)

    os.environ.pop("ARGUS_DETECTION__PER_PROGRAM", None)

    for label, (slow_fired, fast_fired) in verdicts.items():
        if not slow_fired and fast_fired:
            print(f"[OK] {label}: 이길 상대가 실재한다 — 빠른 램프는 잡고 느린 램프는 놓친다")
        elif not slow_fired:
            print(f"[?] {label}: 느린 램프를 놓쳤지만 대조군이 없다 — "
                  "`--minutes 5,60` 으로 룰이 살아 있는지 함께 확인할 것")
        else:
            print(f"[!] {label}: 느린 램프도 룰이 잡았다 — Phase 5·7 의 보류 사유가 뒤집힌다")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

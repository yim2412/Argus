"""스코어보드 CLI.

    python -m argus.eval --detector all
    python -m argus.eval --detector fixed_cpu --scenario memory_leak --save

결함 주입으로 만든 정답 구간 위에서 탐지기를 리플레이하고 채점해 리포트를 낸다.
네트워크도, 실시간 대기도 필요 없다 — 6시간 구간을 1초 안에 재생한다.
"""

from __future__ import annotations

import argparse
import sys

from ..detection import registry
from ..detection.base import run_detector
from ..logging_setup import setup
from ..storage.hot import Database
from . import scoring
from .replay import Replayer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m argus.eval",
        description="탐지기를 저장된 데이터 + 결함 주입 라벨로 채점한다.",
    )
    parser.add_argument(
        "--detector", default="all",
        help="탐지기 이름(쉼표 구분) 또는 all. 기본: all",
    )
    parser.add_argument(
        "--scenario", default=None,
        help="채점할 시나리오만 남긴다(쉼표 구분). 기본: 전부",
    )
    parser.add_argument(
        "--full-window", action="store_true",
        help="주입 구간 주변이 아니라 저장된 전체 구간을 재생한다(오탐률 측정에 유리)",
    )
    parser.add_argument("--save", action="store_true", help="결과를 eval_runs 에 남긴다")
    parser.add_argument("--list", action="store_true", help="탐지기 목록")
    parser.add_argument(
        "--attribution", action="store_true",
        help="탐지 대신 귀인을 채점한다 — 원인 프로세스를 1순위로 지목했는가",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name in registry.names():
            print(f"  {name}")
        return 0

    if args.attribution:
        return _run_attribution(args)

    names = registry.names() if args.detector == "all" else [
        n.strip() for n in args.detector.split(",") if n.strip()
    ]
    scenarios = [s.strip() for s in args.scenario.split(",")] if args.scenario else None

    setup(level="WARNING")
    with Database() as db:
        replayer = Replayer(db)
        available = replayer.available_window()
        if available is None:
            print("[FAIL] 재생할 데이터가 없다 — 먼저 `python -m argus` 로 수집할 것")
            return 1

        window = available if args.full_window else (
            replayer.window_around_injections() or available
        )
        episodes = scoring.load_episodes(db, window, scenarios)
        if not episodes:
            print("[FAIL] 정답 라벨이 없다 — 먼저 결함을 주입할 것:")
            print("       python tools/fault_injector.py memory_leak --duration 180 --ramp")
            return 1

        observations = list(replayer.stream(window))
        stats = replayer.stats

        # 관측이 없는 정답 구간은 채점에서 뺀다. 조용히 빼면 안 된다 — 몇 건으로
        # 채점했는지 모르면 숫자를 믿을 수 없다(CLAUDE.md: 조용히 실패하지 않는다).
        episodes, dropped = scoring.filter_covered(episodes, observations)

        print(f"  재생 구간 : {window}")
        print(f"  관측      : {len(observations)}틱  (신뢰 불가 {stats.get('suspect', 0)}틱)")
        print(f"  정답 구간 : {len(episodes)}건 — " + ", ".join(
            f"{e.scenario}{'(ramp)' if e.ramp else ''} {e.duration_s / 60:.1f}분" for e in episodes
        ))
        for episode, reason in dropped:
            print(f"  ⚠ 제외    : {episode.scenario} #{episode.id} — {reason}")
        if not episodes:
            print("[FAIL] 관측이 있는 정답 구간이 하나도 없다 — 수집이 도는 중에 다시 주입할 것")
            return 1
        print()

        results = []
        for name in names:
            try:
                detector = registry.build(name)
            except KeyError as exc:
                print(f"[FAIL] {exc}")
                return 1
            detections = run_detector(detector, observations)
            result = scoring.score(
                name, detections, episodes, window, observations=observations
            )
            results.append(result)
            if args.save:
                scoring.persist(
                    db, result,
                    params={"observations": len(observations), "suspect_ticks": stats.get("suspect", 0)},
                    notes="python -m argus.eval",
                )

        print(scoring.format_report(results))
        if args.save:
            print(f"\n  eval_runs 에 {len(results)}행 기록.")

    return 0


# DoD 85% 를 가를 수 있는 최소 표본. 6/7 = 85.7% 가 이 문턱을 넘는 가장 작은 조합이다.
MIN_SCORED = 7


def _run_attribution(args) -> int:
    """귀인 채점. 리플레이가 필요 없다 — 원본 프로세스 메트릭을 직접 본다."""
    from . import attribution

    setup(level="WARNING")
    scenarios = [s.strip() for s in args.scenario.split(",")] if args.scenario else None

    with Database() as db:
        verdicts = attribution.score_all(db, scenarios=scenarios)
        if not verdicts:
            print("[FAIL] 결함 주입 라벨이 없다 — 먼저 주입할 것:")
            print("       python tools/fault_injector.py cpu_spin --duration 300 --ramp")
            return 1
        print(attribution.report(verdicts))

        # **제품 경로로도 채점한다.** 위 수치는 자원(`handles` 등)을 라벨에서 입력으로
        # 받은 것이라 "자원을 알려주면 원인을 찾는가"를 재고, 제품이 하는 일은 아니다.
        # 아래가 사용자가 실제로 얻는 결과다.
        product = attribution.score_all_product(db, scenarios=scenarios)
        print(attribution.report_product(product))

        scored = [v for v in verdicts if not v.skipped]
        if not scored:
            return 1
        rate = sum(1 for v in scored if v.is_top1) / len(scored)
        print()
        # **표본이 적으면 판정하지 않는다.** 2건으로는 0·50·100% 밖에 나올 수 없어
        # 100% 가 나와도 우연과 구별되지 않는다. 85% 를 가르려면 최소 7건이 필요하다
        # (6/7 = 85.7%). 2026-07-29 에 보존 정리가 주입 22건의 프로세스 메트릭을
        # 지워 채점 가능한 것이 2건만 남았는데, 그때도 이 판정은 "충족"을 찍었다 —
        # **근거가 사라진 것을 합격으로 보고하는 스코어보드는 없느니만 못하다.**
        if len(scored) < MIN_SCORED:
            print(
                f"[보류] 채점 표본 {len(scored)}건 — {MIN_SCORED}건 미만이라 DoD 를 판정하지 "
                f"않는다 (1순위 지목률은 {rate * 100:.1f}%)"
            )
            print("       주입을 더 쌓을 것: python tools/fault_injector.py handle_leak --duration 720")
            return 1

        # **함수 경로만으로 DoD 를 닫지 않는다.** 그 수치는 자원을 알려준 상태의 것이고,
        # 2026-07-30 에 함수 100% 와 제품 0% 가 공존했다. 제품 경로 수치를 함께 낸다.
        p_scored = [v for v in product if not v.skipped]
        p_rate = (
            sum(1 for v in p_scored if v.is_top1) / len(p_scored) if p_scored else 0.0
        )
        no_incident = sum(
            1 for v in product if v.skipped and "사건이 만들어지지 않음" in v.skipped
        )

        if rate < 0.85:
            print(
                f"[FAIL] Phase 8 DoD 미달 — 함수 경로 1순위 지목률 {rate * 100:.1f}% < 85% "
                f"(표본 {len(scored)}건)"
            )
            return 1

        print(
            f"[OK] 함수 경로 DoD 충족 — 1순위 지목률 {rate * 100:.1f}% ≥ 85% (표본 {len(scored)}건)"
        )
        if not p_scored:
            print("[보류] 제품 경로는 채점할 사건이 없어 판정하지 않는다.")
            return 1
        if len(p_scored) < MIN_SCORED:
            print(
                f"[보류] 제품 경로 표본 {len(p_scored)}건 — {MIN_SCORED}건 미만이라 판정하지 "
                f"않는다 (지목률은 {p_rate * 100:.1f}%)"
            )
        elif p_rate >= 0.85:
            print(
                f"[OK] 제품 경로 DoD 충족 — 지목률 {p_rate * 100:.1f}% ≥ 85% "
                f"(표본 {len(p_scored)}건)"
            )
        else:
            print(
                f"[FAIL] 제품 경로 DoD 미달 — 지목률 {p_rate * 100:.1f}% < 85% "
                f"(표본 {len(p_scored)}건)"
            )
            return 1
        if no_incident:
            print(
                f"[주의] 주입 {no_incident}건은 사건이 만들어지지 않아 사용자가 아무것도 "
                f"받지 못한다 — 귀인이 아니라 탐지 쪽 문제다."
            )
        return 0


if __name__ == "__main__":
    sys.exit(main())

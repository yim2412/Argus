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

        scored = [v for v in verdicts if not v.skipped]
        if not scored:
            return 1
        rate = sum(1 for v in scored if v.is_top1) / len(scored)
        print()
        if rate >= 0.85:
            print(f"[OK] Phase 8 DoD 충족 — 1순위 지목률 {rate * 100:.1f}% ≥ 85%")
            return 0
        print(f"[FAIL] Phase 8 DoD 미달 — 1순위 지목률 {rate * 100:.1f}% < 85%")
        return 1


if __name__ == "__main__":
    sys.exit(main())

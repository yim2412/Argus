"""하루치를 웜에서 리플레이해 **룰이 그날 무엇을 했는지** 본다.

    .venv\\Scripts\\python.exe tools\\replay_day.py --day 2026-09-09
    .venv\\Scripts\\python.exe tools\\replay_day.py --day 2026-09-09 --from 14:00 --to 16:00

**이게 없어서 2026-09-08 을 조사할 수 없었다.** 부하는 평소와 같았는데(CPU 평균
34%·최대 100%·GPU 96도) 신호가 0건이라 이상했지만, 확인하려는 시점에 초 단위 원본이
이미 보존 기한(24시간)에 지워진 뒤였다. 웜에 원본을 두기 시작한 것이 그래서다.

**상주와 같은 구성으로 돈다.** `detection.detector` 설정을 그대로 읽어 같은 탐지기를
세운다 — CLAUDE.md 에 적힌 대로, 재는 대상이 제품보다 좁으면 결론은 항상 "제품이 못
잡는다" 쪽으로 기운다. 2026-08-16 에 하루 세 번 그랬다.

**앞부분은 베이스라인이 차는 중이다.** 상주는 재시작할 때 저장된 최근 구간으로
베이스라인을 데우지만(`live.setup` 의 `warm`), 과거를 재생할 때는 그 시점의 "최근"이
없다. 그래서 창(`detection.baseline_window_s`, 기본 30분)만큼은 판정이 아직 서지 않은
구간으로 보고 따로 표시한다 — 이걸 모르고 "앞부분에 아무것도 없다"를 결론으로 읽으면
그건 재는 대상을 잘못 본 것이다.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.config.loader import load_settings  # noqa: E402
from argus.detection.registry import build  # noqa: E402
from argus.detection.trace import LABELS, RuleTrace  # noqa: E402
from argus.eval.replay import Replayer, Window  # noqa: E402
from argus.logging_setup import setup  # noqa: E402
from argus.storage.hot import Database  # noqa: E402
from argus.storage.replay_db import WarmReplayDatabase  # noqa: E402


#: 이 도구가 살아 있는지 확인하는 법.
#:
#: **"0건"을 결과로 쓰기 전에 반드시 이걸 먼저 돌린다.** 2026-08-17 에 정상 구간
#: 오탐을 12회 재고 전부 "발화 0건"을 얻었는데, 실제로는 도구가 죽어 있었다. 그대로
#: 믿었으면 "오탐 0건이므로 안전하다"가 결론이 됐다. 결함 주입이 있던 날에는 반드시
#: 발화가 나와야 하고, 거기서도 0건이면 재고 있는 것이 없다는 뜻이다.
CHECK_HINT = "tools\\replay_day.py --check"


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _hhmm(day: str, value: str) -> float:
    hour, _, minute = value.partition(":")
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, int(hour), int(minute or 0)).timestamp()


#: 룰별 판정 표의 열 순서와 짧은 머리글. `unready` 는 빼 둔다 — 베이스라인이 서기 전
#: 구간은 위에서 따로 세고 있어, 여기 또 넣으면 같은 것을 두 번 읽게 된다.
#: 긴 이름은 `LABELS` 에 있고 아래 "한 줄 판정"이 그 뜻을 풀어 준다.
OUTCOME_COLS = (
    ("unknown", "불가"),
    ("false", "거짓"),
    ("holding", "지속미달"),
    ("cooldown", "쿨다운"),
    ("fired", "발화"),
)


def _width(text: str) -> int:
    """터미널에서 차지하는 칸 수. **한글은 두 칸이다.**

    `str.ljust` 는 글자 수로 세므로 한글이 섞이면 표가 통째로 어긋난다 — 값이 맞아도
    읽을 수 없으면 진단 도구로 쓸모가 없다.
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return fill + text if right else text + fill


def _print_trace(trace: RuleTrace) -> None:
    """룰별로 어느 관문에서 멈췄는지. **가까이 간 룰부터** 보여준다."""
    stats = trace.ordered()
    if not stats:
        print("  [주의] 룰이 한 번도 평가되지 않았다 — 재고 있는 것이 없다는 뜻이다.")
        print()
        return

    name_w = max(24, max(_width(s.name) for s in stats) + 2)
    print("  룰별 판정 (가까이 간 순서)  — 숫자는 틱 수")
    print("    " + _pad("룰", name_w) + "".join(_pad(h, 10, right=True) for _, h in OUTCOME_COLS))
    for stat in stats:
        row = "    " + _pad(stat.name, name_w)
        row += "".join(
            _pad(f"{stat.counts.get(key, 0):,}", 10, right=True) for key, _ in OUTCOME_COLS
        )
        print(row)
    print()
    print("    " + " · ".join(f"{short}={LABELS[key]}" for key, short in OUTCOME_COLS))
    print()
    print("  한 줄 판정")
    for stat in stats:
        print("    " + _pad(stat.name, name_w) + stat.verdict)
    print()


def _injection_window(days: list[str]) -> tuple[str, str, str] | None:
    """웜에 있으면서 결함 주입이 있었던 날 하나와 그 구간(`HH:MM`).

    가장 최근 것을 고른다 — 오래된 날일수록 그 시절 룰로 잡히던 것이라, 지금 코드의
    생존 확인으로는 최근이 낫다.
    """
    with Database() as db:
        rows = db.query(
            "SELECT ts_start, ts_end FROM fault_injections "
            "WHERE completed = 1 AND ts_end IS NOT NULL ORDER BY ts_start DESC"
        )
    for row in rows:
        start = datetime.fromtimestamp(float(row["ts_start"]))
        end = datetime.fromtimestamp(float(row["ts_end"]))
        if start.date().isoformat() not in days or start.date() != end.date():
            continue
        # 주입 앞뒤로 여유를 준다. 베이스라인이 서야 판정이 나온다.
        lo = max(datetime(start.year, start.month, start.day), start - timedelta(hours=1))
        return start.date().isoformat(), lo.strftime("%H:%M"), (end + timedelta(minutes=10)).strftime("%H:%M")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="웜의 초 단위 원본으로 하루를 리플레이한다")
    parser.add_argument("--day", help="YYYY-MM-DD (기본: 웜에 있는 가장 최근 날짜)")
    parser.add_argument("--from", dest="start", help="HH:MM 부터")
    parser.add_argument("--to", dest="end", help="HH:MM 까지")
    parser.add_argument(
        "--detector",
        help="쉼표 구분. 기본은 detection.detector 설정 — **바꾸면 상주와 다른 구성이다**",
    )
    parser.add_argument("--list", action="store_true", help="리플레이 가능한 날짜만 보여준다")
    parser.add_argument(
        "--check",
        action="store_true",
        help="결함 주입이 있던 날을 재생해 **발화가 나오는지** 본다 (도구 생존 확인)",
    )
    parser.add_argument(
        "--why",
        action="store_true",
        help="룰별로 **어느 관문에서 멈췄는지** 함께 찍는다 (rules 탐지기만)",
    )
    args = parser.parse_args()

    setup(level="WARNING")

    days = WarmReplayDatabase.available_days()
    if args.list or not days:
        print(f"  리플레이 가능한 날짜 {len(days)}일")
        for d in days:
            print(f"    {d}")
        if not days:
            print("  (웜에 초 단위 원본이 없다. 상주가 하루 지난 날짜부터 내보낸다)")
        return 0 if days else 1

    settings = load_settings()

    if args.check:
        picked = _injection_window(days)
        if picked is None:
            print("[FAIL] 웜에 결함 주입이 있는 날짜가 없다 — 생존 확인을 할 수 없다.")
            print("       그 상태에서 나온 '발화 0건'은 근거로 쓸 수 없다.")
            return 1
        day, args.start, args.end = picked
        print(f"  [생존 확인] 주입이 있던 {day} {args.start}~{args.end} 를 재생한다.")
        print("  여기서 0건이면 재고 있는 것이 없다는 뜻이다.")
        print()
    else:
        day = args.day or days[-1]

    if day not in days:
        print(f"[FAIL] {day} 는 웜에 없다. 있는 날짜: {days}")
        return 1
    names = [
        n.strip()
        for n in (args.detector or settings.detection.detector).split(",")
        if n.strip()
    ]
    window_s = float(settings.detection.baseline_window_s)

    d = date.fromisoformat(day)
    day_start = datetime(d.year, d.month, d.day).timestamp()
    start = _hhmm(day, args.start) if args.start else day_start
    end = _hhmm(day, args.end) if args.end else (day_start + 86400)

    detectors = []
    trace = RuleTrace() if args.why else None
    traced = False
    for name in names:
        try:
            det = build(name)
            det.reset()
            if trace is not None and hasattr(det, "trace"):
                det.trace = trace
                traced = True
            detectors.append(det)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] 탐지기 '{name}' 를 세우지 못했다: {exc}")
            return 1

    if trace is not None and not traced:
        # **조용히 빈 표를 내지 않는다.** 추적을 켰는데 아무 탐지기도 안 받으면
        # "이유가 없다"가 아니라 "재고 있는 것이 없다"는 뜻이다(규칙 4).
        print("[FAIL] --why 를 켰지만 추적을 받는 탐지기가 없다.")
        print(f"       지금 구성: {names} — 추적은 'rules' 만 지원한다.")
        return 1

    print(f"  날짜 {day}  구간 {_clock(start)} ~ {_clock(end)}")
    print(f"  탐지기 {[d.name for d in detectors]}  (설정: {settings.detection.detector})")

    with Database() as hot, WarmReplayDatabase.for_day(hot, day) as warm:
        observations = list(Replayer(warm).stream(Window(start, end)))

    if not observations:
        print("[FAIL] 관측이 0개다 — 그날 그 구간에 데이터가 없다")
        return 1

    warm_until = observations[0].ts + window_s
    fired: list[tuple[float, str, float, str]] = []
    by_detector: Counter = Counter()
    best: dict[str, float] = {}
    suspect = 0
    cold = 0

    for obs in observations:
        if obs.suspect:
            suspect += 1
        for det in detectors:
            try:
                detection = det.observe(obs)
            except Exception as exc:  # noqa: BLE001
                print(f"  [경고] {det.name} 가 {_clock(obs.ts)} 에서 예외: {exc}")
                continue
            if detection is None:
                # **`observe()` 는 발화할 때만 `Detection` 을 돌려준다.** 그래서 이
                # 도구는 "문턱에 얼마나 가까웠나"(근접도)를 보여줄 수 없다 — 안 잡힌
                # 이유를 여기서 읽으려 하면 안 된다. 근접도가 필요하면 탐지기가 매 틱의
                # 점수를 내놓아야 하고, 그건 이 도구가 아니라 탐지기 쪽 작업이다.
                continue
            best[det.name] = max(best.get(det.name, 0.0), detection.score)
            if obs.ts < warm_until:
                cold += 1
                continue
            rules = detection.features.get("rules") or detection.features.get("rule")
            label = ", ".join(rules) if isinstance(rules, list) else str(rules or det.name)
            fired.append((detection.ts, det.name, detection.score, label))
            by_detector[det.name] += 1

    print()
    print(f"  관측 {len(observations):,}개  ({_clock(observations[0].ts)} ~ {_clock(observations[-1].ts)})")
    print(f"  신뢰 불가 구간(공백 직후) {suspect:,}개")
    print(f"  베이스라인 채우는 중이라 제외한 판정 {cold:,}건 (~{_clock(warm_until)} 까지)")
    print()

    if trace is not None:
        _print_trace(trace)

    if not fired:
        print("  발화 0건 — 어떤 룰도 문턱을 넘지 않았다.")
        if trace is None:
            print("  **왜** 안 넘었는지는 --why 로 본다 (룰별로 어느 관문에서 멈췄는지).")
        print()
        print("  0건을 결론으로 쓰기 전에 도구가 살아 있는지 확인할 것:")
        print(f"    {CHECK_HINT}")
        if args.check:
            print()
            print("[FAIL] 주입 구간인데 발화가 0건이다 — 이 도구는 지금 아무것도 재지 못한다.")
            return 1
        return 0

    print(f"  발화 {len(fired)}건")
    for name, count in by_detector.most_common():
        print(f"    {name:12} {count:>4}건  최고 점수 {best.get(name, 0):.3f}")
    print()
    print("  발화 목록 (앞 30건):")
    for ts, name, score, label in fired[:30]:
        print(f"    {_clock(ts)}  {name:10} {score:.3f}  {label}")
    if len(fired) > 30:
        print(f"    ... 그리고 {len(fired) - 30}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

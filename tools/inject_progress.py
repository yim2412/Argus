"""주입 배치가 지금 어디까지 왔고, **이 주입이 이미 죽었는지**를 한 화면으로.

배치는 75분이 걸린다. 2026-07-30 오전에 5회 중 3회가 미탐이었는데 **그 사실을 75분이
지나서야 알았다.** 실패는 1회차 2분 시점에 이미 관측 가능했다 — 첫 표본이 40~50초 늦게
잡혀 `first` 가 809~1,864 로 부풀었고, 그래서 배수가 2.25~2.89 로 문턱 3.0 을 못 넘었다.

**판정 기준은 최종 기준에서 유도한다.** 최종 판정은 "1위 기여자가 주입 프로세스"이고,
거기까지 가는 사슬은 이렇다.

    표본 → first → 배수 → 발화 → 사건 → 귀인 1위

중간 점검은 이 사슬의 **고리**를 재야 한다. 처음에는 "표본 밀도(행/초)"로 판정했는데
그건 사슬 밖의 대리 지표라 **틀렸다** — 주입이 상한에 닿으면 더 이상 늘지 않아 핸들
증가량 상위에서 빠지고, 그래서 밀도가 떨어지는 것이 **정상**이다. 정상 실행에 경고를
띄웠다. 지금은 `first` 와 배수를 직접 본다.

문턱은 `config` 에서 읽는다. 여기 숫자를 박으면 튜닝이 두 곳으로 갈린다.

사용:
    python tools/inject_progress.py              # 한 번 찍고 끝
    python tools/inject_progress.py --watch      # 5초마다 갱신 (Ctrl+C 로 종료)
    python tools/inject_progress.py --ids 39,40  # 특정 주입만 (도구 보정용)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.logging_setup import setup  # noqa: E402
from argus.paths import db_path  # noqa: E402
from argus.storage.hot import Database  # noqa: E402

BAR_WIDTH = 24


def _bar(done: float) -> str:
    filled = int(round(max(0.0, min(1.0, done)) * BAR_WIDTH))
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _planned_seconds(row: dict) -> float | None:
    """주입기가 라벨에 남긴 계획 지속 시간. 없으면 None."""
    import json

    try:
        params = json.loads(row.get("params") or "{}")
    except (TypeError, ValueError):
        return None
    value = (params.get("limits") or {}).get("duration_s")
    return float(value) if value else None


# 첫 표본이 이만큼 늦게 잡히면 `first` 가 이미 부풀어 배수를 믿을 수 없다.
# 2026-07-30 오전 실패값이 40~50초, 수정 후가 0~1초라 그 사이에서 넉넉히 잡는다.
MAX_FIRST_SAMPLE_LAG_S = 5.0


def _verdict(db: Database, row: dict, start: float) -> str:
    """이 주입이 탐지될 상태인가. **`procleak.judge()` 를 그대로 부른다.**

    처음에는 `first`·배수를 SQL 로 직접 계산했는데 **그것도 대리 지표였다.** `procleak` 은
    자기 추적 창에 쌓인 표본으로 판정하므로 값이 다르다 — 2026-07-30 오전 #39 에서 SQL
    계산은 배수 15.9(통과), 실제 판정은 5.2 였다. 다른 회차에서는 방향까지 갈려서, 도구가
    실제로는 발화한 주입에 ❌ 를 띄웠다(과거 데이터로 보정하다 잡았다).

    사슬의 고리를 재라는 원칙은 **판정 함수 자체를 부르는 것**까지 가야 한다. 규칙과 문턱은
    `config` 에서 읽으므로 튜닝을 고치면 이 도구도 따라온다.
    """
    from argus.config.loader import load_settings
    from argus.detection.procleak import _Track, judge, rules_from_settings

    cfg = load_settings().process_leak
    rules = {r.attr: r for r in rules_from_settings(cfg)}
    rule = rules.get("handles")
    if rule is None:
        return "❌ handles 규칙이 config 에 없다"

    # **상한을 반드시 건다.** PID 는 재사용된다 — 상한이 없으면 나중에 같은 번호를 받은
    # 다른 프로세스의 값이 섞여 들어와, 값이 뚝 떨어지는 것으로 보이고 트랙이 리셋된다
    # (보정 중 #42 가 "표본 부족 4/20" 으로 나왔다. 실제로는 34행이 있었다).
    end = float(row["ts_end"]) if row["ts_end"] else time.time()
    rows = db.query(
        "SELECT ts, handles FROM process_metrics "
        "WHERE pid = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (row["pid"], start, end),
    )
    samples = [(float(r["ts"]), float(r["handles"])) for r in rows if r["handles"] is not None]
    if not samples:
        return "❌ 표본 없음 — 이 프로세스가 저장 대상에서 빠지고 있다"

    track = _Track()
    for ts, value in samples:
        track.add(ts, value, window_s=cfg.window_s, drop_ratio=cfg.drop_reset_ratio)

    v = judge(track, rule, min_duration_s=cfg.min_duration_s, min_samples=cfg.min_samples)
    lag = samples[0][0] - start
    text = (
        f"표본 {len(samples):>4}행 · 첫 표본 +{lag:.0f}초 · "
        f"{v.first:.0f} → {v.last:.0f} · 배수 {v.ratio:.1f} (문턱 {rule.growth_ratio:g}) · {v.reason}"
    )
    if v.leaking:
        return "✅ " + text
    # **진행 중인 주입의 "아직 못 채웠다"는 실패가 아니다.** 지속·표본 조건은 시간이
    # 지나야 채워지므로, 도는 중에 ❌ 를 띄우면 정상 실행을 실패로 읽게 된다.
    if row["ts_end"] is None and ("지속 부족" in v.reason or "표본 부족" in v.reason):
        return "⏳ " + text + "  ← 조건을 채우는 중"
    return "❌ " + text


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}초"
    return f"{seconds // 60}분 {seconds % 60}초"


def render(db: Database, *, batch_gap_s: float = 600.0,
           ids: list[int] | None = None) -> str:
    """마지막 배치(주입들이 연달아 붙어 있는 묶음)의 진행 상황."""
    if ids:
        rows = [
            dict(r)
            for i in ids
            for r in db.query(
                "SELECT id, scenario, ts_start, ts_end, pid, completed, params "
                "FROM fault_injections WHERE id = ?",
                (i,),
            )
        ]
        rows.sort(key=lambda r: r["ts_start"], reverse=True)
    else:
        rows = [
            dict(r)
            for r in db.query(
                "SELECT id, scenario, ts_start, ts_end, pid, completed, params "
                "FROM fault_injections ORDER BY ts_start DESC LIMIT 40"
            )
        ]
    if not rows:
        return "[FAIL] 주입 기록이 없다 — tools/fault_injector.py 로 먼저 주입할 것"

    # 배치 = 앞 주입 끝에서 `batch_gap_s` 안에 시작한 것들. 배치 사이 대기(180초)보다
    # 넉넉히 크고, 다른 날 주입과는 섞이지 않을 만큼 작다.
    now = time.time()
    if ids:
        batch = list(reversed(rows))
        return _render_rows(db, batch, now)
    batch = [rows[0]]
    for row in rows[1:]:
        prev_start = batch[-1]["ts_start"]
        if prev_start - (row["ts_end"] or row["ts_start"]) <= batch_gap_s:
            batch.append(row)
        else:
            break
    batch.reverse()
    return _render_rows(db, batch, now)


def _render_rows(db: Database, batch: list[dict], now: float) -> str:
    # **배치가 언제 것인지 헤더에 적는다.** 처음에는 "마지막 갱신 <지금 시각>"만 있었는데,
    # 이 도구는 마지막 배치를 보여주므로 **13일 전에 끝난 배치를 보면서 지금 시각을
    # 읽게 된다**(2026-08-15 실측). 지금 도는 것이라고 오해할 자리다.
    started = time.strftime("%m-%d %H:%M", time.localtime(float(batch[0]["ts_start"])))
    lines = [
        f"주입 배치 — {len(batch)}회차 · {started} 시작"
        f" (조회 {time.strftime('%H:%M:%S')})",
        "=" * 66,
    ]
    total_planned = 0.0
    total_done = 0.0

    for index, row in enumerate(batch, start=1):
        start = float(row["ts_start"])
        end = row["ts_end"]
        running = end is None
        span = (now if running else float(end)) - start
        # **계획 길이는 라벨에 있다.** 주입기가 `params.limits.duration_s` 로 남긴다.
        # 끝난 회차의 실측에서 가져오면 첫 회차가 늘 100% 로 보인다(계획을 모르니 지금까지
        # 흐른 시간을 계획으로 착각한다).
        planned = _planned_seconds(row) or span
        total_planned += planned
        # **끝난 회차는 계획을 다 쓴 것으로 센다.** 실측 길이로 세면 중단되어 짧게 끝난
        # 회차가 "덜 진행됨"으로 잡혀, 개별 줄이 전부 `완료 100%` 인데 전체만 85% 가
        # 된다(2026-08-15 실측: 겹쳐서 강제 중단한 #51·#52 가 그랬다). 그 회차는 덜 돈
        # 것이 아니라 **더 돌 계획이 없는 것**이다. 짧게 끝났다는 사실은 개별 줄의
        # `3분 13초 / 12분 0초` 와 판정 ❌ 가 이미 말한다.
        total_done += min(span, planned) if running else planned

        if running:
            state = f"진행 중 {_bar(span / planned)} {span / planned * 100:>3.0f}%"
        else:
            state = f"완료     {_bar(1.0)} 100%"
        lines.append(
            f"{index}. #{row['id']:<3} {row['scenario']:<12} {state}  "
            f"{_hms(span):>9} / {_hms(planned)}"
        )
        lines.append("      " + _verdict(db, row, start))

    remaining = max(0.0, total_planned - total_done)
    # 남은 회차 사이 대기(180초)는 계획에 없으므로 대략만 더한다.
    pending = sum(1 for r in batch if r["ts_end"] is None)
    share = (total_done / total_planned) if total_planned else 0.0
    # **끝난 배치에 "남음"을 적지 않는다.** 진행을 재는 도구가 끝난 것을 두고 "남음 약
    # 17분"이라고 말하면 그건 추측 보고다(전역 규칙 1). 읽는 사람은 뭔가 더 돌 것이
    # 있다고 읽는다 — 2026-08-15 에 실제로 그렇게 읽혔다.
    if pending:
        tail = f"남음 약 {_hms(remaining)}  (다음 회차 대기 시간은 제외)"
    else:
        last_end = max(float(r["ts_end"]) for r in batch)
        tail = f"배치 종료 — 마지막 회차 {time.strftime('%m-%d %H:%M', time.localtime(last_end))}"
    lines += ["-" * 66, f"전체 {_bar(share)} {share * 100:>3.0f}%   {tail}"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="5초마다 갱신")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--ids", help="특정 주입 id 만 (쉼표 구분). 도구 보정에 쓴다")
    args = parser.parse_args()
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None

    setup(level="WARNING")
    with Database(db_path()) as db:
        if not args.watch:
            print(render(db, ids=ids))
            return 0
        try:
            while True:
                print("\033[2J\033[H" + render(db), flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

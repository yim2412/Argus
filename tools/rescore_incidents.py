"""저장된 사건을 현재 코드로 다시 분석해 **전후를 비교한다.**

왜 필요한가: 귀인·병목 판정을 고쳤을 때 "좋아졌다"를 느낌이 아니라 수치로 말해야 한다.
사건은 이미 DB 에 있고 원본 지표도(주입 구간은 보존 정리가 지킨다) 남아 있으므로,
같은 입력에 새 코드를 돌려 제목과 1위 기여자가 어떻게 바뀌는지 그대로 볼 수 있다.

**DB 를 수정하지 않는다.** `fusion.analyze_incident` 가 읽기 전용이라 그대로 쓴다.
계산을 여기 복사하지 않는 것이 요점이다 — 규칙이 두 곳에 있으면 조용히 갈린다.

사용:
    python tools/rescore_incidents.py                    # 최근 24시간
    python tools/rescore_incidents.py --hours 6
    python tools/rescore_incidents.py --ids 12,13,14
    python tools/rescore_incidents.py --faults           # 결함 주입 구간과 겹치는 것만
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config.loader import load_settings  # noqa: E402
from argus.decide.fusion import FusionSettings, analyze_incident  # noqa: E402
from argus.logging_setup import setup  # noqa: E402
from argus.storage.hot import Database  # noqa: E402


def _fault_windows(db: Database) -> list[tuple[float, float]]:
    rows = db.query("SELECT ts_start, ts_end FROM fault_injections WHERE ts_end IS NOT NULL")
    return [(float(r["ts_start"]), float(r["ts_end"])) for r in rows]


def _overlaps(lo: float, hi: float, windows: list[tuple[float, float]]) -> bool:
    return any(lo <= w_hi and hi >= w_lo for w_lo, w_hi in windows)


def _fusion_settings() -> FusionSettings:
    """제품이 쓰는 것과 같은 판정 문턱. 기본값으로 재분석하면 config 를 고친 사용자의
    사건과 결과가 갈려, "고친 뒤 어떻게 달라지나"를 보려는 이 도구의 목적이 깨진다."""
    cfg = load_settings()
    return FusionSettings(bottleneck=cfg.bottleneck, incident=cfg.incident)


def _top(contributors) -> str:
    if not contributors:
        return "(없음)"
    first = contributors[0]
    name = first["name"] if isinstance(first, dict) else first.name
    share = first["share"] if isinstance(first, dict) else first.share
    return f"{name} {share * 100:.0f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0, help="이 시간 안의 사건만")
    parser.add_argument("--ids", help="쉼표로 구분한 사건 id (지정하면 --hours 무시)")
    parser.add_argument("--faults", action="store_true", help="결함 주입 구간과 겹치는 것만")
    args = parser.parse_args()

    setup(level="WARNING")
    changed = 0
    total = 0

    with Database() as db:
        if args.ids:
            ids = [int(x) for x in args.ids.split(",") if x.strip()]
            rows = [
                dict(r)
                for i in ids
                for r in db.query("SELECT * FROM incidents WHERE id = ?", (i,))
            ]
        else:
            rows = [
                dict(r)
                for r in db.query(
                    "SELECT * FROM incidents WHERE ts_start > ? AND ts_end IS NOT NULL "
                    "ORDER BY ts_start",
                    (time.time() - args.hours * 3600,),
                )
            ]

        if args.faults:
            windows = _fault_windows(db)
            rows = [r for r in rows if _overlaps(r["ts_start"], r["ts_end"] or r["ts_start"], windows)]

        if not rows:
            print("[FAIL] 다시 분석할 사건이 없다 (구간을 넓히거나 --ids 를 지정할 것)")
            return 1

        print(f"저장된 사건 {len(rows)}건을 현재 코드로 다시 분석한다 (DB 는 바꾸지 않는다)\n")
        for row in rows:
            total += 1
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(row["ts_start"]))
            old_title = row["title"] or ""
            try:
                old_top = _top(json.loads(row["contributors"] or "[]"))
            except (TypeError, ValueError):
                old_top = "(없음)"

            analysis = analyze_incident(
                db, int(row["id"]), float(row["ts_end"]), _fusion_settings()
            )
            if analysis is None:
                print(f"  [{row['id']}] {stamp}  분석 불가")
                continue
            new_title = analysis.title
            new_top = _top(analysis.contributors)
            mark = "변경" if (new_title != old_title or new_top != old_top) else "동일"
            if mark == "변경":
                changed += 1

            print(f"  [{row['id']}] {stamp}  {mark}")
            print(f"      전: {old_title}")
            print(f"          1위 {old_top}")
            if mark == "변경":
                print(f"      후: {new_title}")
                print(f"          1위 {new_top}  (자원 {analysis.resource})")

    print(f"\n{total}건 중 {changed}건이 바뀐다.")
    print("[OK] rescore_incidents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

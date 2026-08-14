"""이미 저장된 사건에 자동 라벨을 매긴다.

실시간 경로(`fusion.close_incident`)는 앞으로 닫히는 사건만 판정한다. 이미 쌓인 것들은
누가 채워 주지 않으면 영영 빈칸이고, **자동 라벨을 넣은 이유가 바로 그 밀린 것들이다.**

**판정 로직을 여기 복사하지 않는다.** `decide.autolabel.apply` 를 그대로 부른다 —
규칙이 두 곳에 있으면 조용히 갈리고, 그때 "제품이 매긴 라벨"과 "백필이 매긴 라벨"이
같은 칸에 섞여 구분할 방법이 없어진다.

사람이 답한 사건은 건드리지 않는다(`apply` 가 막는다).

사용:
    python tools/autolabel_backfill.py            # 미리보기만 (기본)
    python tools/autolabel_backfill.py --apply    # 실제로 저장
    python tools/autolabel_backfill.py --apply --notified-only
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config.loader import load_settings  # noqa: E402
from argus.decide import autolabel  # noqa: E402
from argus.storage.hot import Database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 저장한다 (기본은 미리보기)")
    parser.add_argument("--days", type=float, default=0.0, help="이 기간 안의 사건만 (0=전부)")
    args = parser.parse_args()

    cfg = load_settings()
    db = Database()
    db.open()
    # **발송된 알림만이다.** `apply` 가 같은 조건으로 거르므로 여기서 넓게 잡으면
    # 미리보기와 실제 저장이 갈린다 — 미리보기의 목적이 정확히 그것을 막는 것이다.
    where = ["user_label IS NULL", "notified = 1"]
    params: list[float] = []
    if args.days > 0:
        where.append("ts_start > ?")
        params.append(time.time() - args.days * 86400)

    rows = db.query(
        "SELECT id, ts_start, bottleneck, contributors, title, user_label, notified"
        f" FROM incidents WHERE {' AND '.join(where)} ORDER BY ts_start",
        tuple(params),
    )
    foreground = autolabel.foreground_map(db)

    counts: dict[str, int] = {}
    for row in rows:
        if autolabel.during_injection(db, int(row["id"])):
            verdict = autolabel.Verdict(None, "결함 주입 구간이다")
        else:
            verdict = autolabel.judge(row, foreground=foreground, settings=cfg.autolabel)
        key = verdict.label or "(판정 없음)"
        counts[key] = counts.get(key, 0) + 1
        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(row["ts_start"])))
        print(f"{row['id']:>4} {stamp} {key:<12} {row['title'][:42]:<44} {verdict.reason}")
        if args.apply:
            autolabel.apply(db, int(row["id"]), cfg.autolabel)

    print()
    print("합계: " + " · ".join(f"{k} {v}건" for k, v in sorted(counts.items())))
    if not args.apply:
        print("미리보기다. 저장하려면 --apply 를 붙인다.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

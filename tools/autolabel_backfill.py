"""이미 저장된 사건에 자동 라벨을 매긴다.

실시간 경로(`fusion.close_incident`)는 앞으로 닫히는 사건만 판정한다. 이미 쌓인 것들은
누가 채워 주지 않으면 영영 빈칸이고, **자동 라벨을 넣은 이유가 바로 그 밀린 것들이다.**

**판정 로직을 여기 복사하지 않는다.** `decide.autolabel` 을 그대로 부른다 — 규칙이
두 곳에 있으면 조용히 갈리고, 그때 "제품이 매긴 라벨"과 "백필이 매긴 라벨"이 같은 칸에
섞여 구분할 방법이 없어진다.

**그리고 미리보기와 저장이 같은 함수를 지난다**(`evaluate` ↔ `apply`, 둘 다 `_decide`).
예전에는 미리보기만 `judge` 를 직접 불렀는데, 판정에 필요한 조회가 늘 때마다 이쪽만
뒤처졌다 — 08-15 에 `observer` 가 필수 인자가 되자 미리보기가 `TypeError` 로 죽었고,
이 도구에 테스트가 없어 전체 531개는 그대로 통과했다. 주입 구간 검사도 여기 복제돼
있었다. **미리보기가 "저장했으면 나왔을 답"과 다르면 미리보기를 볼 이유가 없다.**

사람이 답한 사건은 건드리지 않는다(`apply` 가 막는다).

사용:
    python tools/autolabel_backfill.py            # 미리보기만 (기본)
    python tools/autolabel_backfill.py --apply    # 실제로 저장
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제로 저장한다 (기본은 미리보기)")
    parser.add_argument("--days", type=float, default=0.0, help="이 기간 안의 사건만 (0=전부)")
    args = parser.parse_args(argv)

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
        f"SELECT id, ts_start, title FROM incidents WHERE {' AND '.join(where)} ORDER BY ts_start",
        tuple(params),
    )

    counts: dict[str, int] = {}
    for row in rows:
        if args.apply:
            verdict = autolabel.apply(db, int(row["id"]), cfg.autolabel)
        else:
            verdict = autolabel.evaluate(db, int(row["id"]), cfg.autolabel)
        key = verdict.label or "(판정 없음)"
        counts[key] = counts.get(key, 0) + 1
        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(row["ts_start"])))
        print(f"{row['id']:>4} {stamp} {key:<12} {row['title'][:42]:<44} {verdict.reason}")

    print()
    print("합계: " + " · ".join(f"{k} {v}건" for k, v in sorted(counts.items())))
    if not args.apply:
        print("미리보기다. 저장하려면 --apply 를 붙인다.")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

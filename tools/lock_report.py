"""DB 락 진단 결과를 읽는다 — 누가 락을 쥐고, 누가 막혔나.

`storage.lock_trace` 를 켜면 상주가 5분마다 보유자 집계를 로그에 남기고, 오래 기다린
사건·오래 쥔 사건을 그때그때 WARNING 으로 남긴다. 이 도구는 그 JSON Lines 를 읽어
사람이 보는 표로 편다. 상주 메모리에 직접 붙지 않으므로 **읽기 전용이고 언제 돌려도
안전하다.**

**기록이 0건이면 결과가 아니라 [FAIL] 이다.** 2026-08-17 에 도구가 죽어 있는 것을
"발화 0건"으로 읽어 "안전하다"가 결론이 될 뻔했다. 여기서는 0건을 "진단이 꺼져 있거나
상주가 아직 재시작되지 않았다"로 **명시적으로** 말하고 종료 코드 1 을 준다.

사용:
    .venv\\Scripts\\python.exe tools\\lock_report.py
    .venv\\Scripts\\python.exe tools\\lock_report.py --since 60      # 최근 60분만
    .venv\\Scripts\\python.exe tools\\lock_report.py --watch         # 10초마다 갱신
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from argus.paths import logs_dir  # noqa: E402

MSG_REPORT = "DB 락 보유자"
MSG_SLOW_WAIT = "DB 락을 오래 기다렸다"
MSG_SLOW_HELD = "DB 락을 오래 쥐었다"


def log_files(directory: pathlib.Path) -> list[pathlib.Path]:
    """회전본까지. 오래된 것부터 읽어 시간 순서가 유지되게 한다."""
    base = directory / "argus.jsonl"
    rotated = sorted(directory.glob("argus.jsonl.*"), reverse=True)
    return [p for p in [*rotated, base] if p.exists()]


def read_records(directory: pathlib.Path, since_ts: float) -> list[dict]:
    out: list[dict] = []
    for path in log_files(directory):
        try:
            # 회전 중이거나 마지막 줄이 잘려 있을 수 있다. 한 줄이 깨져도 나머지는 읽는다.
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if MSG_REPORT not in line and MSG_SLOW_WAIT not in line and MSG_SLOW_HELD not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("msg") not in (MSG_REPORT, MSG_SLOW_WAIT, MSG_SLOW_HELD):
                continue
            if float(record.get("ts", 0)) < since_ts:
                continue
            out.append(record)
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def render(records: list[dict], *, slow_limit: int) -> int:
    reports = [r for r in records if r["msg"] == MSG_REPORT]
    waits = [r for r in records if r["msg"] == MSG_SLOW_WAIT]
    helds = [r for r in records if r["msg"] == MSG_SLOW_HELD]

    if not records:
        print("[FAIL] 락 진단 기록이 0건이다. 이것은 '경합이 없다'는 뜻이 아니다.")
        print("       확인할 것: storage.lock_trace 가 true 인가 · 그 뒤 상주를 재시작했는가")
        print(f"       로그: {logs_dir() / 'argus.jsonl'}")
        return 1

    if reports:
        latest = reports[-1]
        print(f"■ 보유자 집계 — {_clock(latest['ts'])} 기준 (관측 {latest.get('elapsed_s')}초)")
        acquires = latest.get("acquires", 0)
        contended = latest.get("contended", 0)
        ratio = (contended / acquires * 100) if acquires else 0.0
        print(f"  획득 {acquires:,}회 · 그중 오래 기다린 것 {contended:,}회 ({ratio:.1f}%)")
        print()
        print(
            f"  {'보유자':<44}{'횟수':>8}{'총 보유':>11}{'최대':>10}{'p95':>9}{'최대 대기':>11}"
        )
        print(f"  {'-' * 91}")
        for row in latest.get("top", []):
            print(
                f"  {row['holder']:<44}{row['count']:>8,}"
                f"{row['total_ms']:>10,.0f}ms{row['max_ms']:>8,.0f}ms"
                f"{row['p95_ms']:>7,.0f}ms{row['wait_max_ms']:>9,.0f}ms"
            )
    else:
        print("■ 보유자 집계 — 아직 없다 (첫 주기 보고 전이다)")

    print()
    print(f"■ 오래 쥔 사건 {len(helds)}건 — 여기 이름이 곧 가해자다")
    for record in helds[-slow_limit:]:
        print(f"  {_clock(record['ts'])}  {record.get('holder')}  {record.get('held_ms')}ms")
    if not helds:
        print("  없음")

    print()
    print(f"■ 오래 기다린 사건 {len(waits)}건 — 막은 쪽을 지목한다")
    for record in waits[-slow_limit:]:
        blocked = record.get("blocked_by") or record.get("last_holder") or "?"
        print(
            f"  {_clock(record['ts'])}  {record.get('waiter')} 가 "
            f"{record.get('wait_ms')}ms 대기 ← {blocked}"
        )
    if not waits:
        print("  없음")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="DB 락 진단 결과 보기")
    parser.add_argument("--since", type=float, default=0, help="최근 N분만 (0=전부)")
    parser.add_argument("--slow-limit", type=int, default=20, help="느린 사건 표시 개수")
    parser.add_argument("--watch", action="store_true", help="10초마다 갱신")
    parser.add_argument("--log-dir", type=pathlib.Path, default=None)
    args = parser.parse_args()

    directory = args.log_dir or logs_dir()
    while True:
        since_ts = time.time() - args.since * 60 if args.since else 0.0
        records = read_records(directory, since_ts)
        if args.watch:
            print("\033[2J\033[H", end="")
        code = render(records, slow_limit=args.slow_limit)
        if not args.watch:
            return code
        time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())

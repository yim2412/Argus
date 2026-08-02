"""평가용 스냅샷 — **같은 구간을 두 번 잴 수 있게 한다.**

왜 필요한가: 리플레이 평가의 재생 구간이 실행마다 줄어든다. 보존 정리가 앞부분을 계속
지우기 때문이다. 2026-08-02 에 세 번 돌렸더니 1401.6 → 1371.6 → 1346.6분이었고, 그래서
`rules` 의 오탐이 22 → 21 → 20 으로 준 것이 **고친 효과인지 구간이 짧아진 효과인지 가를
수 없었다.** 문턱을 튜닝하면서 "오탐이 몇 건 줄었다"고 말하려면 입력이 고정돼야 한다.

담는 것은 **결함 주입 구간과 그 주변**뿐이다. 채점이 보는 것이 거기고, 전체를 뜨면
스냅샷이 원본만큼 커진다. 여유(`fault_guard_s`)를 붙이는 이유는 보존 정리와 같다 —
비교 창(주입 150초 전부터)과 선행성 조회(±300초)가 구간 밖으로 나간다.

**`net_connections` 는 담지 않는다.** 채점이 쓰지 않고, 네트워크 목적지는 규칙 5 가 말하는
민감 정보다. 스냅샷은 파일로 남아 옮겨 다니기 쉬우므로 애초에 넣지 않는다.

사용:
    python tools/eval_snapshot.py make                    # 채점 가능한 라벨 전부
    python tools/eval_snapshot.py make --ids 53,54,55
    python tools/eval_snapshot.py make --out 2026-08-02-handle
    python tools/eval_snapshot.py list

만든 뒤:
    python -m argus.eval --db <경로>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config.loader import load_settings  # noqa: E402
from argus.paths import data_dir, db_path  # noqa: E402

# 스냅샷에 담는 테이블과, 구간으로 자를 때 쓰는 시각 컬럼.
# `None` 은 통째로 복사한다는 뜻이다 — 라벨·사건은 양이 적고 전부 있어야 채점이 선다.
SLICED = {
    "metrics_raw": "ts",
    "gpu_metrics": "ts",
    "process_metrics": "ts",
    "process_events": "ts",
    "anomaly_signals": "ts",
    "system_events": "ts",
}
WHOLE = ("fault_injections", "incidents", "incident_signals", "meta")


def snapshot_dir() -> Path:
    return data_dir() / "eval_snapshots"


def _windows(conn: sqlite3.Connection, ids: list[int] | None, guard: float) -> list[tuple[float, float]]:
    """담을 구간. 라벨 하나당 (시작 - 여유, 끝 + 여유)."""
    sql = "SELECT id, ts_start, ts_end FROM fault_injections"
    params: tuple = ()
    if ids:
        sql += f" WHERE id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)

    windows = []
    for _id, start, end in conn.execute(sql, params):
        if end is None:
            # 닫히지 않은 라벨은 담지 않는다. 끝을 모르면 어디까지 잘라야 할지도 모르고,
            # 채점에서도 "주입이 완료되지 않음"으로 빠진다.
            continue
        windows.append((float(start) - guard, float(end) + guard))
    return sorted(windows)


def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not windows:
        return []
    out = [windows[0]]
    for lo, hi in windows[1:]:
        last_lo, last_hi = out[-1]
        if lo <= last_hi:
            out[-1] = (last_lo, max(last_hi, hi))
        else:
            out.append((lo, hi))
    return out


def _quiet_windows(
    conn: sqlite3.Connection, injected: list[tuple[float, float]], hours: float
) -> list[tuple[float, float]]:
    """주입이 없는 구간에서 최근 `hours` 시간을 모은다.

    **주입 구간만 담으면 오탐률이 부풀려진다.** 정밀도와 오탐/시간은 "평소에 얼마나
    조용한가"를 재는 지표인데, 평소가 스냅샷에 없으면 잴 것이 없다. 2026-08-02 실측:
    같은 탐지기가 전체 DB 에서 정밀도 80.0%(FP 2)였는데 주입 구간만 담은 스냅샷에서는
    **100.0%(FP 0)** 로 나왔고 오탐/시간은 아예 계산되지 않았다.

    오탐을 줄이는 것이 목적인 작업(규칙 2 정리 등)에서는 이 수치가 판정의 근거이므로,
    조용한 시간을 함께 담아야 스냅샷이 쓸모가 있다.
    """
    if hours <= 0:
        return []
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM metrics_raw").fetchone()
    if not row or row[0] is None:
        return []
    lo_all, hi_all = float(row[0]), float(row[1])

    # 주입 구간을 뺀 빈틈들. 최근 것부터 채운다 — 오래된 구간은 보존 정리에 이미
    # 잘려 있을 수 있어 실제로 담기는 양이 요청보다 적어진다.
    gaps: list[tuple[float, float]] = []
    cursor = lo_all
    for lo, hi in injected:
        if lo > cursor:
            gaps.append((cursor, min(lo, hi_all)))
        cursor = max(cursor, hi)
    if cursor < hi_all:
        gaps.append((cursor, hi_all))

    wanted = hours * 3600.0
    picked: list[tuple[float, float]] = []
    for lo, hi in reversed(gaps):
        if wanted <= 0:
            break
        span = hi - lo
        if span <= 0:
            continue
        take = min(span, wanted)
        picked.append((hi - take, hi))  # 빈틈의 뒤쪽(최근)부터
        wanted -= take
    return sorted(picked)


def make(args: argparse.Namespace) -> int:
    source = db_path()
    if not source.exists():
        print(f"[FAIL] 원본 DB 가 없다: {source}")
        return 1

    guard = float(load_settings().retention.fault_guard_s)
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    injected = _merge(_windows(src, ids, guard))
    quiet = _quiet_windows(src, injected, args.quiet_hours)
    windows = _merge(sorted(injected + quiet))
    if not injected:
        print("[FAIL] 담을 주입 구간이 없다 — 먼저 tools/fault_injector.py 로 주입할 것")
        src.close()
        return 1

    name = args.out or datetime.now().strftime("%Y-%m-%d-%H%M")
    out_dir = snapshot_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.db"
    if target.exists() and not args.force:
        print(f"[FAIL] 이미 있다: {target}  (--force 로 덮어쓰기)")
        src.close()
        return 1

    # **스키마는 원본에서 그대로 가져온다.** 손으로 적으면 마이그레이션 때마다 갈린다.
    #
    # **파일 복사가 아니라 `backup()` 을 쓴다.** DB 가 WAL 모드라 최근 데이터는 아직
    # `-wal` 에 있고, `shutil.copy2` 는 `.db` 만 가져간다 — 상주 인스턴스가 돌고 있으면
    # 마지막 체크포인트 이후가 통째로 빠진다(테스트에서 4KB 짜리 빈 스냅샷이 나왔다).
    # `backup()` 은 WAL 을 포함해 일관된 사본을 만들고, 원본을 잠그지도 않는다.
    temp = target.with_suffix(".tmp")
    temp.unlink(missing_ok=True)

    started = time.perf_counter()
    dst = sqlite3.connect(str(temp))
    src.backup(dst)
    try:
        # 복사본에서 **구간 밖을 지운다.** 반대로(빈 DB 에 넣기) 하면 인덱스·트리거·
        # 스키마 버전을 전부 따라 만들어야 하고, 그 목록이 원본과 어긋나면 조용히 깨진다.
        keep = " OR ".join(["(ts >= ? AND ts <= ?)"] * len(windows))
        flat: list[float] = [x for w in windows for x in w]
        for table in SLICED:
            try:
                dst.execute(f"DELETE FROM {table} WHERE NOT ({keep})", flat)
            except sqlite3.OperationalError:
                continue  # 그 테이블이 없는 옛 스키마

        # 채점이 쓰지 않고 민감한 것은 통째로 비운다(규칙 5).
        for table in ("net_connections", "net_activity_5m", "self_telemetry"):
            try:
                dst.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                continue

        dst.commit()
        dst.execute("VACUUM")
        dst.commit()
    finally:
        dst.close()
        src.close()

    temp.replace(target)
    elapsed = time.perf_counter() - started

    check = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    rows = {}
    for table in list(SLICED) + list(WHOLE):
        try:
            rows[table] = check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    check.close()

    span = sum(hi - lo for lo, hi in windows)
    quiet_span = sum(hi - lo for lo, hi in quiet)
    print(f"  스냅샷   : {target}")
    print(f"  구간     : {len(windows)}개 · 합계 {span / 60:.1f}분 (여유 ±{guard:.0f}초 포함)")
    print(
        f"  그중 조용: {quiet_span / 60:.1f}분 — 오탐률은 이 구간에서 나온다. "
        f"0 이면 정밀도가 부풀려진다"
    )
    print(f"  크기     : {target.stat().st_size:,} bytes   ({elapsed:.1f}초 걸림)")
    for table, count in rows.items():
        if count:
            print(f"    {table:20} {count:>10,}행")
    print()
    print(f"  쓰는 법  : python -m argus.eval --db \"{target}\"")
    print("[OK] eval_snapshot")
    return 0


def list_snapshots(_args: argparse.Namespace) -> int:
    out_dir = snapshot_dir()
    files = sorted(out_dir.glob("*.db")) if out_dir.exists() else []
    if not files:
        print(f"  스냅샷이 없다: {out_dir}")
        print("  만들기: python tools/eval_snapshot.py make")
        return 0
    for path in files:
        stat = path.stat()
        when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  {path.name:28} {stat.st_size:>12,} bytes   {when}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python tools/eval_snapshot.py",
        description="평가용 스냅샷 — 주입 구간을 고정해 같은 입력으로 반복 채점한다.",
    )
    sub = parser.add_subparsers(dest="cmd")

    make_p = sub.add_parser("make", help="스냅샷을 만든다")
    make_p.add_argument("--ids", help="담을 주입 라벨 id (쉼표 구분). 기본: 닫힌 것 전부")
    make_p.add_argument("--out", help="파일 이름(확장자 제외). 기본: 날짜-시각")
    make_p.add_argument(
        "--quiet-hours",
        type=float,
        default=6.0,
        help="주입이 없는 시간을 이만큼 함께 담는다(기본 6). **오탐률은 여기서 나온다** — "
             "0 으로 두면 정밀도가 실제보다 좋게 나온다(실측 80%% → 100%%).",
    )
    make_p.add_argument("--force", action="store_true", help="같은 이름이 있으면 덮어쓴다")
    make_p.set_defaults(func=make)

    list_p = sub.add_parser("list", help="만들어 둔 스냅샷 목록")
    list_p.set_defaults(func=list_snapshots)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

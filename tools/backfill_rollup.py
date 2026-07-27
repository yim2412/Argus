"""롤업 소급 집계 — 워터마크보다 과거에 남아 있는 원본을 접는다.

**왜 필요한가** — 롤업은 워터마크 **이후**만 접는다(`_RollupBase._pending_range`).
새 롤업을 도입하면서 워터마크가 "지금"으로 세워지면, 그 시점 이전의 원본은 접힐 기회를
영영 얻지 못한 채 보존 기한이 되어 삭제된다. 보존 정리는 "롤업 워터마크를 넘지 못한다"는
보호 장치를 갖고 있지만, 워터마크가 이미 앞서 있으므로 그 장치가 통과 상태다.

2026-07-28 에 실제로 그랬다. `process_5m` 이 07-28 00:00 부터만 있고 그 앞
16.5시간(원본 `process_metrics` 는 07-27 07:33 부터 살아 있었다)이 비어 있었다.
`net_activity_5m` 은 같은 기간이 연속으로 접혀 있어 대조가 됐다. 원본의 24시간 보존
기한까지 3시간 남은 상태에서 발견했다.

**이 도구는 일회성이 아니다.** 같은 일은 롤업을 새로 추가할 때마다 생길 수 있고,
Argus 가 오래 꺼져 있다 켜져도(밀린 구간이 `max_buckets_per_run` 을 넘으면 한 틱에
다 못 접는다) 같은 형태로 밀린다. 그때 다시 쓴다.

**안전장치** — 상주 인스턴스가 돌고 있으면 시작하지 않는다. 같은 DB 에 롤업 두 개가
동시에 돌면 워터마크가 서로 덮인다. `InstanceLock` 을 그대로 써서 판정한다.

    python tools\\backfill_rollup.py                # 진단만 (기본)
    python tools\\backfill_rollup.py --apply        # 실제로 접는다
    python tools\\backfill_rollup.py --apply --which process
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config.loader import load_settings  # noqa: E402
from argus.logging_setup import setup  # noqa: E402
from argus.runtime.singleton import AlreadyRunning, InstanceLock  # noqa: E402
from argus.storage.hot import Database  # noqa: E402
from argus.storage.rollup import NetworkRollup, ProcessRollup, Rollup, bucket_of  # noqa: E402

# (인자 이름, 롤업 클래스, 원본 테이블, 원본 ts 컬럼, 집계 테이블, 집계 ts 컬럼)
TARGETS = {
    "metrics": (Rollup, "metrics_raw", "ts", "metrics_1m", "ts_min"),
    "process": (ProcessRollup, "process_metrics", "ts", "process_5m", "ts_5m"),
    "network": (NetworkRollup, "net_connections", "ts", "net_activity_5m", "ts_5m"),
}


def _stamp(ts: float | None) -> str:
    return f"{datetime.fromtimestamp(ts):%m-%d %H:%M}" if ts else "-"


def _make(cls, db: Database, settings) -> object:
    # ProcessRollup 만 top_n 을 받는다. 상주 인스턴스와 같은 값을 써야 결과가 이어진다.
    if cls is ProcessRollup:
        return cls(db, settings.rollup, top_n=settings.rollup.process_top_n)
    return cls(db, settings.rollup)


def survey(db: Database, settings, which: list[str], now: float) -> dict[str, dict]:
    """접히지 않은 채 남아 있는 구간을 잰다.

    `now` 를 호출자가 넘기는 이유: 전후 비교가 같은 기준선을 써야 한다. 각자
    `time.time()` 을 부르면 그 사이에 분 경계를 넘었을 때 **진행 중이라 원래 접지 않는
    마지막 버킷**이 누락으로 잡혀, 성공한 백필이 실패로 보고된다.
    """
    report = {}
    for key in which:
        cls, src, src_ts, dst, dst_ts = TARGETS[key]
        rollup = _make(cls, db, settings)
        wm = rollup.watermark()

        row = db.query(f"SELECT MIN({src_ts}) AS lo, MAX({src_ts}) AS hi FROM {src}")[0]
        src_lo, src_hi = row["lo"], row["hi"]
        row = db.query(f"SELECT MIN({dst_ts}) AS lo, MAX({dst_ts}) AS hi, COUNT(*) AS n FROM {dst}")[0]
        dst_lo, dst_hi, dst_n = row["lo"], row["hi"], row["n"]

        # **행 수가 아니라 버킷으로 센다.** 워터마크 이전 원본을 그냥 세면 정상적으로
        # 접힌 것까지 포함되어(원본은 접은 뒤에도 보존 기한까지 남는다) 항상 거대한
        # 숫자가 나온다 — 누락과 구분되지 않는다. 원본에는 있는데 집계에 없는 버킷만
        # 세야 "접힐 기회를 잃은 구간"이 나온다.
        size = rollup.bucket_s
        cutoff = bucket_of(now - settings.rollup.lag_s, size)  # 진행 중 버킷 제외
        missing = db.query(
            f"SELECT COUNT(*) AS c FROM ("
            f"  SELECT DISTINCT CAST({src_ts} / {size} AS INT) * {size} AS b FROM {src}"
            f"  WHERE {src_ts} < ?"
            f") WHERE b NOT IN (SELECT DISTINCT {dst_ts} FROM {dst})",
            (cutoff,),
        )[0]["c"]

        report[key] = {
            "rollup": rollup,
            "watermark": wm,
            "src": (src, src_lo, src_hi),
            "dst": (dst, dst_lo, dst_hi, dst_n),
            "missing": missing,
            "bucket_s": size,
        }
    return report


def backfill(rollup, db: Database, src: str, src_ts: str) -> tuple[int, int]:
    """원본 최소 시각까지 워터마크를 되돌리고 끝까지 접는다.

    저장이 `INSERT OR REPLACE` 라 이미 접힌 버킷과 겹쳐도 결과가 같다(멱등).
    한 번의 `run_once` 는 `max_buckets_per_run` 만큼만 처리하므로 반복해야 한다.
    """
    lo = db.query(f"SELECT MIN({src_ts}) AS lo FROM {src}")[0]["lo"]
    if lo is None:
        return 0, 0

    rollup._set_watermark(float(lo))  # noqa: SLF001 - 백필이 이 도구의 목적이다

    now = time.time()
    rounds = written = 0
    while rollup._pending_range(now) is not None:  # noqa: SLF001
        written += rollup.run_once(now)
        rounds += 1
        if rounds > 10000:  # 워터마크가 전진하지 않는 버그를 무한루프로 만들지 않는다
            raise RuntimeError("백필이 끝나지 않는다 — 워터마크가 전진하지 않는다")
    return rounds, written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="실제로 접는다 (없으면 진단만)")
    p.add_argument(
        "--which", choices=[*TARGETS, "all"], default="all", help="대상 롤업"
    )
    args = p.parse_args()

    setup(level="INFO", console=False)
    which = list(TARGETS) if args.which == "all" else [args.which]
    settings = load_settings()

    lock = None
    if args.apply:
        try:
            lock = InstanceLock().acquire()
        except AlreadyRunning as e:
            print(f"[FAIL] Argus 가 실행 중입니다 — {e.detail}")
            print('  먼저 멈추세요: schtasks /end /tn "Argus"')
            return 1

    try:
        t0 = time.time()
        with Database() as db:
            before = survey(db, settings, which, t0)

            print(f"  기준 시각 : {_stamp(t0)}\n")
            for key, r in before.items():
                src, s_lo, s_hi = r["src"]
                dst, d_lo, d_hi, d_n = r["dst"]
                print(f"  [{key}]")
                print(f"    원본 {src:16} {_stamp(s_lo)} ~ {_stamp(s_hi)}")
                print(f"    집계 {dst:16} {_stamp(d_lo)} ~ {_stamp(d_hi)}  {d_n}행")
                print(f"    워터마크  {_stamp(r['watermark'])}")
                verdict = (
                    f"누락 버킷 {r['missing']}개"
                    f" (≈{r['missing'] * r['bucket_s'] / 3600:.1f}시간)"
                    if r["missing"]
                    else "누락 없음"
                )
                print(f"    판정      {verdict}\n")

            if not args.apply:
                stale = [k for k, r in before.items() if r["missing"]]
                if stale:
                    print(f"  --apply 로 소급 집계할 수 있습니다: {', '.join(stale)}")
                else:
                    print("  소급할 것이 없습니다.")
                return 0

            for key in which:
                r = before[key]
                if not r["missing"]:
                    print(f"  [{key}] 건너뜀 — 누락 없음")
                    continue
                _, src, src_ts, dst, dst_ts = TARGETS[key]
                started = time.perf_counter()
                rounds, written = backfill(r["rollup"], db, src, src_ts)
                elapsed = time.perf_counter() - started
                n = db.query(f"SELECT COUNT(*) AS c FROM {dst}")[0]["c"]
                buckets = db.query(f"SELECT COUNT(DISTINCT {dst_ts}) AS c FROM {dst}")[0]["c"]
                print(
                    f"  [{key}] {rounds}회 실행, {written}행 기록 ({elapsed:.1f}초)"
                    f" → {dst} {r['dst'][3]}행 → {n}행, 버킷 {buckets}개"
                )

            print()
            after = survey(db, settings, which, t0)
            ok = True
            for key, r in after.items():
                d_lo, d_hi, d_n = r["dst"][1], r["dst"][2], r["dst"][3]
                print(f"  [{key}] 집계 {_stamp(d_lo)} ~ {_stamp(d_hi)}  {d_n}행")
                if r["missing"]:
                    print(f"    [FAIL] 아직 {r['missing']}개 버킷이 비어 있다")
                    ok = False
            print("\n[OK] 소급 집계 완료" if ok else "\n[FAIL] 남은 구간이 있다")
            return 0 if ok else 1
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

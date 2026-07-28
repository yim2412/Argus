"""장기 조회 — 핫(SQLite)과 웜(Parquet)을 하나로 보이게 한다.

**왜 필요한가.** 롤업은 두 곳에 나뉘어 산다. 최근 이틀은 `metrics_1m`·`process_5m`
(SQLite)에 있고, 그보다 오래된 날짜는 Parquet 으로 내보낸 뒤 **SQLite 에서 지워진다**
(`warm.export_date` 의 `purge_after_export`). 그래서 SQLite 만 읽는 코드는 아무리
오래 돌려도 **이틀치밖에 못 본다.**

2026-07-29 에 이것이 실제로 드러났다. `tools/readiness.py` 가 "3일 이상 관측된 프로세스"
를 `process_5m` 에서만 세고 있어 영원히 0종이었고, 대시보드 타임라인은 07-28 이 통째로
빈 채 그려지고 있었다. 데이터는 멀쩡히 Parquet 에 있었다 — 세는 쪽이 절반만 보고 있었다.

**장기 데이터를 보는 코드는 전부 이 모듈을 거친다.** 소비자마다 두 계층을 각자 합치면
합치는 규칙(아래)이 제각각이 되고, 같은 버그를 소비자 수만큼 다시 만든다.

**합치는 규칙: 날짜 단위로 웜이 정본이다.** 한 날짜가 양쪽에 다 있으면 웜만 쓴다.
`tools/backfill_rollup.py` 로 소급 집계하면 이미 내보낸 날짜가 SQLite 에 되살아나는데
(07-27 이 그랬다), 그대로 더하면 그날이 두 배로 세어져 고부하 판정과 지문 통계가
조용히 틀어진다. 웜을 정본으로 삼는 이유는 내보낼 때 행 수까지 검증된 불변 파일이기
때문이다.

**웜 날짜에 걸칠 때만 DuckDB 를 켠다.** 대시보드 기본 조회는 24시간이라 대부분 핫만으로
끝난다. 매번 DuckDB 커넥션을 여는 것은 관측자를 무겁게 만든다(설계 규칙 1).
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..paths import db_path
from . import warm
from .rollup import COLUMNS as ROLLUP_COLUMNS

# 한 버킷이 덮는 시간(초). 관측 시간을 재는 데 쓴다.
BUCKET_S = {"metrics": 60, "process": 300}

_TABLE = {"metrics": ("metrics_1m", "ts_min"), "process": ("process_5m", "ts_5m")}


@dataclass(frozen=True)
class DayCoverage:
    """하루가 실제로 얼마나 관측됐는지.

    **날짜 수가 아니라 이것을 세야 한다.** 2시간 켜 둔 날과 12시간 켜 둔 날이 똑같이
    "1일"로 세어지면, 착수 조건이 채워졌다는 판정이 거짓말이 된다.
    """

    day: str  # YYYY-MM-DD
    buckets: int
    source: str  # "warm" | "hot"
    bucket_s: int = 60

    @property
    def hours(self) -> float:
        return self.buckets * self.bucket_s / 3600.0


def _hot() -> sqlite3.Connection | None:
    """핫 저장소 읽기 전용 연결. 없으면 None — 웜만으로도 답할 수 있다."""
    path = db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _day_bounds(day: str) -> tuple[float, float]:
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def coverage(kind: str = "metrics") -> dict[str, DayCoverage]:
    """관측된 날짜별 커버리지. 웜 + 핫, 날짜 단위로 웜 우선."""
    bucket_s = BUCKET_S[kind]
    out: dict[str, DayCoverage] = {}

    warm_days = set(warm.partition_days(kind))
    if warm_days:
        view = "warm" if kind == "metrics" else "warm_process"
        ts = "ts_min" if kind == "metrics" else "ts_5m"
        rows = warm.query(
            f"SELECT strftime(to_timestamp({ts}), '%Y-%m-%d'), COUNT(DISTINCT {ts}) "
            f"FROM {view} GROUP BY 1"
        )
        for day, buckets in rows:
            out[day] = DayCoverage(day, int(buckets), "warm", bucket_s)

    conn = _hot()
    if conn is not None:
        table, ts = _TABLE[kind]
        try:
            rows = conn.execute(
                f"SELECT strftime('%Y-%m-%d', {ts}, 'unixepoch', 'localtime') AS d, "
                f"COUNT(DISTINCT {ts}) AS n FROM {table} GROUP BY 1"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        for row in rows:
            if row["d"] in warm_days:
                continue  # 웜이 정본. 백필로 되살아난 잔여물일 수 있다.
            out[row["d"]] = DayCoverage(row["d"], int(row["n"]), "hot", bucket_s)

    return dict(sorted(out.items()))


def observed_days(kind: str = "metrics", min_hours: float = 0.0) -> list[str]:
    """관측된 날짜. `min_hours` 미만만 켜져 있던 날은 빼고 센다."""
    return [d for d, c in coverage(kind).items() if c.hours >= min_hours]


def process_day_index() -> dict[str, dict[str, int]]:
    """프로세스명 → {날짜: 버킷 수}.

    버킷 수까지 돌려주는 이유: "며칠 보였나"만으로는 지문을 세울 수 있는지 알 수 없다.
    3일에 걸쳐 보였어도 매번 5분씩이면 p99 를 세울 표본이 못 된다.
    """
    out: dict[str, dict[str, int]] = {}
    warm_days = set(warm.partition_days("process"))

    if warm_days:
        rows = warm.query(
            "SELECT name, strftime(to_timestamp(ts_5m), '%Y-%m-%d'), COUNT(DISTINCT ts_5m) "
            "FROM warm_process GROUP BY 1, 2"
        )
        for name, day, buckets in rows:
            out.setdefault(name, {})[day] = int(buckets)

    conn = _hot()
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT name, strftime('%Y-%m-%d', ts_5m, 'unixepoch', 'localtime') AS d, "
                "COUNT(DISTINCT ts_5m) AS n FROM process_5m GROUP BY 1, 2"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        for row in rows:
            if row["d"] in warm_days:
                continue
            out.setdefault(row["name"], {})[row["d"]] = int(row["n"])

    return out


def rollup_range(lo_ts: float, hi_ts: float | None = None) -> list[dict[str, Any]]:
    """`[lo_ts, hi_ts)` 구간의 1분 롤업을 시간순으로. 웜 + 핫."""
    hi_ts = hi_ts if hi_ts is not None else time.time()
    rows: list[dict[str, Any]] = []

    # 구간이 웜 날짜에 걸칠 때만 DuckDB 를 켠다.
    warm_days = [d for d in warm.partition_days("metrics") if _overlaps(d, lo_ts, hi_ts)]
    if warm_days:
        keys = ", ".join(f"'{d}'" for d in warm_days)  # 파일명에서 온 ISO 날짜뿐이다
        cols = ", ".join(ROLLUP_COLUMNS)
        rows += [
            dict(zip(ROLLUP_COLUMNS, r))
            for r in warm.query(
                f"SELECT {cols} FROM warm WHERE ts_min >= ? AND ts_min < ? "
                f"AND strftime(to_timestamp(ts_min), '%Y-%m-%d') IN ({keys})",
                [lo_ts, hi_ts],
            )
        ]

    conn = _hot()
    if conn is not None:
        try:
            hot_rows = conn.execute(
                "SELECT * FROM metrics_1m WHERE ts_min >= ? AND ts_min < ? ORDER BY ts_min",
                (lo_ts, hi_ts),
            ).fetchall()
        except sqlite3.OperationalError:
            hot_rows = []
        finally:
            conn.close()
        covered = set(warm_days)
        rows += [
            dict(row)
            for row in hot_rows
            if time.strftime("%Y-%m-%d", time.localtime(row["ts_min"])) not in covered
        ]

    rows.sort(key=lambda r: r["ts_min"])
    return rows


def _overlaps(day: str, lo_ts: float, hi_ts: float) -> bool:
    start, end = _day_bounds(day)
    return start < hi_ts and end > lo_ts


def span(kind: str = "metrics") -> tuple[float, float, int] | None:
    """전체 보유 구간 `(가장 이른 ts, 가장 늦은 ts, 버킷 수)`. 없으면 None.

    버킷 수는 중복 제거 뒤의 값이다 — 웜과 핫에 같은 날짜가 있어도 한 번만 센다.
    """
    cov = coverage(kind)
    if not cov:
        return None

    days = sorted(cov)
    lo, _ = _day_bounds(days[0])
    _, hi = _day_bounds(days[-1])
    # 날짜 경계가 아니라 실제 데이터의 양 끝을 쓴다 — 화면이 "보유 시간"을 이걸로 잰다.
    lo = max(lo, _edge(kind, days[0], cov[days[0]].source, "MIN"))
    hi = min(hi, _edge(kind, days[-1], cov[days[-1]].source, "MAX"))
    return lo, hi, sum(c.buckets for c in cov.values())


def _edge(kind: str, day: str, source: str, agg: str) -> float:
    """한 날짜 안에서 실제 데이터의 처음/끝 타임스탬프."""
    start, end = _day_bounds(day)
    ts = "ts_min" if kind == "metrics" else "ts_5m"
    if source == "warm":
        view = "warm" if kind == "metrics" else "warm_process"
        rows = warm.query(
            f"SELECT {agg}({ts}) FROM {view} WHERE {ts} >= ? AND {ts} < ?", [start, end]
        )
        return float(rows[0][0]) if rows and rows[0][0] is not None else start

    conn = _hot()
    if conn is None:
        return start
    table = _TABLE[kind][0]
    try:
        row = conn.execute(
            f"SELECT {agg}({ts}) FROM {table} WHERE {ts} >= ? AND {ts} < ?", (start, end)
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else start
    except sqlite3.OperationalError:
        return start
    finally:
        conn.close()


if __name__ == "__main__":  # 스모크: python -m argus.storage.history
    ok = True
    for kind in ("metrics", "process"):
        cov = coverage(kind)
        print(f"  {kind:<8} 관측 {len(cov)}일")
        for c in cov.values():
            print(f"    {c.day}  {c.buckets:>5}버킷 = {c.hours:>5.1f}시간  ({c.source})")
        if not cov:
            print("    (아직 롤업이 없다 — 상주 인스턴스가 도는지 확인할 것)")

    index = process_day_index()
    multi = [n for n, d in index.items() if len(d) >= 3]
    print(f"  프로세스     전체 {len(index)}종, 3일 이상 관측 {len(multi)}종")

    rows = rollup_range(time.time() - 86400 * 7)
    print(f"  최근 7일 롤업: {len(rows)}행")
    if rows:
        lo = datetime.fromtimestamp(rows[0]["ts_min"])
        hi = datetime.fromtimestamp(rows[-1]["ts_min"])
        print(f"    {lo:%m-%d %H:%M} ~ {hi:%m-%d %H:%M}")
        # 합치기 규칙이 깨지면 여기서 잡힌다 — 같은 분이 두 번 나오면 중복 계상이다.
        stamps = [r["ts_min"] for r in rows]
        if len(stamps) != len(set(stamps)):
            print("  [FAIL] 같은 분이 중복으로 들어 있다 — 웜/핫 병합 규칙이 깨졌다")
            ok = False

    print("[OK] storage.history" if ok else "[FAIL] storage.history")
    raise SystemExit(0 if ok else 1)

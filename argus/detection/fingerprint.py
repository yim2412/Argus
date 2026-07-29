"""프로세스 지문 (Phase 6-B) — "이 프로그램의 평소는 어디까지인가".

**6-A(procleak)의 오탐을 줄이는 것이 목적이다.** 6-A 는 자기 과거만 보므로 "평소에도
핸들을 4천 개씩 쓰는 프로그램"과 "평소 200개인데 4천 개가 된 프로그램"을 구분하지
못한다. 실측에서 medal(게임 녹화)이 핸들 383 → 1,395 로 늘어 발화했는데 medal 의 평소
`handles_max` p99 는 **12,466** 이었다 — 완전히 정상 범위였다.

**억제는 한쪽으로만 작동한다.** 지문이 있고 도달값이 평소 p99 이내일 때만 발화를 막는다.
지문이 없으면(신규·희귀 프로세스) 아무것도 하지 않는다 — 6-A 에서 상한 문제로 주입
프로세스를 통째로 놓친 적이 있어, 모르는 것을 막는 방향으로는 절대 틀지 않는다.

**단위에 주의.** 지문은 "5분 버킷 통계량의 분포"지 "순간값의 분포"가 아니다. 원본
`process_metrics` 가 24시간만 남아 버킷 통계를 다시 집계할 수밖에 없는데, 버킷별 p99 를
모아 다시 p99 를 내면 그건 분위수가 아니다 — **예외도 안 나고 조용히 그럴듯해 보인다.**
무엇을 집계했는지 `stat` 에 남기는 이유다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..logging_setup import get_logger
from ..storage import history

log = get_logger(__name__)

# procleak 이 보는 지표 → `process_5m` 에서 대응하는 버킷 통계량.
# 누수는 "얼마나 높이 갔나"의 문제라 최대·상위 분위수 쪽을 쓴다.
STAT_FOR = {"handles": "handles_max", "rss_mb": "rss_p95"}

# 지문이 되기 위한 최소 조건. 3일에 걸쳐 보였어도 매번 5분씩이면 p99 를 세울 표본이 못 된다.
DEFAULT_MIN_DAYS = 3
DEFAULT_MIN_BUCKETS = 100
# 하루로 세는 최소 관측 시간. 2시간 켜 둔 날과 12시간 켜 둔 날은 같은 하루가 아니다.
MIN_DAY_HOURS = 6.0

COLUMNS = ("name", "regime", "stat", "p50", "p95", "p99", "maximum", "samples", "days", "built_at")
DEFAULT_REGIME = "all"


@dataclass(frozen=True)
class Fingerprint:
    name: str
    stat: str
    p50: float
    p95: float
    p99: float
    maximum: float
    samples: int
    days: int
    regime: str = DEFAULT_REGIME

    def within_normal(self, value: float) -> bool:
        """이 값이 이 프로그램의 평소 범위 안인가."""
        return value <= self.p99


def quantile(values: list[float], p: float) -> float:
    """정렬 후 **최근접 순위**(nearest-rank): `ceil(p × n)` 번째 값.

    `statistics.quantiles` 는 표본이 적으면 보간으로 **관측된 적 없는 값**을 만들어 낸다.
    여기서 기준이 되는 것은 "이 프로그램이 실제로 도달한 수준"이라 그게 맞지 않는다.

    `round(p × (n-1))` 로 짜면 1~100 의 p50 이 51 이 된다. 큰 차이는 아니지만 지문은
    조용히 틀리는 쪽이라, 표준 정의에서 벗어난 값이 섞이면 나중에 근거를 못 댄다.
    """
    ordered = sorted(values)
    if not ordered:
        raise ValueError("표본이 비어 있다")
    import math

    rank = math.ceil(p * len(ordered))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


def _series(stat: str) -> dict[str, list[float]]:
    """프로세스명 → 버킷 통계량 목록. **핫+웜을 합쳐 읽는다.**

    롤업은 이틀이 지나면 Parquet 으로 옮겨가고 SQLite 에서 지워지므로, 핫만 읽으면
    아무리 오래 돌려도 이틀치밖에 못 본다. 병합 규칙(날짜 단위로 웜이 정본)은
    `history` 가 한 곳에서 갖고 있다.
    """
    if stat not in {"handles_max", "rss_p95", "cpu_p95", "cpu_p99", "rss_max", "threads_max"}:
        raise ValueError(f"허용되지 않은 통계량: {stat}")  # 문자열이 SQL 에 그대로 들어간다

    rows = history._by_day(  # noqa: SLF001 — 병합 규칙을 재구현하지 않으려고 그대로 쓴다
        "process",
        f"SELECT strftime(to_timestamp(ts_5m), '%Y-%m-%d'), name, {stat} "
        f"FROM warm_process WHERE {stat} IS NOT NULL",
        f"SELECT strftime('%Y-%m-%d', ts_5m, 'unixepoch', 'localtime'), name, {stat} "
        f"FROM process_5m WHERE {stat} IS NOT NULL",
    )
    out: dict[str, list[float]] = {}
    for _day, name, value in rows:
        out.setdefault(name, []).append(float(value))
    return out


def build(
    *,
    min_days: int = DEFAULT_MIN_DAYS,
    min_buckets: int = DEFAULT_MIN_BUCKETS,
    stats: tuple[str, ...] = tuple(STAT_FOR.values()),
) -> list[Fingerprint]:
    """지문을 만든다. 자격 미달 프로그램은 아예 넣지 않는다 — 없는 것과 같아야 한다."""
    index = history.process_day_index()
    out: list[Fingerprint] = []

    for stat in stats:
        series = _series(stat)
        for name, values in series.items():
            by_day = index.get(name, {})
            days = len(by_day)
            if days < min_days or len(values) < min_buckets:
                continue
            out.append(
                Fingerprint(
                    name=name,
                    stat=stat,
                    p50=quantile(values, 0.50),
                    p95=quantile(values, 0.95),
                    p99=quantile(values, 0.99),
                    maximum=max(values),
                    samples=len(values),
                    days=days,
                )
            )
    return out


def save(db, fingerprints: list[Fingerprint]) -> int:
    now = time.time()
    rows = [
        (f.name, f.regime, f.stat, f.p50, f.p95, f.p99, f.maximum, f.samples, f.days, now)
        for f in fingerprints
    ]
    if rows:
        db.insert_many("process_fingerprint", COLUMNS, rows, replace=True)
    return len(rows)


def load(db, stat: str | None = None) -> dict[tuple[str, str], Fingerprint]:
    """`(이름, stat)` → 지문. 조회 실패는 빈 결과다 — 지문이 없어도 탐지는 돌아야 한다."""
    sql = "SELECT * FROM process_fingerprint"
    params: tuple = ()
    if stat:
        sql += " WHERE stat = ?"
        params = (stat,)
    try:
        rows = db.query(sql, params)
    except Exception as exc:
        log.warning("지문 조회 실패 — 억제 없이 진행한다", extra={"error": str(exc)})
        return {}

    out = {}
    for r in rows:
        fp = Fingerprint(
            name=r["name"], stat=r["stat"], p50=r["p50"], p95=r["p95"], p99=r["p99"],
            maximum=r["maximum"], samples=r["samples"], days=r["days"], regime=r["regime"],
        )
        out[(fp.name, fp.stat)] = fp
    return out


class FingerprintBuilder:
    """지문을 주기적으로 다시 만든다.

    **자주 할 일이 아니다.** 3일 이상 관측된 것만 지문이 되므로 한 시간 사이에 결과가
    달라질 일이 없고, 웜(Parquet)까지 훑는 작업이라 싸지도 않다. 기본은 6시간이다.

    빌드 실패가 상주 프로세스를 죽이면 안 된다 — 지문이 없으면 억제가 없을 뿐이고
    탐지는 그대로 돈다.
    """

    name = "fingerprint"

    def __init__(self, db, *, interval_s: float = 21600.0, min_days: int = DEFAULT_MIN_DAYS,
                 min_buckets: int = DEFAULT_MIN_BUCKETS) -> None:
        self.db = db
        self.interval_s = interval_s
        self.min_days = min_days
        self.min_buckets = min_buckets
        self._built = 0

    def setup(self) -> None:
        pass

    def tick(self) -> None:
        try:
            prints = build(min_days=self.min_days, min_buckets=self.min_buckets)
            self._built = save(self.db, prints)
            log.info("지문 갱신", extra={"count": self._built})
        except Exception as exc:
            log.warning("지문 갱신 실패 — 다음 주기에 다시 시도한다", extra={"error": str(exc)})

    def teardown(self) -> None:
        log.info("지문 빌더 종료", extra={"last_built": self._built})


if __name__ == "__main__":  # 스모크: python -m argus.detection.fingerprint
    from ..storage.hot import Database

    prints = build()
    print(f"  지문 {len(prints)}건 (자격: {DEFAULT_MIN_DAYS}일↑ · {DEFAULT_MIN_BUCKETS}버킷↑)")

    by_stat: dict[str, list[Fingerprint]] = {}
    for fp in prints:
        by_stat.setdefault(fp.stat, []).append(fp)
    for stat, items in sorted(by_stat.items()):
        print(f"    {stat}: {len(items)}종")
        for fp in sorted(items, key=lambda f: -f.p99)[:3]:
            print(f"      {fp.name:<24} p50={fp.p50:>9.0f} p95={fp.p95:>9.0f} "
                  f"p99={fp.p99:>9.0f}  버킷 {fp.samples}, {fp.days}일")

    with Database() as db:
        saved = save(db, prints)
        loaded = load(db)
        print(f"  저장 {saved}건 → 조회 {len(loaded)}건")

    problems = []
    if not prints:
        problems.append("지문이 하나도 만들어지지 않았다 — 롤업이 도는지 확인할 것")
    if saved != len(prints) or len(loaded) < saved:
        problems.append(f"저장/조회 개수가 어긋난다: 만듦 {len(prints)} 저장 {saved} 조회 {len(loaded)}")
    # 분위수는 조용히 틀린다. 순서가 어긋나면 계산이 잘못된 것이다.
    broken = [f for f in prints if not (f.p50 <= f.p95 <= f.p99 <= f.maximum)]
    if broken:
        problems.append(f"분위수 순서가 어긋난 지문 {len(broken)}건 (예: {broken[0].name}/{broken[0].stat})")
    thin = [f for f in prints if f.samples < DEFAULT_MIN_BUCKETS or f.days < DEFAULT_MIN_DAYS]
    if thin:
        problems.append(f"자격 미달인데 지문이 만들어졌다: {len(thin)}건")

    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.fingerprint")

"""기여도 분해 — 누가 그 자원을 가져갔나.

**프로세스 하나가 아니라 프로그램을 본다.** 한 프로그램은 여러 프로세스다. Chrome 은
탭마다 프로세스를 만들고, 빌드는 컴파일러를 코어 수만큼 띄운다. PID 단위로 보면
각각 3% 라 게임 하나(4.3%)보다 낮게 나오고, 순위가 뒤집힌다. 실측에서 정확히 그랬다 —
CPU 스핀 주입의 자식 8개가 각 4.2% 였고 1위는 엉뚱하게 게임이었다. 합치면 33.6% 다.

묶는 기준은 **프로세스 트리가 아니라 이름**이다. 트리로 묶으면 셸이나 탐색기가
관측 창 안에서 시작된 경우 그 아래 전부가 한 덩어리가 되어 구분이 사라진다.
이름은 그런 붕괴가 없고, 사용자가 듣고 싶은 답의 단위이기도 하다("크롬이 68%").
트리는 버리지 않고 **근거**로 남긴다 — 채점의 정답 집합을 만들고, 리포트에서
"프로세스 30개"를 말할 때 쓴다.

**Shapley 를 쓰지 않는다.** 계획서는 "Shapley 근사"를 지정하지만, Shapley 가 값을
하는 것은 참여자의 기여가 *상호작용할 때*다. CPU 시간·디스크 바이트·메모리는
**가산적**이다 — A 가 쓴 30% 와 B 가 쓴 20% 를 더하면 50% 이고, 둘이 함께여야만
생기는 시너지가 없다. 그런 자원에서 Shapley 값은 단순 델타 비율과 수학적으로 같고
계산 비용만 늘어난다. 상호작용이 실제로 관측되면(캐시 경합, 큐 포화의 비선형 지연)
그때 도입한다.

**선행성을 함께 잰다.** 같은 시각에 같이 오른 둘 중 먼저 오른 쪽이 원인에 가깝다.
상관은 인과가 아니지만, 시간 순서는 인과의 필요조건이다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..config.loader import IncidentSettings
from ..logging_setup import get_logger

log = get_logger(__name__)

# 자원 이름 → process_metrics 컬럼
RESOURCE_COLUMNS = {
    "cpu": "cpu_percent",
    "rss": "rss_mb",
    "io_read": "io_read_bps",
    "io_write": "io_write_bps",
    "handles": "handles",
}

# **저량 자원 — 보유량이지 소비량이 아니다.**
#
# 나머지(cpu·io_read·io_write)는 유량이다. 초당 얼마를 쓰는가라서, 구간 중에 새로 뜬
# 프로세스가 CPU 30% 를 먹으면 그 30% 전부가 진짜로 새로 생긴 부하다 — "전 구간에는
# 0 이었으니 증가분은 30" 이 맞는 계산이다.
#
# 저량은 다르다. 프로세스가 뜰 때 들고 오는 핸들 400개는 **늘어난 것이 아니라 그
# 프로그램의 기본 보유량**이다. 그런데 전/후 창을 비교하는 방식은 그것을 전부 증가분으로
# 센다. 2026-07-29 채점에서 `compattelrunner` 13개가 구간 중에 뜨면서 각자 400 핸들을
# 들고 온 것이 5,200 증가로 계산됐고, 혼자 +8,313 을 낸 실제 원인을 40% 대 23% 로
# 밀어내 1위를 빼앗았다. 그래서 저량은 전/후 비교가 아니라 **각자 자기 시계열 안에서
# 얼마나 자랐는가**로 잰다(`_window_growth`). 새로 뜬 프로세스는 자기 첫 관측값이
# 기준이 되므로 기본 보유량이 자동으로 빠지고, 그러면서도 **진짜로 뜨면서 8GB 를
# 먹어치우는 프로세스는 여전히 잡힌다** — 그런 프로세스는 자기 시계열 안에서도 자란다.
STOCK_RESOURCES = frozenset({"rss", "handles"})

# 저량 시계열에서 값이 최대치의 이 비율 아래로 떨어지면 시계열을 끊는다. 정상적인
# 해제이거나 PID 가 재사용되어 다른 프로그램의 값이 이어 붙은 것이다. `procleak` 의
# `drop_reset_ratio` 와 같은 판단이고, 사건 구간은 수십 분일 수 있어 더 자주 걸린다.
# → config: `incident.stock_drop_reset_ratio`

# **이름으로 합치면 안 되는 프로세스들.**
#
# Windows 의 공유 호스트 프로세스는 이름이 같아도 서로 다른 프로그램이다. `svchost.exe`
# 한 대에 서로 무관한 서비스가 하나씩 들어 있고, 이 PC 에는 91개가 떠 있다. 이름으로
# 합치면 "svchost 가 41%"라는 답이 나오는데, 그건 틀린 답이자 **쓸모없는 답**이다 —
# 사용자가 어떤 서비스인지 알 방법이 없다. 실측에서 이 합산이 주입 프로세스를 제치고
# 1위를 차지했다.
#
# 이건 임계값이 아니라 플랫폼 사실이라 코드에 둔다. 목록이 늘면 config 로 옮긴다.
SHARED_HOSTS = frozenset(
    {
        "svchost",
        "dllhost",
        "rundll32",
        "taskhostw",
        "wmiprvse",
        "runtimebroker",
        "backgroundtaskhost",
        "conhost",
        "sihost",
    }
)


def group_key(name: str, pid: int) -> str:
    """묶음 키. 공유 호스트는 PID 를 분리해 개별 프로그램으로 다룬다."""
    base = (name or "").lower().removesuffix(".exe")
    if base in SHARED_HOSTS:
        return f"{name}#{pid}"
    return name


@dataclass
class Contributor:
    """한 후보(프로그램)의 기여. 같은 이름의 프로세스를 모두 합친 것."""

    name: str
    pids: set[int] = field(default_factory=set)
    before: float = 0.0
    after: float = 0.0
    share: float = 0.0
    lead_s: float | None = None
    is_new: bool = False
    """이상 구간에 처음 나타난 프로그램인가. 새로 뜬 것은 강한 용의자다.

    **관측 여부로 정한다 — 사용량이 0 이었는지가 아니다.** 예전에는 `before == 0.0`
    으로 유도했는데 그건 두 가지를 뭉갠다. 유량에서는 비교 창 내내 CPU 를 안 쓰고
    가만히 있던 프로세스가 "새로 시작됨"으로 표시됐고, 저량에서는 `before` 가 구간
    초반값이 되면서 진짜 새 프로세스조차 영영 False 가 된다.
    """

    @property
    def main_pid(self) -> int | None:
        return min(self.pids) if self.pids else None

    @property
    def delta(self) -> float:
        return self.after - self.before

    def label(self) -> str:
        if len(self.pids) > 1:
            return f"{self.name} (프로세스 {len(self.pids)}개)"
        return f"{self.name} (PID {self.main_pid})"


# ---------------------------------------------------------------- 프로세스 트리


def process_trees(db, ts_start: float, ts_end: float, lookback_s: float = 3600.0) -> dict[int, set[int]]:
    """부모 PID → 자식 PID 집합. 트리 질의의 원재료다.

    구간보다 앞선 이벤트까지 보는 이유(`lookback_s`)는 부모가 구간 전에 떴을 수 있어서다.
    PID 는 재사용되므로 같은 PID 의 나중 `start` 가 앞선 관계를 덮는다.
    """
    rows = db.query(
        "SELECT ts, pid, ppid FROM process_events "
        "WHERE event = 'start' AND ts >= ? AND ts <= ? ORDER BY ts",
        (ts_start - lookback_s, ts_end),
    )
    children: dict[int, set[int]] = {}
    for row in rows:
        if row["pid"] is None or row["ppid"] is None:
            continue
        children.setdefault(int(row["ppid"]), set()).add(int(row["pid"]))
    return children


def descendants(db, root_pid: int, ts_start: float, ts_end: float) -> set[int]:
    """한 PID 아래 전부.

    채점의 정답 집합이 여기서 나온다. 결함 주입기는 부모 하나만 기록하는데 실제
    부하는 자식들이 낸다(CPU 스핀은 GIL 때문에 프로세스로 fork 한다). 부모만
    정답으로 두면 어떤 귀인 엔진도 통과할 수 없다 — 부모는 CPU 를 쓰지 않는다.
    """
    children = process_trees(db, ts_start, ts_end)
    out = {root_pid}
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


# ---------------------------------------------------------------- 기여도


def _window_usage(
    db, resource: str, ts_start: float, ts_end: float, *, robust: bool = False
) -> dict[int, tuple[str, float]]:
    """구간 내 PID 별 (이름, 사용량).

    합이 아니라 대푯값인 이유: 구간 길이와 표본 수가 창마다 다르다. 합을 쓰면 긴 창이
    무조건 이긴다.

    `robust=True` 면 평균 대신 **중앙값**을 쓴다. "평소"를 정하는 창에 필요하다 —
    평균은 그 창 안의 우연한 스파이크 하나에 끌려간다. 실측에서 이것 때문에 순위가
    뒤집혔다: 비교 창에서 우연히 조용했던 chrome 의 델타가 과장돼 주입 프로세스를
    제쳤다. 베이스라인이 평균 대신 중앙값을 쓰는 이유와 같은 이유다.
    """
    column = RESOURCE_COLUMNS[resource]
    if not robust:
        rows = db.query(
            f"SELECT pid, name, AVG({column}) AS value FROM process_metrics "
            "WHERE ts >= ? AND ts < ? AND pid IS NOT NULL GROUP BY pid, name",
            (ts_start, ts_end),
        )
        return {int(r["pid"]): (r["name"], float(r["value"] or 0.0)) for r in rows}

    rows = db.query(
        f"SELECT pid, name, {column} AS value FROM process_metrics "
        "WHERE ts >= ? AND ts < ? AND pid IS NOT NULL AND value IS NOT NULL",
        (ts_start, ts_end),
    )
    grouped: dict[int, tuple[str, list[float]]] = {}
    for row in rows:
        pid = int(row["pid"])
        entry = grouped.setdefault(pid, (row["name"], []))
        entry[1].append(float(row["value"]))
    return {
        pid: (name, statistics.median(values) if values else 0.0)
        for pid, (name, values) in grouped.items()
    }


def _window_growth(
    db, resource: str, ts_start: float, ts_end: float, drop_reset_ratio: float
) -> dict[tuple[int, str], tuple[float, float]]:
    """저량 자원에서 `(pid, 이름)` 별 (구간 초반값, 구간 후반값).

    전/후 두 창을 비교하는 대신 **한 구간 안의 자기 시계열**을 본다. 그래서 구간
    중에 새로 뜬 프로세스도 자기 첫 관측값이 기준이 되고, 기본 보유량이 증가분으로
    둔갑하지 않는다.

    양 끝점 하나씩만 보지 않고 앞/뒤 1/4 의 중앙값을 쓰는 것은 `procleak.judge` 와
    같은 이유다 — 끝점 하나가 잡음이면 판정이 통째로 뒤집힌다.

    키에 이름을 넣는 이유: PID 는 재사용된다. `(pid, name)` 으로 가르면 죽은 프로세스와
    그 PID 를 물려받은 다른 프로그램이 한 시계열로 이어 붙는 일이 없다.
    """
    column = RESOURCE_COLUMNS[resource]
    rows = db.query(
        f"SELECT ts, pid, name, {column} AS value FROM process_metrics "
        "WHERE ts >= ? AND ts < ? AND pid IS NOT NULL AND name IS NOT NULL "
        f"AND {column} IS NOT NULL ORDER BY ts",
        (ts_start, ts_end),
    )

    series: dict[tuple[int, str], list[float]] = {}
    for row in rows:
        key = (int(row["pid"]), row["name"])
        values = series.setdefault(key, [])
        value = float(row["value"])
        # 크게 떨어졌다 = 자원을 해제했다. 이어 붙이면 골짜기를 사이에 둔 두 봉우리가
        # 하나의 긴 상승으로 보여, 이미 반납한 양까지 증가분으로 세게 된다.
        if values:
            peak = max(values)
            if peak > 0 and value < peak * drop_reset_ratio:
                values.clear()
        values.append(value)

    out: dict[tuple[int, str], tuple[float, float]] = {}
    for key, values in series.items():
        quarter = max(1, len(values) // 4)
        out[key] = (
            statistics.median(values[:quarter]),
            statistics.median(values[-quarter:]),
        )
    return out


def _observed_pids(db, ts_start: float, ts_end: float) -> set[int]:
    """그 창에 한 번이라도 관측된 PID. `is_new` 판정의 근거다."""
    rows = db.query(
        "SELECT DISTINCT pid FROM process_metrics WHERE ts >= ? AND ts < ? AND pid IS NOT NULL",
        (ts_start, ts_end),
    )
    return {int(r["pid"]) for r in rows}


def attribute(
    db,
    resource: str,
    *,
    before: tuple[float, float],
    after: tuple[float, float],
    limit: int = 8,
    settings: IncidentSettings | None = None,
) -> list[Contributor]:
    """변화점 전후를 비교해 기여도를 매긴다.

    돌려주는 것은 **증가한** 후보들뿐이다. 줄어든 프로세스는 원인이 아니다.

    유량(cpu·io)과 저량(rss·handles)은 계산이 다르다 — `STOCK_RESOURCES` 주석 참조.
    """
    if resource not in RESOURCE_COLUMNS:
        raise ValueError(f"모르는 자원: {resource}")

    cfg = settings or IncidentSettings()

    groups: dict[str, Contributor] = {}

    def group_for(pid: int, name: str) -> Contributor:
        key = group_key(name, pid)
        contributor = groups.get(key)
        if contributor is None:
            contributor = Contributor(name=name)
            groups[key] = contributor
        contributor.pids.add(pid)
        return contributor

    if resource in STOCK_RESOURCES:
        # **창을 비교 구간 시작까지 넓혀 잡는다.** 누수는 증상보다 먼저 시작되므로
        # (사건은 증상이 관측된 시점에 열린다) 이상 구간 안만 보면 미리 오르기 시작한
        # 진짜 원인의 상승분을 놓친다. `lead_time` 이 "40초 선행"을 재고 있다는 것
        # 자체가 원인이 구간 밖에서 오르기 시작한다는 뜻이다.
        for (pid, name), (first, last) in _window_growth(
            db, resource, before[0], after[1], cfg.stock_drop_reset_ratio
        ).items():
            contributor = group_for(pid, name)
            contributor.before += first
            contributor.after += last
    else:
        # "평소"를 정하는 창은 중앙값으로 본다 — 그 창의 우연한 스파이크에 끌려가지 않게.
        usage_before = _window_usage(db, resource, *before, robust=True)
        usage_after = _window_usage(db, resource, *after)
        for pid, (name, value) in usage_before.items():
            group_for(pid, name).before += value
        for pid, (name, value) in usage_after.items():
            group_for(pid, name).after += value

    pids_before = _observed_pids(db, *before)
    for contributor in groups.values():
        contributor.is_new = not (contributor.pids & pids_before)

    risers = [c for c in groups.values() if c.delta > 0]
    total = sum(c.delta for c in risers)
    for contributor in risers:
        contributor.share = contributor.delta / total if total > 0 else 0.0

    risers.sort(key=lambda c: c.delta, reverse=True)
    return risers[:limit]


# ---------------------------------------------------------------- 선행성


def lead_time(
    db,
    contributor: Contributor,
    resource: str,
    onset_ts: float,
    *,
    lookback_s: float = 300.0,
    rise_ratio: float = 0.1,
) -> float | None:
    """이 후보가 시스템 악화보다 몇 초 앞서 **오르기 시작했나**.

    재는 것은 상승의 *개시* 시점이지 완료 시점이 아니다. `rise_ratio` 가 0.1 인 이유가
    이것이다. 처음에 0.5(상승폭의 절반 도달)로 뒀더니 **서서히 오르는 부하에서 원인
    프로세스가 일관되게 '후행'으로 나왔다** — 5분 램프의 절반 지점은 당연히 늦다.

    시스템 쪽 `onset_ts` 는 "평소 범위를 처음 벗어난 시점"이므로, 프로세스도 같은
    성격의 시점(처음 유의하게 오른 때)과 비교해야 공정하다. 기준이 다르면 비교
    자체가 무의미하다.

    양수면 선행(원인에 가깝다), 음수면 후행(결과일 수 있다). 이것으로 인과를
    증명하지는 못한다. 다만 **시간 순서는 인과의 필요조건**이라, 뒤따라 오른
    프로세스를 1순위로 지목하는 실수는 막을 수 있다.
    """
    if not contributor.pids or contributor.delta <= 0:
        return None

    column = RESOURCE_COLUMNS[resource]
    placeholders = ",".join("?" * len(contributor.pids))
    rows = db.query(
        f"SELECT ts, SUM({column}) AS value FROM process_metrics "
        f"WHERE ts >= ? AND ts <= ? AND pid IN ({placeholders}) GROUP BY ts ORDER BY ts",
        (onset_ts - lookback_s, onset_ts + lookback_s, *sorted(contributor.pids)),
    )
    if not rows:
        return None

    target = contributor.before + contributor.delta * rise_ratio

    # 조회 창 첫 표본이 이미 목표를 넘고 있으면 **상승 시점을 특정할 수 없다.**
    # 이때 첫 표본을 답으로 내면 항상 `lookback_s` 에 가까운 값이 나와 "285초 선행"
    # 같은 가짜 선행이 만들어진다(실제 뜻은 "원래 그 수준이었다"이다). 모르는 것은
    # 모른다고 해야 한다 — 근거 없는 선행성은 엉뚱한 프로세스를 1순위로 만든다.
    if (rows[0]["value"] or 0.0) >= target:
        return None

    for row in rows:
        if (row["value"] or 0.0) >= target:
            return round(onset_ts - float(row["ts"]), 1)
    return None

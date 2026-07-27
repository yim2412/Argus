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

    @property
    def main_pid(self) -> int | None:
        return min(self.pids) if self.pids else None

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def is_new(self) -> bool:
        """이상 구간에 처음 나타난 프로세스인가. 새로 뜬 프로그램은 강한 용의자다."""
        return self.before == 0.0 and self.after > 0.0

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


def attribute(
    db,
    resource: str,
    *,
    before: tuple[float, float],
    after: tuple[float, float],
    limit: int = 8,
) -> list[Contributor]:
    """변화점 전후를 비교해 기여도를 매긴다.

    돌려주는 것은 **증가한** 후보들뿐이다. 줄어든 프로세스는 원인이 아니다.
    """
    if resource not in RESOURCE_COLUMNS:
        raise ValueError(f"모르는 자원: {resource}")

    # "평소"를 정하는 창은 중앙값으로 본다 — 그 창의 우연한 스파이크에 끌려가지 않게.
    usage_before = _window_usage(db, resource, *before, robust=True)
    usage_after = _window_usage(db, resource, *after)

    groups: dict[str, Contributor] = {}

    def group_for(pid: int, name: str) -> Contributor:
        key = group_key(name, pid)
        contributor = groups.get(key)
        if contributor is None:
            contributor = Contributor(name=name)
            groups[key] = contributor
        contributor.pids.add(pid)
        return contributor

    for pid, (name, value) in usage_before.items():
        group_for(pid, name).before += value
    for pid, (name, value) in usage_after.items():
        group_for(pid, name).after += value

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

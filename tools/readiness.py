"""착수 조건 판정 — "지금 시작해도 되는가"에 데이터가 답하게 한다.

`PLAN.md` 의 남은 작업 중 여럿이 "데이터가 며칠 쌓인 뒤"를 전제한다. 그런데 **날짜를
세는 것은 답이 아니다.** 하루 종일 게임만 한 3일과 유휴 위주의 3일은 같은 3일이 아니고,
사건 발생률도 사용 패턴에 따라 몇 배씩 흔들린다. 달력이 아니라 표본이 기준이어야 한다.

`python tools/readiness.py` — 각 작업의 착수 조건을 실제 DB 에서 세어 [OK]/[대기] 로
찍는다. 부족하면 무엇이 얼마나 부족한지 말한다.

원본(1초)은 24시간 창이라 며칠을 기다려도 늘지 않는다. 그래서 판정은 전부 **영구 보존되는
계층**(`metrics_1m`·`process_5m`·`incidents`) 만 본다.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 판정 기준
#
# 여기 숫자는 탐지 임계값이 아니라 **작업 착수 기준**이라 config 로 빼지 않는다.
# 사용자가 튜닝할 대상이 아니고, 근거가 바뀌면 숫자가 아니라 이 주석이 바뀌어야 한다.

# ② 알람 품질: 정해야 하는 것이 "warning 인데 점수 낮은 것"의 하한선이라, 판단 재료는
# 전체 사건 수가 아니라 warning 이상 표본 수다. 4건으로 선을 그으면 그 4건에 과적합된다.
MIN_ACTIONABLE_INCIDENTS = 10

# 하루만 보면 그날의 사용 패턴을 전체로 착각한다. 실측 첫날 사건 10건은 거의 전부 게임
# 세션에서 나왔다 — 유휴 위주의 날이 섞여야 "게임 아닌 날의 점수 분포"를 알 수 있다.
MIN_DISTINCT_DAY_KINDS = 2

# 하루 중 GPU 가 이만큼 돌아간 시간이 이 이상이면 '고부하 날'로 본다. 게임·렌더링을
# 구분하려는 것이 아니라 **부하 있는 날과 없는 날을 가르는 것**이 목적이라 느슨하게 잡는다.
BUSY_GPU_UTIL = 50.0
BUSY_MINUTES = 30

# Phase 6 프로세스 지문: p95/p99 를 세우려면 한 프로세스가 여러 날에 걸쳐 관측돼야 한다.
# 하루치로 만든 지문은 그날 하루의 습관이다.
FINGERPRINT_MIN_DAYS = 3
FINGERPRINT_MIN_PROCS = 15

# Phase 4-B 레짐: GMM + HMM 이 요일 효과까지 잡으려면 주말과 평일이 모두 들어와야 한다.
REGIME_MIN_DAYS = 7


@dataclass
class Check:
    """조건 하나의 판정 결과."""

    label: str
    ok: bool
    detail: str


@dataclass
class Readiness:
    """한 작업의 착수 준비 상태."""

    name: str
    note: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def _connect() -> sqlite3.Connection:
    path = os.path.join(os.environ.get("APPDATA", ""), "Argus", "argus.db")
    if not os.path.exists(path):
        print(f"[FAIL] DB 를 찾지 못했다: {path}", file=sys.stderr)
        raise SystemExit(1)
    # 상주 인스턴스가 쓰고 있으므로 읽기 전용으로만 연다.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _day(ts: float) -> str:
    return time.strftime("%m-%d", time.localtime(ts))


def _day_kinds(conn: sqlite3.Connection) -> dict[str, str]:
    """관측된 날짜별로 '고부하'인지 '유휴 위주'인지.

    GPU 를 쓰는 이유는 이 PC 에서 부하 있는 날을 가장 잘 가르는 신호이기 때문이다.
    GPU 가 없는 PC 에서는 이 판정이 전부 '유휴'가 되는데, 그때는 사용 패턴 다양성을
    다른 축으로 봐야 한다 — 지금은 그 경우를 만나면 그렇게 말하고 넘어간다.
    """
    busy_minutes: dict[str, int] = defaultdict(int)
    days: set[str] = set()
    for row in conn.execute("SELECT ts_min, gpu_util_mean FROM metrics_1m"):
        day = _day(row["ts_min"])
        days.add(day)
        if (row["gpu_util_mean"] or 0.0) >= BUSY_GPU_UTIL:
            busy_minutes[day] += 1
    return {d: ("고부하" if busy_minutes[d] >= BUSY_MINUTES else "유휴 위주") for d in sorted(days)}


def _has_gpu(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(gpu_util_mean) FROM metrics_1m WHERE gpu_util_mean IS NOT NULL"
    ).fetchone()
    return bool(row and row[0])


def check_alarm_quality(conn: sqlite3.Connection, kinds: dict[str, str]) -> Readiness:
    """② 심각도-점수 게이트."""
    rows = conn.execute(
        "SELECT severity, peak_score FROM incidents WHERE severity IN ('warning','critical')"
    ).fetchall()
    scored = [r["peak_score"] for r in rows if r["peak_score"] is not None]

    distinct = sorted(set(kinds.values()))
    result = Readiness(
        "② 알람 품질 — 심각도·점수 역전",
        "발송 게이트의 점수 하한을 정한다. 판단 재료는 warning 이상 표본이다.",
    )
    result.checks.append(
        Check(
            f"warning 이상 사건 {MIN_ACTIONABLE_INCIDENTS}건 이상",
            len(rows) >= MIN_ACTIONABLE_INCIDENTS,
            f"현재 {len(rows)}건"
            + (f" (점수 {min(scored):.2f}~{max(scored):.2f})" if scored else ""),
        )
    )
    result.checks.append(
        Check(
            "고부하 날과 유휴 위주 날이 모두 포함",
            len(distinct) >= MIN_DISTINCT_DAY_KINDS,
            f"관측 {len(kinds)}일: " + ", ".join(f"{d}({k})" for d, k in kinds.items()),
        )
    )
    return result


def check_fingerprint(conn: sqlite3.Connection) -> Readiness:
    """Phase 6 프로세스 지문."""
    seen: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT ts_5m, name FROM process_5m"):
        seen[row["name"]].add(_day(row["ts_5m"]))
    ready = [n for n, days in seen.items() if len(days) >= FINGERPRINT_MIN_DAYS]

    result = Readiness(
        "Phase 6 — 프로세스 지문",
        f"프로세스명별 p50/p95/p99 를 세운다. {FINGERPRINT_MIN_DAYS}일 이상 관측된 것만 지문이 된다.",
    )
    result.checks.append(
        Check(
            f"{FINGERPRINT_MIN_DAYS}일 이상 관측된 프로세스 {FINGERPRINT_MIN_PROCS}종 이상",
            len(ready) >= FINGERPRINT_MIN_PROCS,
            f"현재 {len(ready)}종 / 전체 {len(seen)}종",
        )
    )
    return result


def check_regime(conn: sqlite3.Connection, kinds: dict[str, str]) -> Readiness:
    """Phase 4-B 레짐 추론."""
    result = Readiness(
        "Phase 4-B — 레짐 추론",
        "GMM + HMM. 요일 효과를 잡으려면 주말과 평일이 모두 들어와야 한다.",
    )
    result.checks.append(
        Check(
            f"롤업 관측 {REGIME_MIN_DAYS}일 이상",
            len(kinds) >= REGIME_MIN_DAYS,
            f"현재 {len(kinds)}일",
        )
    )
    return result


def main() -> int:
    conn = _connect()
    try:
        kinds = _day_kinds(conn)
        if not kinds:
            print("[대기] 롤업 데이터가 아직 없다. 상주 인스턴스가 도는지 먼저 확인할 것.")
            return 0

        if not _has_gpu(conn):
            print("  참고: GPU 지표가 없어 '고부하/유휴' 구분이 성립하지 않는다.")
            print("        사용 패턴 다양성은 사람이 판단할 것.\n")

        reports = [
            check_alarm_quality(conn, kinds),
            check_fingerprint(conn),
            check_regime(conn, kinds),
        ]
    finally:
        conn.close()

    for report in reports:
        mark = "[OK]" if report.ok else "[대기]"
        print(f"{mark} {report.name}")
        print(f"      {report.note}")
        for check in report.checks:
            print(f"      {'v' if check.ok else 'x'} {check.label}")
            print(f"        └ {check.detail}")
        print()

    ready = [r.name for r in reports if r.ok]
    print("착수 가능:", ", ".join(ready) if ready else "없음 — 더 모을 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

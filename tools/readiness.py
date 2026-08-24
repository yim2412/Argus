"""착수 조건 판정 — "지금 시작해도 되는가"에 데이터가 답하게 한다.

`PLAN.md` 의 남은 작업 중 여럿이 "데이터가 며칠 쌓인 뒤"를 전제한다. 그런데 **날짜를
세는 것은 답이 아니다.** 하루 종일 게임만 한 3일과 유휴 위주의 3일은 같은 3일이 아니고,
사건 발생률도 사용 패턴에 따라 몇 배씩 흔들린다. 달력이 아니라 표본이 기준이어야 한다.

    .venv\\Scripts\\python.exe tools\\readiness.py

각 작업의 착수 조건을 실제 데이터에서 세어 [OK]/[대기] 로 찍는다. 부족하면 무엇이 얼마나
부족한지 말한다. **가상환경으로 실행해야 한다** — 웜 스토어를 읽는 데 DuckDB 가 필요하다.

원본(1초)은 24시간 창이라 며칠을 기다려도 늘지 않는다. 그래서 판정은 롤업 계층만 본다.
그 롤업이 **두 곳에 나뉘어 산다는 것**이 2026-07-29 에 문제가 됐다 — 이 도구가
`process_5m`(SQLite)만 세고 있었는데 이틀 지난 날짜는 Parquet 으로 옮겨지며 SQLite 에서
지워진다. 그래서 "3일 이상 관측된 프로세스"가 영원히 0종이었다. 지금은 두 계층을 합쳐
읽는 `argus.storage.history` 를 거친다.

**날짜를 세지 않는다 — 관측 시간을 센다.** 2시간 켜 둔 날과 12시간 켜 둔 날이 똑같이
"1일"이면 판정이 거짓말이 된다. 같은 이유로 프로세스 지문은 "며칠 보였나"가 아니라
"몇 버킷 쌓였나"까지 본다.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.storage import history  # noqa: E402

# ---------------------------------------------------------------- 판정 기준
#
# 여기 숫자는 탐지 임계값이 아니라 **작업 착수 기준**이라 config 로 빼지 않는다.
# 사용자가 튜닝할 대상이 아니고, 근거가 바뀌면 숫자가 아니라 이 주석이 바뀌어야 한다.

# 하루 중 GPU 가 이만큼 돌아간 시간이 이 이상이면 '고부하 날'로 본다. 게임·렌더링을
# 구분하려는 것이 아니라 **부하 있는 날과 없는 날을 가르는 것**이 목적이라 느슨하게 잡는다.
BUSY_GPU_UTIL = 50.0
BUSY_MINUTES = 30

# 이만큼은 관측돼야 '하루'로 센다. Argus 는 PC 가 켜져 있을 때만 도므로 관측 시간은
# 사용 시간이고, 날마다 크게 다르다(실측 3일: 10.4h / 11.3h / 2.1h). 2시간짜리 날을
# 1일로 세면 "3일 관측"이 실질 하루치가 된다.
#
# 6시간인 이유: 하루의 1/4 이고, PC 사용은 시간대별로 성격이 갈리는데(업무 낮 · 게임 밤)
# 이 정도는 돼야 한 구간을 온전히 덮는다. 실측 분포에서 4~10시간 어디로 잡아도 판정이
# 같아 경계가 민감하지 않다 — 표본이 늘어 경계에 걸리는 날이 나오면 그때 다시 본다.
MIN_DAY_HOURS = 6.0

# Phase 6 프로세스 지문: p95/p99 를 세우려면 한 프로세스가 여러 날에 걸쳐 관측돼야 한다.
# 하루치로 만든 지문은 그날 하루의 습관이다.
FINGERPRINT_MIN_DAYS = 3
FINGERPRINT_MIN_PROCS = 15

# 날짜만으로는 부족하다. 3일에 걸쳐 보였어도 매번 5분씩이면 표본이 15개다.
# p99 를 세우려면 최소 100 표본은 있어야 하고, 5분 버킷으로 100개면 8.3시간이다.
FINGERPRINT_MIN_BUCKETS = 100

# 10번 현재 손실 축: "같은 부하에서 클럭이 얼마나 깎였나"는 날짜 간 비교라, 부하가 있던
# 날이 여러 날 있어야 기준선이 선다. 하루치로는 그날의 냉각 상태가 곧 기준선이 된다.
# 3일인 이유는 지문(`FINGERPRINT_MIN_DAYS`)과 같다 — 이상치 하루를 나머지 둘이 가른다.
LOSS_AXIS_MIN_DAYS = 3

# 클럭 컬럼은 2026-08-03 에 롤업에 추가됐다. 그 전 날짜는 값이 NULL 이라 부하가 있었어도
# 비교에 쓸 수 없다 — 그래서 '부하 있는 날'이 아니라 '부하 + 클럭이 있는 날'을 센다.
LOSS_AXIS_COLUMN = "gpu_clock_sm_mean"

# `severity` 등급 역전: **전체 라벨 수가 아니라 등급 역전 축의 라벨 수를 센다.**
#
# 계획서는 "사람 답 20건"을 조건으로 적어 뒀는데, 그 수는 첫 라벨 7건을 보고 "7건은
# 아직 적다"에서 나온 어림수였다(`PLAN.md` 2026-08-14). 2026-08-16 에 그 조건이
# 재려는 대상과 어긋나 있다는 것이 실측으로 드러났다:
#
#     08-14  라벨 7건  = CPU·경합 4(전부 normal) + 발열 3(전부 real)
#     08-16  라벨 11건 = CPU·경합 8(전부 normal) + 발열 3(전부 real)
#
# **늘어난 4건이 전부 CPU 계열이고 등급 역전에 대해 아무것도 말하지 않는다.** 등급
# 역전은 "안 나간 것이 나갔어야 했다"는 실패라 증거가 미탐에만 있고, 이 PC 에서 그
# 축은 발열뿐이다. 전체 라벨이 20건이 되어도 발열이 3건이면 착수 근거는 그대로 3건이다.
#
# 6건인 이유. 사용자 답이 등급과 무관하다면(귀무가설) 한 방향으로 몰릴 확률은 3건에서
# 12.5% 로 우연히도 나오지만 6건이면 1.6% 다 — 그때 비로소 "일관되다"가 우연이 아니게
# 된다. **방향이 갈려도 6건에서 판단이 선다**(갈렸다는 것 자체가 "등급이 뒤집힌 게
# 아니라 사건마다 다르다"는 답이다). 그래서 조건은 방향이 아니라 건수다.
SEVERITY_AXIS_MIN_LABELS = 6

# **조건이 충족될 수 있는지도 같이 잰다.** 부족한 것(`[대기]`)과 더 오지 않는 것(`[막힘]`)은
# 다른 상태인데, 건수만 세면 둘이 똑같이 보인다. 2026-07-29 Phase 6 과 2026-08-19
# severity 가 같은 함정이었다 — 도달 불가능한 조건이 무기한 `[대기]` 로 남아 있었고,
# 그동안 다음 세션은 "곧 찰 것"으로 읽었다.
#
# 7일인 이유. 이 PC 사용은 주 단위로 성격이 갈리므로(주중 업무 · 주말 게임) 한 주를
# 온전히 덮어야 "그 축이 안 나오는 것"과 "이번 주에 마침 안 했다"가 갈린다. 실측:
# THERMAL 사건은 08-10 이 마지막이고 그 뒤 9일간 0건인데, 그 사이 고부하 날이 8일
# 있었다 — 부하가 없어서가 아니라 **문턱에 닿지 못해서**다.
#
# 관측이 멈춘 기간은 세지 않는다. PC 를 안 켠 날까지 "안 왔다"로 세면 여행 다녀온
# 것이 조건 재설계 신호가 된다.
SUPPLY_STALL_DAYS = 7


@dataclass
class Check:
    """조건 하나의 판정 결과."""

    label: str
    ok: bool
    detail: str
    # 이 조건의 표본 공급이 멈췄다면 그 이유. 채워지면 `[대기]` 가 아니라 `[막힘]` 이다 —
    # "더 모으면 된다"와 "모을 곳이 없다"를 눈으로 갈라야 계획이 그 자리에 머물지 않는다.
    stalled: str | None = None


@dataclass
class Readiness:
    """한 작업의 착수 준비 상태."""

    name: str
    note: str
    checks: list[Check] = field(default_factory=list)
    # 이미 끝났거나 기각된 작업. 조건은 계속 보여 주되 "착수 가능"으로는 세지 않는다 —
    # 끝난 것을 할 일로 계속 내밀면 다음 세션이 잘못 이어간다.
    #
    # **기각된 것을 지우지 않고 남기는 이유**는 그 판정이 실측이었기 때문이다. 항목이
    # 사라지면 다음 세션이 계획서 원문을 보고 같은 조건을 다시 세운다 (2026-08-06 의
    # 점수 하한이 정확히 그런 경우다 — 계획서에는 아직 "가치 높음"으로 적혀 있다).
    done: str | None = None
    mark: str = "[완료]"

    @property
    def ok(self) -> bool:
        return self.done is None and all(c.ok for c in self.checks)

    @property
    def stalled(self) -> str | None:
        """못 채운 조건 중 공급이 멈춘 것의 이유. 없으면 `None`.

        **막힘은 완료·기각을 덮지 않는다.** 이미 결론이 난 항목에 "표본이 안 온다"를
        띄우면 끝난 일을 다시 할 일처럼 보이게 한다.
        """
        if self.done is not None or self.ok:
            return None
        return next((c.stalled for c in self.checks if not c.ok and c.stalled), None)


@dataclass
class Day:
    """관측된 하루."""

    kind: str  # "고부하" | "유휴 위주"
    hours: float

    @property
    def counts(self) -> bool:
        """'며칠' 을 셀 때 이 날을 한 날로 칠지."""
        return self.hours >= MIN_DAY_HOURS


def _days() -> dict[str, Day]:
    """관측된 날짜별 성격과 관측 시간.

    고부하/유휴를 GPU 로 가르는 이유는 이 PC 에서 부하 있는 날을 가장 잘 가르는 신호이기
    때문이다. GPU 가 없는 PC 에서는 이 판정이 전부 '유휴'가 되는데, 그때는 사용 패턴
    다양성을 다른 축으로 봐야 한다 — 지금은 그 경우를 만나면 그렇게 말하고 넘어간다.

    세는 일은 저장소가 한다. 롤업 전 구간을 파이썬으로 올리면 데이터가 쌓일수록 느려지고,
    여기서 필요한 것은 날짜별 카운트뿐이다.
    """
    busy = history.busy_minutes("gpu_util_mean", BUSY_GPU_UTIL)
    return {
        day: Day("고부하" if busy.get(day, 0) >= BUSY_MINUTES else "유휴 위주", cov.hours)
        for day, cov in history.coverage("metrics").items()
    }


def _counted(days: dict[str, Day]) -> list[str]:
    return [d for d, v in days.items() if v.counts]


def _day_summary(days: dict[str, Day]) -> str:
    parts = []
    for d, v in days.items():
        mark = "" if v.counts else f", {MIN_DAY_HOURS:g}h 미만"
        parts.append(f"{d[5:]}({v.kind} {v.hours:.1f}h{mark})")
    return ", ".join(parts)


def _has_gpu() -> bool:
    return history.has_column_data("gpu_util_mean")


def closed_alarm_quality() -> Readiness:
    """② 발송 게이트 점수 하한 — 실측으로 기각됐다.

    조건을 고치는 대신 항목을 내린 이유가 둘이다.

    - **판정 자체가 뒤집혔다.** 하한 0.3 을 걸면 걸러지는 것은 GPU 발열 8건이고
      살아남는 것은 op.gg(peak_score 0.910) 다 — 계획서가 "가치 높음"이라 부른 쪽을
      정확히 죽인다. `peak_score` 는 "평소와 얼마나 다른가"지 "얼마나 손해인가"가
      아니라서 심각도의 대리 지표로 쓸 수 없다.
    - **조건이 영영 충족되지 않았다.** "유휴 위주 날이 섞여야 한다"였는데 관측 14일이
      전부 고부하다. 매일 게임하는 사용자에게는 도달 불가능한 조건이고, 그런 조건은
      기다림이 아니라 판정의 결함이다(2026-07-29 Phase 6 과 같은 유형).
    """
    return Readiness(
        "② 알람 품질 — 발송 게이트 점수 하한",
        "등급 역전을 점수 하한으로 막으려 했다.",
        done=(
            "2026-08-06 기각. `peak_score` 는 이례성이지 손해가 아니다 — 하한을 걸면 "
            "op.gg 가 남고 GPU 발열이 걸러진다. 그때 대안으로 지목한 10번 현재 손실 "
            "축도 08-09 에 기각됐다(위) — 남은 길은 사용자 라벨이다."
        ),
        mark="[기각]",
    )


def check_fingerprint(days: dict[str, Day]) -> Readiness:
    """Phase 6 프로세스 지문."""
    index = history.process_day_index()
    # 짧게만 켜 둔 날은 날짜로 치지 않는다 — 날을 세는 규칙은 한 곳이어야 한다.
    counted = set(_counted(days))

    def day_count(per_day: dict[str, int]) -> int:
        return len([d for d in per_day if d in counted])

    ready = [
        name
        for name, per_day in index.items()
        if day_count(per_day) >= FINGERPRINT_MIN_DAYS
        and sum(per_day.values()) >= FINGERPRINT_MIN_BUCKETS
    ]
    # 어느 조건에서 걸리는지 보여야 무엇을 더 기다릴지 안다.
    by_days = [n for n, p in index.items() if day_count(p) >= FINGERPRINT_MIN_DAYS]
    by_buckets = [n for n, p in index.items() if sum(p.values()) >= FINGERPRINT_MIN_BUCKETS]

    result = Readiness(
        "Phase 6 — 프로세스 지문",
        f"프로세스명별 p50/p95/p99 를 세운다. {FINGERPRINT_MIN_DAYS}일 이상 관측되고 "
        f"누적 {FINGERPRINT_MIN_BUCKETS}버킷 이상 쌓인 것만 지문이 된다.",
        done="2026-07-29 완료 (6-A 누수 탐지 + 6-B 지문). 아래는 지문 자격 현황이다.",
    )
    result.checks.append(
        Check(
            f"조건을 만족하는 프로세스 {FINGERPRINT_MIN_PROCS}종 이상",
            len(ready) >= FINGERPRINT_MIN_PROCS,
            f"현재 {len(ready)}종 / 전체 {len(index)}종 "
            f"(날짜 조건만: {len(by_days)}종, 표본 조건만: {len(by_buckets)}종)",
        )
    )
    return result


def closed_regime() -> Readiness:
    """Phase 4-B 레짐 추론 — 구현됐고, 여기 있던 조건은 그 구현의 조건이 아니었다.

    조건이 "롤업 7일"이었던 것은 GMM + HMM 을 전제했기 때문이다. 그 방식은 예광탄
    3회 뒤 기각됐고(근거는 `docs/DONE.md`), 실제로 만든 것은 베이스라인을
    `(program, metric)` 별로 나누는 것이다 — 요일도 주말/평일도 쓰지 않는다.
    **조건이 채워져서 착수한 것이 아니라 조건 자체가 다른 설계의 것이었다.**
    """
    return Readiness(
        "Phase 4-B — 레짐 추론",
        "부하 상태에 따라 '평소'를 나눈다.",
        done=(
            "2026-08-04 완료. 프로그램별 베이스라인으로 구현했다(GMM+HMM 은 기각). "
            "이 PC 에만 켜져 있고 배포 기본값은 아직 꺼짐 — 근거가 한 대뿐이다."
        ),
    )


def check_loss_axis(days: dict[str, Day]) -> Readiness:
    """10번 등급 — 현재 손실 축(GPU 클럭). **2026-08-09 실측으로 기각됐다.**

    조건은 계속 센다 — 기각한 것이 축의 가치가 아니라 **이 PC 에서의 검증 가능성**이라,
    다른 하드웨어 데이터가 들어오면 같은 조건으로 다시 보게 된다.

    `thermal.py` 와 같은 기준으로 부하 날을 가르되, 클럭 컬럼이 실제로 들어 있는 날만
    센다. 컬럼은 2026-08-03 에 추가돼 그 전 날짜는 부하가 있었어도 값이 없다.
    """
    clocked = history.busy_minutes(LOSS_AXIS_COLUMN, 0.0)
    counted = set(_counted(days))
    usable = sorted(
        day
        for day, buckets in clocked.items()
        if buckets and day in counted and days[day].kind == "고부하"
    )

    result = Readiness(
        "10 — 등급의 현재 손실 축",
        "같은 부하에서 클럭이 얼마나 깎였나. 위험 축은 2026-08-03 에 끝났다.",
        done=(
            "2026-08-09 기각. 같은 게임·같은 부하에서 클럭이 6일간 ±1.5% 안에 붙어 "
            "있다 — 날짜별 하락은 전부 '그날 무슨 게임을 했느냐'였고 포어그라운드를 "
            "고정하면 사라진다. 이 카드는 상시 전력 제한이라 `thermal.py` 가 잡는 것 "
            "말고 이 축이 더 잡을 것이 이 PC 데이터에는 없다. 다른 하드웨어가 오면 다시 본다."
        ),
        mark="[기각]",
    )
    result.checks.append(
        Check(
            f"클럭이 기록된 고부하 날 {LOSS_AXIS_MIN_DAYS}일 이상",
            len(usable) >= LOSS_AXIS_MIN_DAYS,
            f"현재 {len(usable)}일"
            + (f": {', '.join(d[5:] for d in usable)}" if usable else " (컬럼은 08-03 추가)"),
        )
    )
    return result


def check_severity_inversion(days: dict[str, Day]) -> Readiness:
    """`severity` 등급 역전 — 발열 축 사람 라벨.

    **여기만 롤업이 아니라 사건을 센다.** 이 조건의 표본은 관측 시간이 아니라 사람이
    답한 라벨이고, 그것은 사건 테이블에만 있다.

    `days` 는 공급 정지를 재는 데만 쓴다 — "마지막 사건 이후 며칠을 관측했는가"를
    달력이 아니라 관측일로 세기 위해서다.

    **축을 여기서 다시 정하지 않는다.** 무엇을 미탐으로 물을지는 이미
    `label.ask_unnotified_bottlenecks` 가 갖고 있고(지금은 `THERMAL`), 등급 역전
    후보 축은 정확히 그것과 같은 개념이다 — 두 곳에 두면 config 를 고쳐도 이 판정만
    옛 축을 세는 상태가 된다.

    주입 구간을 빼는 판정도 `dashboard.data` 의 것을 그대로 쓴다. 답 대기에서 빼는
    기준과 여기서 세는 기준이 갈리면, 물어보지도 않은 사건을 착수 조건에 세게 된다.
    """
    from argus.config.loader import load_settings
    from argus.dashboard.data import _DURING_INJECTION, query

    kinds = [str(k).upper() for k in load_settings().label.ask_unnotified_bottlenecks]
    axis = "/".join(kinds) if kinds else "(없음)"

    result = Readiness(
        "severity 등급 역전",
        f"미탐이 나갔어야 했나. 증거는 {axis} 축의 사람 라벨에만 있다.",
    )
    if not kinds:
        result.checks.append(
            Check(
                "미탐을 물어볼 축이 설정돼 있을 것",
                False,
                "`label.ask_unnotified_bottlenecks` 가 비어 있다 — 답 대기에 미탐이 "
                "올라오지 않으므로 이 조건은 영영 안 찬다",
            )
        )
        return result

    placeholders = ",".join("?" * len(kinds))

    # **알림이 나간 사건은 세지 않는다 (2026-08-25).**
    #
    # 등급 역전은 *안 나간 것이 나갔어야 했다* 는 실패다. 그러니 증거는 미탐에만 있고,
    # 발송된 사건에 붙은 라벨은 이 질문에 **아무 말도 하지 않는다.** 위 71~87행 주석이
    # 처음부터 그렇게 적혀 있었는데 쿼리에는 그 조건이 없었다.
    #
    # **왜 여태 안 보였나.** 08-24 까지 THERMAL 라벨 3건이 전부 `notified=0` 이라
    # 필터가 있든 없든 결과가 같았다. 08-23 에 처음으로 알림이 나간 THERMAL 사건
    # (#200·#202)이 생기면서 비로소 갈렸다 — 두 값이 우연히 같아 배선이 끊겨도
    # 참이던 경우와 정확히 같은 구조다.
    #
    # 고치지 않았으면 그 2건에 답하는 순간 6건이 차서 `[착수 가능]` 이 떴을 것이고,
    # **착수했을 때 손에 쥔 근거는 4건**이었을 것이다. 08-16 에 "전체 20건"을
    # "발열 축 6건"으로 바꾼 것과 같은 유형의 오류다(재는 대상이 조건과 어긋남).
    unnotified = f" AND COALESCE(i.notified,0)=0"
    rows = query(
        "SELECT COALESCE(i.user_label,'?') AS label, COUNT(*) AS n FROM incidents i"
        f" WHERE i.user_label IS NOT NULL AND UPPER(COALESCE(i.bottleneck,'')) IN ({placeholders})"
        f"{unnotified} AND NOT {_DURING_INJECTION} GROUP BY label",
        tuple(kinds),
    )
    by_label = {r["label"]: r["n"] for r in rows}
    total = sum(by_label.values())
    breakdown = ", ".join(f"{k} {v}" for k, v in sorted(by_label.items())) or "없음"

    # 세지 않은 것을 **숫자로 보여 준다.** 조용히 빼면 "왜 답했는데 안 늘지"가 된다.
    notified_labeled = query(
        "SELECT COUNT(*) AS n FROM incidents i"
        f" WHERE i.user_label IS NOT NULL AND UPPER(COALESCE(i.bottleneck,'')) IN ({placeholders})"
        f" AND COALESCE(i.notified,0)=1 AND NOT {_DURING_INJECTION}",
        tuple(kinds),
    )[0]["n"]

    # 전체 라벨도 함께 보여 준다 — 계획서의 옛 조건(20건)을 기억하는 사람이 두 수를
    # 나란히 봐야 왜 조건이 바뀌었는지 알 수 있다.
    everything = query(
        "SELECT COUNT(*) AS n FROM incidents WHERE user_label IS NOT NULL"
    )[0]["n"]

    result.checks.append(
        Check(
            f"{axis} 축 **미탐** 사람 라벨 {SEVERITY_AXIS_MIN_LABELS}건 이상",
            total >= SEVERITY_AXIS_MIN_LABELS,
            f"현재 {total}건 ({breakdown}) · 전체 라벨은 {everything}건"
            + (
                f" · 같은 축에서 알림이 나간 라벨 {notified_labeled}건은 세지 않았다"
                " (등급 역전은 미탐에만 답이 있다)"
                if notified_labeled
                else ""
            )
            + (
                ""
                if total >= SEVERITY_AXIS_MIN_LABELS
                else f" — {SEVERITY_AXIS_MIN_LABELS - total}건 부족"
            ),
            stalled=_axis_supply_stall(kinds, axis, days),
        )
    )
    return result


def _axis_supply_stall(kinds: list[str], axis: str, days: dict[str, Day]) -> str | None:
    """그 축의 **사건**이 더 안 생기고 있으면 그 사실을 말한다.

    라벨이 아니라 사건을 세는 이유: 라벨은 사람이 눌러야 생기므로 "안 눌렀다"와
    "생기지 않았다"가 섞인다. 조건이 막혔는지를 가르는 것은 **물어볼 사건이 오는가**다.

    이 판정은 원인을 말하지 않는다 — 문턱이 높아서인지, 하드웨어가 그런 것인지는
    데이터가 답할 수 없다. 말할 수 있는 것은 "관측은 계속됐는데 안 왔다"까지고,
    거기서부터는 사람이 본다.
    """
    from argus.dashboard.data import _DURING_INJECTION, query

    placeholders = ",".join("?" * len(kinds))
    rows = query(
        "SELECT MAX(i.ts_start) AS last_ts FROM incidents i"
        f" WHERE UPPER(COALESCE(i.bottleneck,'')) IN ({placeholders})"
        f" AND NOT {_DURING_INJECTION}",
        tuple(kinds),
    )
    last_ts = rows[0]["last_ts"] if rows else None
    if last_ts is None:
        # 사건이 한 번도 없었으면 "멈췄다"가 아니라 "시작한 적이 없다"다. 관측 기간이
        # 짧을 뿐일 수 있어 여기서 단정하지 않는다.
        return None

    last_day = dt.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d")
    since = [d for d in days if d > last_day]
    if len(since) < SUPPLY_STALL_DAYS:
        return None
    return (
        f"마지막 {axis} 사건이 {last_day[5:]} 이고 그 뒤 {len(since)}일을 관측했는데 "
        f"0건이다. 라벨을 안 누른 것이 아니라 **물어볼 사건이 오지 않는다** — "
        f"기다려서 채워질 조건이 아니므로 조건이나 축을 다시 봐야 한다."
    )


def main() -> int:
    days = _days()
    if not days:
        print("[대기] 롤업 데이터가 아직 없다. 상주 인스턴스가 도는지 먼저 확인할 것.")
        return 0

    print(f"관측 {len(days)}일: {_day_summary(days)}\n")
    if not _has_gpu():
        print("  참고: GPU 지표가 없어 '고부하/유휴' 구분이 성립하지 않는다.")
        print("        사용 패턴 다양성은 사람이 판단할 것.\n")

    reports = [
        check_severity_inversion(days),
        check_loss_axis(days),
        check_fingerprint(days),
        closed_regime(),
        closed_alarm_quality(),
    ]

    for report in reports:
        if report.done:
            mark = report.mark
        elif report.ok:
            mark = "[OK]"
        else:
            mark = "[막힘]" if report.stalled else "[대기]"
        print(f"{mark} {report.name}")
        print(f"      {report.note}")
        if report.done:
            print(f"      → {report.done}")
        for check in report.checks:
            print(f"      {'v' if check.ok else 'x'} {check.label}")
            print(f"        └ {check.detail}")
            if not check.ok and check.stalled:
                print(f"        ! {check.stalled}")
        print()

    ready = [r.name for r in reports if r.ok]
    print("착수 가능:", ", ".join(ready) if ready else "없음 — 더 모을 것")

    # **막힌 것은 따로 한 줄 더 말한다.** 위 목록 안에만 있으면 "아직 대기 중"으로
    # 읽히고, 그 오독이 정확히 07-29·08-19 를 만들었다.
    blocked = [r.name for r in reports if r.stalled]
    if blocked:
        print("막힌 조건:", ", ".join(blocked), "— 기다려도 안 찬다. 조건을 다시 볼 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

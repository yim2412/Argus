"""일일 리포트 롤업.

여기서 지키는 것은 **전부 조용히 깨지는 것들이다** — 예외가 아니라 숫자만 틀어지고,
화면은 그럴듯한 값을 계속 보여 준다. 리포트가 "어제 3시간 작업"이라고 하면 사용자는
그게 맞는지 확인할 방법이 없다.

1. **진행 중인 날은 접지 않는다.** 접으면 부분값이 확정으로 남는다.
2. **원본이 잘려 나간 날은 요약을 만들지 않는다.** `daily_report` 는 영구 보존인데
   원본은 하루면 지워지므로, 한 번 잘못 들어가면 고칠 방법이 없다.
3. **공백은 사용시간이 아니다.** 표본 간격을 그대로 더하면 PC 가 꺼져 있던 시간까지
   센다(실측: 상한 없이 361.6시간, 실제 13.8시간).
4. **시각당 한 번만 센다.** 같은 이름 프로세스가 여럿이면 시간이 몇 배가 된다.
5. **분류·시간대는 config 에서 온다.** 코드에 박히면 YAML 을 고쳐도 안 바뀐다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from argus.config.loader import RollupSettings, UsageSettings
from argus.report.builder import DailyReportRollup, categorize, slot_of
from argus.storage.hot import Database


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _midnight(days_ago: int) -> float:
    d = date.today() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day).timestamp()


def _day(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _session(db: Database, start: float, end: float) -> None:
    """관측 세션 하나. 리포트의 분모(`observed_s`)가 여기서 나온다."""
    db.insert_many("system_events", ("ts", "event"), [(start, "startup"), (end, "shutdown")])


def _foreground(db: Database, start: float, seconds: int, name: str, *, step: float = 1.0) -> None:
    """`start` 부터 1초 간격으로 포어그라운드 표본을 넣는다."""
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "foreground"),
        [(start + i * step, 100, name, 1) for i in range(seconds)],
    )


def _run(db: Database, **overrides) -> int:
    settings = RollupSettings(**overrides)
    return DailyReportRollup(db, settings, UsageSettings()).run_once()


def _row(db: Database, day: str) -> dict | None:
    rows = db.query("SELECT * FROM daily_report WHERE day = ?", (day,))
    return dict(rows[0]) if rows else None


# ----------------------------------------------------------------- 진행 중인 날


def test_today_is_never_folded(db: Database) -> None:
    """**오늘은 접지 않는다.** 아직 끝나지 않은 날을 접으면 부분값이 확정으로 남는다.

    **커버리지를 넉넉히 만든다.** 관측을 포어그라운드의 두 배로 두면 커버리지가 딱
    문턱(0.5)이라, "오늘이라서" 안 접힌 것인지 "원본이 부족해서" 안 접힌 것인지
    구분되지 않는다 — 실제로 mutation 에서 이 테스트가 아무것도 잡지 못했다.
    """
    start = _midnight(0) + 3600
    _session(db, start, start + 3600)
    _foreground(db, start, 3600, "chrome")

    _run(db)
    assert _row(db, _day(0)) is None, "오늘 리포트를 만들었다 — 하루가 끝나기 전이다"


def test_yesterday_is_folded(db: Database) -> None:
    """반대쪽. 이게 없으면 "아무것도 안 접는" 구현으로도 위 테스트가 통과한다.

    **관측 시간을 포어그라운드와 같게 둔다.** 2:1 로 두면 커버리지가 정확히 0.5 라
    문턱과 같은 값이 되고, 그러면 이 테스트가 재려는 것(어제는 접힌다)이 아니라
    부동소수 반올림이 결과를 정한다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 3600)
    _foreground(db, start, 3600, "chrome")

    assert _run(db) == 1
    row = _row(db, _day(1))
    assert row is not None and row["total_s"] == pytest.approx(3600, abs=5)


# --------------------------------------------------------------- 잘려 나간 원본


def test_a_day_whose_source_was_purged_is_not_stored(db: Database) -> None:
    """**원본이 잘린 날은 요약을 만들지 않는다.**

    `daily_report` 는 영구 보존인데 `process_metrics` 는 하루면 지워진다. 밀린 과거를
    그대로 접으면 "그날 6분 썼다"가 굳고, 원본이 없어 나중에 고칠 수도 없다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 10 * 3600)   # 10시간 관측
    _foreground(db, start, 360, "chrome")    # 그런데 원본은 6분치뿐 (1.0%)

    assert _run(db) == 0
    assert _row(db, _day(1)) is None, "원본이 1% 만 남았는데 요약을 만들었다"


def test_the_watermark_advances_even_when_the_day_is_skipped(db: Database) -> None:
    """건너뛴 날에서 멈추면 안 된다.

    워터마크가 그대로면 다음 틱도 같은 날을 보고 또 건너뛴다 — **영영 진행하지 못하고**,
    그동안 `retention` 은 이 롤업을 기다리느라 원본을 못 지워 DB 가 자란다.
    """
    start = _midnight(2) + 3600
    _session(db, start, start + 10 * 3600)
    _foreground(db, start, 360, "chrome")

    rollup = DailyReportRollup(db, RollupSettings(), UsageSettings())
    rollup.run_once()
    assert rollup.watermark() is not None and rollup.watermark() > start


def test_coverage_floor_comes_from_config(db: Database) -> None:
    """문턱이 config 에서 온다 — **기본값이 아닌 값으로 잰다.**

    코드 기본값(0.5)으로 재면 배선이 끊겨도 통과한다. 문턱을 0 으로 내리면 같은
    데이터가 저장돼야 한다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 10 * 3600)
    _foreground(db, start, 360, "chrome")

    assert _run(db, daily_report_min_coverage=0.0) == 1, (
        "문턱을 0 으로 내렸는데도 건너뛰었다 — 설정이 닿지 않는다"
    )


# ------------------------------------------------------------------- 공백 처리


def test_a_gap_is_not_usage(db: Database) -> None:
    """**표본 사이의 공백을 사용시간으로 세지 않는다.**

    표본 두 개 사이가 두 시간이면 그 두 시간은 PC 가 꺼져 있었거나 수집이 멈춘
    것이지 크롬을 본 시간이 아니다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    _foreground(db, start, 600, "chrome")                    # 10분
    _foreground(db, start + 2 * 3600, 600, "chrome")         # 두 시간 뒤 10분 더

    _run(db, daily_report_min_coverage=0.0)
    row = _row(db, _day(1))
    assert row is not None
    # 20분 + 공백 하나(상한 5초). 두 시간이 들어가면 7,200초가 된다.
    assert row["total_s"] == pytest.approx(1200, abs=30), (
        f"공백이 사용시간으로 들어갔다: {row['total_s']}초"
    )


def test_gap_cap_comes_from_config(db: Database) -> None:
    """상한이 config 에서 온다 — **기본값이 아닌 값으로 잰다.**

    상한을 크게 잡으면 같은 데이터에서 더 긴 시간이 나와야 한다. 값이 그대로면
    `daily_report_gap_cap_s` 가 닿지 않는 것이다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    # 30초 간격 표본 10개 — 상한 5초면 45초, 상한 60초면 270초로 세어진다.
    _foreground(db, start, 10, "chrome", step=30.0)

    _run(db, daily_report_min_coverage=0.0)
    tight = _row(db, _day(1))["total_s"]

    db.conn.execute("DELETE FROM daily_report")
    db.conn.execute("DELETE FROM rollup_state WHERE name='daily_report'")
    db.conn.commit()
    _run(db, daily_report_min_coverage=0.0, daily_report_gap_cap_s=60.0)
    loose = _row(db, _day(1))["total_s"]

    assert loose > tight * 2, f"상한을 12배로 늘렸는데 값이 그대로다: {tight} -> {loose}"


def test_one_moment_counts_once(db: Database) -> None:
    """**같은 시각의 행이 여럿이어도 한 번만 센다.**

    크롬처럼 프로세스가 많은 프로그램은 같은 순간에 행이 여러 개 남을 수 있는데,
    그걸 다 더하면 시간이 프로세스 수만큼 부풀어 오른다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    for pid in (100, 200, 300):  # 같은 시각·같은 이름, PID 만 다르다
        db.insert_many(
            "process_metrics",
            ("ts", "pid", "name", "foreground"),
            [(start + i, pid, "chrome", 1) for i in range(600)],
        )

    _run(db, daily_report_min_coverage=0.0)
    row = _row(db, _day(1))
    assert row["total_s"] == pytest.approx(600, abs=10), (
        f"프로세스 3개를 각각 세어 {row['total_s']}초가 됐다 (기대 600초)"
    )


# ------------------------------------------------------------- 분류·시간대 배선


def test_categories_come_from_config(db: Database) -> None:
    """분류가 config 에서 온다 — **기본 목록에 없는 이름으로 잰다.**

    기본값으로 재면 코드와 YAML 이 우연히 같아 배선이 끊겨도 통과한다
    (2026-08-04 에 같은 유형이 네 번 나왔다).
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    _foreground(db, start, 600, "나만의도구")

    usage = UsageSettings(categories={"내분류": ("나만의도구",)})
    DailyReportRollup(
        db, RollupSettings(daily_report_min_coverage=0.0), usage
    ).run_once()

    row = _row(db, _day(1))
    assert json.loads(row["by_category"]) .get("내분류"), (
        f"설정한 분류가 닿지 않았다: {row['by_category']}"
    )
    assert json.loads(row["top_apps"])[0]["category"] == "내분류"


def test_unmapped_names_are_kept_as_other(db: Database) -> None:
    """분류에 없는 이름을 **버리지 않는다.**

    카테고리 매핑은 이 PC 의 것이라 남의 PC 에서는 대부분 비어 있다. 매핑에 없다고
    시간을 빼면 그런 사용자에게는 리포트가 통째로 0 이 된다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    _foreground(db, start, 600, "듣도보도못한프로그램")

    DailyReportRollup(
        db, RollupSettings(daily_report_min_coverage=0.0), UsageSettings(categories={})
    ).run_once()

    row = _row(db, _day(1))
    assert row["total_s"] == pytest.approx(600, abs=10), "분류가 없다고 시간이 사라졌다"
    assert json.loads(row["by_category"]) == {"기타": pytest.approx(600, abs=10)}


def test_slots_come_from_config(db: Database) -> None:
    """시간대 경계가 config 에서 온다 — **기본값이 아닌 구간으로 잰다.**"""
    start = _midnight(1) + 3600  # 새벽 1시
    _session(db, start, start + 4 * 3600)
    _foreground(db, start, 600, "chrome")

    usage = UsageSettings(slots={"한밤중": (0, 5)})
    DailyReportRollup(
        db, RollupSettings(daily_report_min_coverage=0.0), usage
    ).run_once()

    assert "한밤중" in json.loads(_row(db, _day(1))["by_slot"])


def test_the_denominator_is_stored(db: Database) -> None:
    """**`observed_s` 를 함께 저장한다**(014 교훈).

    "3시간"은 그날 PC 를 4시간 켰는지 16시간 켰는지에 따라 뜻이 전혀 다른데,
    원본이 지워지고 나면 그 분모를 되돌릴 방법이 없다.
    """
    start = _midnight(1) + 3600
    _session(db, start, start + 4 * 3600)
    _foreground(db, start, 3600, "chrome")

    _run(db, daily_report_min_coverage=0.0)
    row = _row(db, _day(1))
    assert row["observed_s"] == pytest.approx(4 * 3600, abs=60), (
        f"관측 시간이 저장되지 않았다: {row['observed_s']}"
    )


# --------------------------------------------------------------------- 순수 함수


def test_the_rollup_is_wired_into_the_resident() -> None:
    """**배선을 로직과 따로 잰다.**

    위 테스트들은 롤업을 직접 만들어 부르므로, 상주가 이것을 **등록하지 않아도** 전부
    통과한다. 그러면 리포트는 영원히 안 만들어지고, 더 나쁘게는 `retention` 이 이
    롤업의 워터마크를 기다리느라 `process_metrics` 를 영영 못 지워 DB 가 자란다.

    설정도 함께 본다 — `usage` 를 넘기지 않으면 분류가 기본값으로 굳어 YAML 이
    무시된다.
    """
    import ast
    import inspect

    import argus.__main__ as main_module

    tree = ast.parse(inspect.getsource(main_module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DailyReportRollup"
    ]
    assert calls, "상주가 DailyReportRollup 을 등록하지 않는다 — 리포트가 만들어지지 않는다"

    args = [ast.unparse(a) for a in calls[0].args]
    assert "settings.usage" in args, (
        f"usage 설정을 넘기지 않는다 — 분류·시간대 YAML 이 무시된다: {args}"
    )


def test_retention_waits_for_this_rollup_too() -> None:
    """`process_metrics` 의 보존이 **이 롤업도** 기다린다.

    `process_5m` 만 보면 이 롤업이 접기 전에 원본이 지워진다 — 그날 리포트는 영영
    만들 수 없고, 같은 정보를 가진 테이블이 없어 복원도 안 된다.
    """
    from argus.config.loader import RetentionSettings
    from argus.storage.retention import Retention

    rules = {t: r for t, _keep, r in Retention(None, RetentionSettings())._rules()}  # noqa: SLF001
    assert "daily_report" in rules["process_metrics"], (
        f"process_metrics 가 daily_report 를 기다리지 않는다: {rules['process_metrics']}"
    )


def test_categorize_falls_back_to_other() -> None:
    assert categorize("chrome", {"브라우징": ("chrome",)}) == "브라우징"
    assert categorize("모르는것", {"브라우징": ("chrome",)}) == "기타"


def test_slot_of_allows_uncovered_hours() -> None:
    """구간이 하루를 다 덮지 않아도 된다 — 관심 있는 시간대만 정의하는 것도 설정이다."""
    slots = {"근무": (9, 18)}
    assert slot_of(10, slots) == "근무"
    assert slot_of(3, slots) is None

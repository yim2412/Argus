"""프로그램 사용시간 롤업.

여기서 지키는 것은 셋이다. 셋 다 **조용히 깨진다** — 예외가 아니라 값만 틀어지고,
표는 그럴듯한 숫자를 계속 보여 준다.

1. **같은 이름이 여럿 떠 있어도 한 번만 센다.** 크롬 프로세스 30개를 더하면
   관측 시간을 넘는 값이 나온다(실측: chrome 1,073시간 / 관측 383시간).
2. **모든 구간은 관측 세션 안에서 끝난다.** Argus 가 꺼져 있던 동안을 사용시간으로
   세면 안 되고, `exit` 이벤트 하나가 큐 드롭으로 사라져도 구간이 며칠로 늘면 안 된다.
3. **접히기 전의 `process_events` 는 지워지지 않는다.** 여기가 깨지면 그 날짜의
   사용시간은 영구히 복원 불가능하다 — 같은 정보를 가진 테이블이 없다.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from argus.config.loader import RetentionSettings, RollupSettings
from argus.storage.hot import Database
from argus.storage.retention import Retention
from argus.storage.rollup import ProgramUsageRollup, _intersect_seconds, _union

HOUR = 3600.0


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _midnight(days_ago: int) -> float:
    """`days_ago` 일 전의 로컬 자정. 롤업이 로컬 날짜로 자르므로 UTC 를 쓰면 안 된다."""
    d = date.today() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day).timestamp()


def _day(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _sessions(database: Database, marks: list[tuple[float, str]]) -> None:
    database.insert_many("system_events", ("ts", "event"), marks)


def _events(database: Database, rows: list[tuple]) -> None:
    database.insert_many("process_events", ("ts", "event", "pid", "name"), rows)


def _usage(database: Database, day: str, name: str) -> dict | None:
    rows = database.query(
        "SELECT * FROM program_usage_daily WHERE day = ? AND name = ?", (day, name)
    )
    return dict(rows[0]) if rows else None


# ------------------------------------------------------------------ 합집합


def test_union_merges_overlaps() -> None:
    assert _union([(0.0, 10.0), (5.0, 20.0)]) == [(0.0, 20.0)]
    assert _union([(0.0, 10.0), (10.0, 20.0)]) == [(0.0, 20.0)]  # 맞닿은 것도 하나다
    assert _union([(0.0, 5.0), (10.0, 15.0)]) == [(0.0, 5.0), (10.0, 15.0)]
    assert _union([(5.0, 5.0)]) == []  # 길이 0 은 구간이 아니다
    assert _intersect_seconds([(0.0, 10.0)], [(5.0, 20.0)]) == 5.0


def test_same_name_counted_once(db: Database) -> None:
    """크롬 프로세스 둘이 겹쳐 떠 있으면 겹친 시간은 한 번만 센다.

    구간은 [1h+60초, 3h] 과 [2h, 4h] 이다. 단순 합이면 7,140 + 7,200 = 14,340초,
    합집합은 10,740초다 — 이 차이가 이 기능의 전부다.
    """
    base = _midnight(2)
    _sessions(db, [(base + 1 * HOUR, "startup"), (base + 5 * HOUR, "shutdown")])
    _events(
        db,
        [
            (base + 1 * HOUR + 60, "start", 101, "chrome"),
            (base + 3 * HOUR, "exit", 101, None),
            (base + 2 * HOUR, "start", 102, "chrome"),
            (base + 4 * HOUR, "exit", 102, None),
        ],
    )

    rollup = ProgramUsageRollup(db, RollupSettings())
    assert rollup.run_once() > 0

    row = _usage(db, _day(2), "chrome")
    assert row is not None
    assert row["seconds"] == pytest.approx(3 * HOUR - 60, abs=1.0)
    assert row["observed_s"] == pytest.approx(4 * HOUR, abs=1.0)


# --------------------------------------------------------------- 세션 클램프


def test_missing_exit_is_cut_at_session_end(db: Database) -> None:
    """`exit` 이 없는 구간은 그 세션의 끝에서 잘린다.

    수집 큐가 가득 차면 오래된 표본부터 버리므로 `exit` 하나가 통째로 없을 수 있다.
    자르지 않으면 몇 분 살다 죽은 프로세스가 며칠을 실행한 것이 된다.
    """
    base = _midnight(3)
    _sessions(
        db,
        [
            (base + 1 * HOUR, "startup"),
            (base + 5 * HOUR, "shutdown"),
            # 다음 날에도 관측이 있었다. 잘리지 않으면 여기까지 이어진다.
            (base + 26 * HOUR, "startup"),
            (base + 30 * HOUR, "shutdown"),
        ],
    )
    _events(db, [(base + 2 * HOUR, "start", 201, "notepad")])  # exit 없음

    ProgramUsageRollup(db, RollupSettings()).run_once()

    first = _usage(db, _day(3), "notepad")
    assert first is not None
    assert first["seconds"] == pytest.approx(3 * HOUR, abs=1.0)  # 2h -> 세션 끝(5h)
    # 다음 날로 새어 나가지 않는다. 관측하지 않은 시간은 사용시간이 아니다.
    assert _usage(db, _day(2), "notepad") is None


def test_unclean_shutdown_ends_the_session(db: Database) -> None:
    """`unclean_shutdown` 의 `ts` 는 죽은 시각이다 — 그게 세션의 끝이다.

    이 행은 **다음 기동에 기록되지만** `ts` 는 마지막 데이터 시각으로 되돌려 놓는다
    (`runtime/session.py`). 이걸 기록 시각으로 다루면 세션들이 서로 겹쳐 관측 합계가
    달력 시간을 몇 배씩 넘는다 — 시제품이 16일(384h)을 11,873h 로 셌다.
    """
    base = _midnight(2)
    _sessions(
        db,
        [
            (base + 1 * HOUR, "startup"),
            (base + 3 * HOUR, "unclean_shutdown"),  # 전원이 끊겼다
            (base + 6 * HOUR, "startup"),
            (base + 8 * HOUR, "shutdown"),
        ],
    )
    # 죽기 전에 떠 있었고, 그 뒤로는 알 수 없다.
    _events(db, [(base + 2 * HOUR, "start", 301, "game")])

    ProgramUsageRollup(db, RollupSettings()).run_once()

    row = _usage(db, _day(2), "game")
    assert row is not None
    # 관측은 2h + 2h = 4h 다. 세션을 겹쳐 읽으면 7h 가 된다.
    assert row["observed_s"] == pytest.approx(4 * HOUR, abs=1.0)
    # 사용시간도 죽은 시각까지만이다. 공백(3h~6h)은 관측 밖이다.
    assert row["seconds"] == pytest.approx(1 * HOUR, abs=1.0)


# ------------------------------------------------------------------ 실행 횟수


def test_session_start_burst_is_not_a_launch(db: Database) -> None:
    """기동 직후의 `start` 폭주는 실행 횟수가 아니다.

    수집기는 첫 스냅샷에서 **이미 떠 있던 프로세스 전부**를 신규로 본다(`_known` 이
    비어 있으므로). 이걸 세면 부팅할 때마다 크롬을 200번 실행한 것이 된다.
    """
    base = _midnight(2)
    _sessions(db, [(base + 1 * HOUR, "startup"), (base + 5 * HOUR, "shutdown")])
    _events(
        db,
        [
            (base + 1 * HOUR + 1, "start", 401, "chrome"),  # 기동 직후 = 이미 떠 있던 것
            (base + 2 * HOUR, "exit", 401, None),
            (base + 3 * HOUR, "start", 402, "chrome"),  # 사용자가 실제로 실행했다
            (base + 4 * HOUR, "exit", 402, None),
        ],
    )

    ProgramUsageRollup(db, RollupSettings()).run_once()

    row = _usage(db, _day(2), "chrome")
    assert row is not None and row["launches"] == 1


# -------------------------------------------------------------------- 배선


def test_days_per_run_comes_from_config(db: Database) -> None:
    """**기본값이 아닌 값으로 잰다.** 7(코드 기본)로 재면 배선이 끊겨도 통과한다."""
    base = _midnight(5)
    marks: list[tuple[float, str]] = []
    rows: list[tuple] = []
    for i in range(5):
        day_start = base + i * 24 * HOUR
        marks += [(day_start + HOUR, "startup"), (day_start + 3 * HOUR, "shutdown")]
        # 날마다 행이 생겨야 "며칠이 접혔나"를 행으로 셀 수 있다.
        rows += [(day_start + 2 * HOUR, "start", 501 + i, "chrome")]
    _sessions(db, marks)
    _events(db, rows)

    rollup = ProgramUsageRollup(db, RollupSettings(program_usage_days_per_run=2))
    rollup.run_once()

    days = {row["day"] for row in db.query("SELECT DISTINCT day FROM program_usage_daily")}
    assert days == {_day(5), _day(4)}  # 2일만 접혔다


def test_interval_comes_from_config(db: Database) -> None:
    rollup = ProgramUsageRollup(db, RollupSettings(program_usage_interval_s=123.0))
    assert rollup.interval_s == 123.0


def test_today_is_not_folded(db: Database) -> None:
    """진행 중인 날을 접으면 부분값이 확정으로 남는다."""
    now = time.time()
    base = _midnight(0)
    _sessions(db, [(base, "startup")])
    _events(db, [(base + 60, "start", 601, "chrome")])

    ProgramUsageRollup(db, RollupSettings()).run_once(now=now)
    assert db.query("SELECT COUNT(*) AS c FROM program_usage_daily")[0]["c"] == 0


# ------------------------------------------------------------------- 보존


def test_events_survive_until_folded(db: Database) -> None:
    """접히지 않은 `process_events` 는 기한이 지나도 지워지지 않는다.

    보존 규칙에서 롤업 이름을 떼면(`None`) 이 테스트가 첫 단계에서 실패한다.
    """
    old = time.time() - 400 * 86400
    _events(db, [(old, "start", 701, "chrome")])
    retention = Retention(db, RetentionSettings())

    # 1) 워터마크가 그 시각에 못 미치면 지키다
    db.conn.execute(
        "INSERT INTO rollup_state (name, watermark_ts, updated_at) VALUES "
        "('program_usage_daily', ?, ?)",
        (old - 1, time.time()),
    )
    db.conn.commit()
    retention.purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM process_events")[0]["c"] == 1

    # 2) 접고 나면 지운다 — 아니면 DB 가 무한히 자란다
    db.conn.execute(
        "UPDATE rollup_state SET watermark_ts = ? WHERE name = 'program_usage_daily'",
        (time.time(),),
    )
    db.conn.commit()
    retention.purge_once()
    assert db.query("SELECT COUNT(*) AS c FROM process_events")[0]["c"] == 0

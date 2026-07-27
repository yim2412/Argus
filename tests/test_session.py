"""직전 세션 사후 판정 — `argus.runtime.session`.

정상 종료에서만 `shutdown` 이 남는다는 사실 자체는 고칠 수 없다(강제 종료에는 코드가 돌 기회가
없다). 그래서 다음 기동 때 역추적하는데, 이 판정이 조용히 망가지면 배포 후에야 알게 된다.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from argus.runtime.session import detect_unclean_shutdown


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE system_events (ts REAL NOT NULL, event TEXT NOT NULL,"
        " gap_seconds REAL, detail TEXT)"
    )
    c.execute("CREATE TABLE self_telemetry (ts REAL NOT NULL)")
    return c


def _startup(conn: sqlite3.Connection, ts: float) -> None:
    conn.execute("INSERT INTO system_events VALUES (?,?,?,?)", (ts, "startup", None, "{}"))


def test_첫_실행은_판정하지_않는다(conn):
    assert detect_unclean_shutdown(conn, boot_time=time.time() - 9999) is None


def test_정상_종료를_미종결로_보지_않는다(conn):
    now = time.time()
    _startup(conn, now - 3600)
    conn.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 600, "shutdown", None, "{}"))
    assert detect_unclean_shutdown(conn, boot_time=now - 9999) is None


def test_강제_종료를_잡고_사망시각에_기록한다(conn):
    now = time.time()
    death = now - 1800
    _startup(conn, now - 3600)
    conn.execute("INSERT INTO self_telemetry VALUES (?)", (death,))

    row = detect_unclean_shutdown(conn, boot_time=now - 9999)
    assert row is not None
    ts, event, gap, detail = row
    assert event == "unclean_shutdown"
    # ts 는 '지금'이 아니라 마지막 흔적의 시각이어야 시간순 정렬이 유지된다.
    assert ts == pytest.approx(death, abs=1.0)
    assert gap == pytest.approx(now - death, abs=1.0)
    assert json.loads(detail)["likely_cause"] == "process_killed_or_crash"


def test_사망_이후_부팅이면_재부팅으로_본다(conn):
    now = time.time()
    _startup(conn, now - 7200)
    conn.execute("INSERT INTO self_telemetry VALUES (?)", (now - 3600,))

    row = detect_unclean_shutdown(conn, boot_time=now - 300)
    assert json.loads(row[3])["likely_cause"] == "reboot_or_power_loss"


def test_같은_세션을_두_번_기록하지_않는다(conn):
    now = time.time()
    _startup(conn, now - 3600)
    conn.execute("INSERT INTO self_telemetry VALUES (?)", (now - 1800,))

    row = detect_unclean_shutdown(conn, boot_time=now - 9999)
    conn.execute("INSERT INTO system_events VALUES (?,?,?,?)", row)
    assert detect_unclean_shutdown(conn, boot_time=now - 9999) is None


def test_기동_직후_사망도_기록한다(conn):
    """데이터가 한 줄도 없는 세션 — 이게 가장 나쁜 신호라 놓치면 안 된다."""
    now = time.time()
    _startup(conn, now - 3600)

    row = detect_unclean_shutdown(conn, boot_time=now - 9999)
    assert row is not None
    assert json.loads(row[3])["session_uptime_s"] == 0.0


def test_흔적_테이블이_없어도_죽지_않는다(conn):
    """마이그레이션 이전 DB. 판정이 기동을 막으면 안 된다."""
    now = time.time()
    conn.execute("DROP TABLE self_telemetry")
    _startup(conn, now - 3600)

    row = detect_unclean_shutdown(conn, boot_time=now - 9999)
    assert row is not None
    # 흔적이 없으면 startup 시각으로 후퇴한다. 판정을 포기하는 것보다 낫다.
    assert row[0] == pytest.approx(now - 3600, abs=1.0)
    assert json.loads(row[3])["session_uptime_s"] == 0.0


def test_부팅_시각을_모르면_원인을_단정하지_않는다(conn):
    now = time.time()
    _startup(conn, now - 3600)
    conn.execute("INSERT INTO self_telemetry VALUES (?)", (now - 1800,))

    # psutil 이 없거나 실패한 환경. 틀린 단정보다 '불명'이 낫다.
    from argus.runtime import session as mod

    original = mod._boot_time
    mod._boot_time = lambda: None
    try:
        row = detect_unclean_shutdown(conn)
    finally:
        mod._boot_time = original
    assert json.loads(row[3])["likely_cause"] == "unknown"

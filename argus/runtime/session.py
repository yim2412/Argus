"""직전 세션이 정상 종료됐는지 사후 판정한다.

**왜 필요한가**: `shutdown` 이벤트는 시그널 핸들러를 거친 정상 종료에서만 기록된다
(`__main__` 의 종료 경로). 그런데 상주 프로그램이 실제로 죽는 방식은 대부분 그게 아니다 —
Windows 종료, 전원 차단, BSOD, 작업 관리자 강제 종료, 크래시. 이때는 아무 흔적도 남지 않고,
`system_events` 에는 `startup` 만 연달아 쌓인다.

남의 PC 에서 나는 문제는 재현이 불가능하다. "Argus 가 어제 밤부터 안 돌았다"는 제보를 받았을 때
그게 사용자가 껐기 때문인지, 재부팅됐기 때문인지, 크래시인지 구분할 근거가 없으면 진단이
성립하지 않는다. 그래서 **다음 기동 시점에 직전 세션의 마지막 흔적을 역추적해** 기록한다.

**판정 방법**: 마지막 `startup` 이후 종결 이벤트가 없으면 미종결로 본다. 사망 시각은 그 뒤에
기록된 마지막 데이터 행의 타임스탬프로 추정한다(`self_telemetry` 가 5초마다 쓰이므로 해상도는
그 수준). 부팅 시각이 사망 추정 시각보다 뒤면 재부팅·전원 차단, 아니면 같은 부팅 세션 안에서
프로세스만 죽은 것 — 후자가 크래시일 가능성이 높은 쪽이다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

# 직전 세션이 남긴 마지막 흔적을 찾을 테이블. 주기적으로 쓰이는 것만 골라야
# 사망 시각 추정이 촘촘해진다.
_TRACE_TABLES = ("self_telemetry", "metrics_raw", "process_metrics")

# 세션의 끝을 뜻하는 이벤트. 이 중 하나라도 마지막 startup 뒤에 있으면 이미 결론이 난 세션이다.
_TERMINAL_EVENTS = ("shutdown", "unclean_shutdown")


def detect_unclean_shutdown(conn: sqlite3.Connection, boot_time: float | None = None) -> tuple | None:
    """직전 세션이 미종결이면 `system_events` 한 행을, 아니면 None 을 돌려준다.

    새 `startup` 을 기록하기 **전에** 호출해야 한다. 그래야 "마지막 startup" 이
    직전 세션을 가리킨다.
    """
    row = conn.execute(
        "SELECT ts FROM system_events WHERE event = 'startup' ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None  # 첫 실행
    prev_startup = float(row[0])

    placeholders = ",".join("?" * len(_TERMINAL_EVENTS))
    closed = conn.execute(
        f"SELECT 1 FROM system_events WHERE ts >= ? AND event IN ({placeholders}) LIMIT 1",
        (prev_startup, *_TERMINAL_EVENTS),
    ).fetchone()
    if closed is not None:
        return None  # 정상 종료였거나, 이미 미종결로 판정해 기록해 둔 세션이다

    last_data = _last_trace_ts(conn, prev_startup)
    if last_data is None:
        # startup 은 찍혔는데 데이터가 한 줄도 없다 = 기동 직후 죽었다. 그 자체가 신호다.
        last_data = prev_startup

    if boot_time is None:
        boot_time = _boot_time()

    now = time.time()
    detail: dict[str, Any] = {
        "prev_startup_ts": round(prev_startup, 3),
        "last_data_ts": round(last_data, 3),
        "session_uptime_s": round(last_data - prev_startup, 1),
        # 아무도 보고 있지 않았던 시간. 이 구간은 베이스라인 학습에서 빠져야 한다.
        "unobserved_s": round(now - last_data, 1),
        "likely_cause": _classify(last_data, boot_time),
    }
    if boot_time is not None:
        detail["boot_time"] = round(boot_time, 3)

    log.warning("직전 세션이 정상 종료되지 않았다", extra=detail)

    # ts 는 '지금'이 아니라 사망 추정 시각으로 둔다. 그래야 이벤트가 실제 시간순으로
    # 정렬되고, 공백 구간의 시작점을 그대로 읽어낼 수 있다.
    return (last_data, "unclean_shutdown", round(now - last_data, 3), json.dumps(detail, ensure_ascii=False))


def _last_trace_ts(conn: sqlite3.Connection, after: float) -> float | None:
    best: float | None = None
    for table in _TRACE_TABLES:
        try:
            row = conn.execute(f"SELECT MAX(ts) FROM {table} WHERE ts >= ?", (after,)).fetchone()
        except sqlite3.Error:
            # 테이블이 아직 없을 수 있다(마이그레이션 이전 DB). 나머지로 계속 추정한다.
            continue
        if row and row[0] is not None:
            ts = float(row[0])
            if best is None or ts > best:
                best = ts
    return best


def _classify(last_data: float, boot_time: float | None) -> str:
    if boot_time is None:
        return "unknown"
    # 부팅이 사망 이후면 그 사이에 재부팅이 있었다는 뜻. 여유 5초는 부팅 시각 보고의 흔들림.
    if boot_time > last_data + 5.0:
        return "reboot_or_power_loss"
    return "process_killed_or_crash"


def _boot_time() -> float | None:
    try:
        import psutil

        return float(psutil.boot_time())
    except Exception:
        return None


if __name__ == "__main__":  # 스모크: python -m argus.runtime.session
    from ..logging_setup import setup

    setup(level="ERROR")  # 경고 로그가 스모크 출력을 가리지 않게

    def fresh() -> sqlite3.Connection:
        c = sqlite3.connect(":memory:")
        c.execute(
            "CREATE TABLE system_events (ts REAL NOT NULL, event TEXT NOT NULL,"
            " gap_seconds REAL, detail TEXT)"
        )
        c.execute("CREATE TABLE self_telemetry (ts REAL NOT NULL)")
        return c

    now = time.time()

    # 1) 첫 실행 — 판정할 직전 세션이 없다
    c = fresh()
    r = detect_unclean_shutdown(c, boot_time=now - 10000)
    print(f"  첫 실행: {r} (None 이어야 정상)")
    if r is not None:
        print("[FAIL] 이력이 없는데 미종결로 판정했다")
        raise SystemExit(1)

    # 2) 정상 종료된 세션 — 조용해야 한다
    c = fresh()
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 3600, "startup", None, "{}"))
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 600, "shutdown", None, "{}"))
    r = detect_unclean_shutdown(c, boot_time=now - 10000)
    print(f"  정상 종료 이력: {r} (None 이어야 정상)")
    if r is not None:
        print("[FAIL] 정상 종료를 미종결로 오인했다")
        raise SystemExit(1)

    # 3) 같은 부팅 세션 안에서 프로세스만 죽은 경우
    c = fresh()
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 3600, "startup", None, "{}"))
    c.execute("INSERT INTO self_telemetry VALUES (?)", (now - 1800,))
    r = detect_unclean_shutdown(c, boot_time=now - 10000)
    if r is None:
        print("[FAIL] 미종결 세션을 놓쳤다")
        raise SystemExit(1)
    d = json.loads(r[3])
    print(f"  강제 종료: 원인={d['likely_cause']}, 미관측 {d['unobserved_s']:.0f}초, ts=사망추정({r[0]:.0f})")
    if d["likely_cause"] != "process_killed_or_crash":
        print(f"[FAIL] 원인 추정이 잘못됐다: {d['likely_cause']}")
        raise SystemExit(1)
    if abs(r[0] - (now - 1800)) > 1:
        print("[FAIL] 이벤트 ts 가 사망 추정 시각이 아니다")
        raise SystemExit(1)

    # 4) 재부팅 — 사망 이후에 부팅이 있었다
    c = fresh()
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 7200, "startup", None, "{}"))
    c.execute("INSERT INTO self_telemetry VALUES (?)", (now - 3600,))
    r = detect_unclean_shutdown(c, boot_time=now - 300)
    d = json.loads(r[3])
    print(f"  재부팅: 원인={d['likely_cause']}, 세션 가동 {d['session_uptime_s']:.0f}초")
    if d["likely_cause"] != "reboot_or_power_loss":
        print(f"[FAIL] 재부팅을 감지하지 못했다: {d['likely_cause']}")
        raise SystemExit(1)

    # 5) 중복 판정 방지 — 이미 기록한 미종결 세션을 다시 세면 안 된다
    c = fresh()
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 3600, "startup", None, "{}"))
    c.execute("INSERT INTO self_telemetry VALUES (?)", (now - 1800,))
    row = detect_unclean_shutdown(c, boot_time=now - 10000)
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", row)
    again = detect_unclean_shutdown(c, boot_time=now - 10000)
    print(f"  재판정: {again} (None 이어야 정상)")
    if again is not None:
        print("[FAIL] 같은 세션을 두 번 기록한다")
        raise SystemExit(1)

    # 6) 기동 직후 사망 — 데이터가 한 줄도 없는 경우
    c = fresh()
    c.execute("INSERT INTO system_events VALUES (?,?,?,?)", (now - 3600, "startup", None, "{}"))
    r = detect_unclean_shutdown(c, boot_time=now - 10000)
    d = json.loads(r[3])
    print(f"  기동 직후 사망: 가동 {d['session_uptime_s']:.0f}초")
    if r is None or d["session_uptime_s"] != 0.0:
        print("[FAIL] 데이터 없는 세션을 제대로 다루지 못했다")
        raise SystemExit(1)

    print("[OK] runtime.session")

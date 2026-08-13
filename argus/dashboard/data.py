"""대시보드 조회 계층.

**대시보드가 관측 대상을 오염시키면 안 된다.** 이 도구는 "PC 가 느린 이유"를 찾는
프로그램인데, 그걸 보는 화면이 CPU 를 먹고 디스크를 때리면 자기가 만든 이상을
자기가 관측하게 된다. 그래서 셋을 지킨다.

- **읽기 전용 연결**(`mode=ro`). 대시보드가 실수로도 쓰지 못한다.
- **캐시**. 새로고침마다 같은 쿼리를 다시 돌리지 않는다(`ttl` 은 데이터 주기에 맞춘다).
- **범위 제한**. `SELECT *` 로 전 구간을 긁지 않고 항상 시간 창을 건다.

수집기가 도는 중에도 안전한 이유는 WAL 이다 — 읽기가 쓰기를 막지 않는다.

**UI 프레임워크에 의존하지 않는다.** 원래 캐시가 `st.cache_data` 였는데, 그러면 이
계층이 Streamlit 없이는 못 돈다 — 네이티브 창(PySide6)으로 옮기는 순간 조회 코드까지
따라 옮겨야 한다. 캐시는 "같은 질문을 짧은 시간 안에 다시 묻지 않는다"는 것뿐이고,
그건 UI 와 아무 상관이 없다. 2026-08-03 에 `ttl_cache` 로 바꿔 떼어냈다.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

from ..paths import data_dir, db_path
from ..storage import history


def ttl_cache(ttl: float):
    """호출 결과를 `ttl` 초 동안 재사용한다.

    `functools.lru_cache` 를 쓰지 않는 이유: 시간 기반 만료가 없어 대시보드가 영원히
    옛 값을 보여 준다. 여기서 필요한 것은 "얼마나 오래된 값까지 괜찮은가"다.

    락을 두는 이유: Qt 는 워커 스레드에서 조회하고 메인 스레드에서 그린다. 캐시가
    스레드 안전하지 않으면 그 경계에서 깨진다.
    """

    def decorate(fn):
        store: dict[tuple, tuple[float, Any]] = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit is not None and now - hit[0] < ttl:
                    return hit[1]
            value = fn(*args, **kwargs)
            with lock:
                store[key] = (now, value)
            return value

        wrapper.cache_clear = store.clear  # 테스트가 캐시를 비울 수 있어야 한다
        wrapper.cache_ttl = ttl
        return wrapper

    return decorate


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """읽기 전용 조회. 커넥션은 요청마다 열고 닫는다(오래 붙들면 WAL 이 커진다)."""
    try:
        conn = _connect()
    except sqlite3.OperationalError:
        return []
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        # 마이그레이션 이전 DB 나 아직 없는 테이블. 화면 하나가 비는 게
        # 대시보드 전체가 죽는 것보다 낫다.
        return []
    finally:
        conn.close()


def db_exists() -> bool:
    return db_path().exists()


# ---------------------------------------------------------------- 실시간


# 수집이 1초 주기이므로 캐시도 1초다. 2초였던 것은 Streamlit 의 rerun 폭주를 막으려던
# 값인데, 네이티브 창은 폴링 주기를 스스로 정하므로 그 캐시가 오히려 갱신을 절반으로
# 떨어뜨린다(예광탄 실측: 12초에 6개). 단일 행 조회라 1초로 낮춰도 부하는 무시할 수준이다.
@ttl_cache(1.0)
def latest_metrics() -> dict | None:
    rows = query("SELECT * FROM metrics_raw ORDER BY ts DESC LIMIT 1")
    return rows[0] if rows else None


@ttl_cache(1.0)
def latest_gpu() -> list[dict]:
    return query(
        "SELECT * FROM gpu_metrics WHERE ts = (SELECT MAX(ts) FROM gpu_metrics) ORDER BY gpu_index"
    )


@ttl_cache(5.0)
def recent_metrics(seconds: int = 600) -> list[dict]:
    return query(
        "SELECT ts, cpu_total, cpu_max_core, mem_percent, disk_read_bps, disk_write_bps, "
        "disk_queue, disk_resp_ms, net_rx_bps, net_tx_bps, ctx_switches_ps "
        "FROM metrics_raw WHERE ts > ? ORDER BY ts",
        (time.time() - seconds,),
    )


@ttl_cache(5.0)
def recent_gpu(seconds: int = 600) -> list[dict]:
    return query(
        "SELECT ts, util_percent, vram_used_mb, temp_c, power_w FROM gpu_metrics "
        "WHERE ts > ? AND gpu_index = 0 ORDER BY ts",
        (time.time() - seconds,),
    )


# ---------------------------------------------------------------- 장기(롤업)


@ttl_cache(30.0)
def rollup(hours: float = 24.0) -> list[dict]:
    """장기 지표. **웜(Parquet)까지 읽는다.**

    `metrics_1m` 만 보면 이틀 지난 날짜가 통째로 빈다 — 내보낸 뒤 SQLite 에서 지워지기
    때문이다. 2026-07-29 에 타임라인에서 07-28 이 실제로 빈 채 그려지고 있었다.
    """
    return history.rollup_range(time.time() - hours * 3600)


@ttl_cache(60.0)
def rollup_span() -> dict | None:
    result = history.span("metrics")
    if result is None:
        return None
    lo, hi, buckets = result
    return {"lo": lo, "hi": hi, "n": buckets}


@ttl_cache(60.0)
def warm_exports() -> list[dict]:
    return query("SELECT * FROM warm_exports ORDER BY date_key DESC LIMIT 30")


@ttl_cache(60.0)
def warm_span() -> dict | None:
    """웜에 며칠치가 들어 있는지 `{days, lo, hi}`.

    **크기(KB)만 보여주면 안 되는 이유**: 저장소 화면의 테이블 목록은 핫 보유분이라
    이틀 지난 롤업이 빠져나가면 보유 시간이 줄어든다. 어디로 갔는지 화면에 없으면
    사용자는 데이터가 사라진다고 읽는다.
    """
    rows = warm_exports()
    days = sorted({row["date_key"] for row in rows})
    if not days:
        return None
    return {"days": len(days), "lo": days[0], "hi": days[-1]}


# ---------------------------------------------------------------- 프로세스


@ttl_cache(5.0)
def top_processes(seconds: int = 60, limit: int = 20) -> list[dict]:
    """최근 창에서 프로세스별 평균 사용량.

    같은 이름의 프로세스가 여럿일 수 있어(chrome) 이름으로 합친다 — 사용자가 보는
    단위는 PID 가 아니라 프로그램이다.
    """
    return query(
        "SELECT name, COUNT(DISTINCT pid) AS pids, AVG(cpu_percent) AS cpu, "
        "MAX(cpu_percent) AS cpu_max, AVG(rss_mb) AS rss, MAX(handles) AS handles, "
        "MAX(foreground) AS foreground "
        "FROM process_metrics WHERE ts > ? GROUP BY name "
        "ORDER BY cpu DESC LIMIT ?",
        (time.time() - seconds, limit),
    )


@ttl_cache(10.0)
def process_series(name: str, seconds: int = 1800) -> list[dict]:
    return query(
        "SELECT ts, SUM(cpu_percent) AS cpu, SUM(rss_mb) AS rss, SUM(handles) AS handles "
        "FROM process_metrics WHERE ts > ? AND name = ? GROUP BY ts ORDER BY ts",
        (time.time() - seconds, name),
    )


# ---------------------------------------------------------------- 자기 계측


@ttl_cache(5.0)
def self_telemetry(hours: float = 8.0) -> list[dict]:
    return query(
        "SELECT * FROM self_telemetry WHERE ts > ? ORDER BY ts",
        (time.time() - hours * 3600,),
    )


@ttl_cache(30.0)
def rollup_state() -> dict | None:
    rows = query("SELECT * FROM rollup_state WHERE name='metrics_1m'")
    return rows[0] if rows else None


@ttl_cache(30.0)
def table_counts() -> list[dict]:
    """테이블별 보유 구간. 보존 정책이 실제로 도는지 여기서 보인다."""
    out = []
    for table in (
        "metrics_raw",
        "gpu_metrics",
        "process_metrics",
        "net_connections",
        "process_events",
        "self_telemetry",
        "metrics_1m",
        "process_5m",
        "net_activity_5m",
        "anomaly_signals",
    ):
        # 여기 보이는 것은 **핫(SQLite) 보유분**이다. 롤업은 이틀 지나면 웜으로 옮겨가며
        # 여기서 사라지는데, 그게 정상 동작이다(사라진 만큼은 Parquet 에 있다).
        column = {"metrics_1m": "ts_min", "process_5m": "ts_5m", "net_activity_5m": "ts_5m"}.get(
            table, "ts"
        )
        rows = query(f"SELECT MIN({column}) AS lo, MAX({column}) AS hi, COUNT(*) AS n FROM {table}")
        if rows and rows[0]["n"]:
            row = dict(rows[0])
            row["table"] = table
            out.append(row)
    return out


def db_size_bytes() -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path()) + suffix)
        if path.exists():
            total += path.stat().st_size
    return total


def warm_size_bytes() -> int:
    warm = data_dir() / "warm"
    if not warm.is_dir():
        return 0
    return sum(f.stat().st_size for f in warm.rglob("*.parquet"))


# ---------------------------------------------------------------- 탐지·평가


@ttl_cache(15.0)
def anomaly_signals(hours: float = 24.0) -> list[dict]:
    """실시간 탐지 신호만. `run_id` 가 있는 것은 리플레이 평가 결과라 제외한다."""
    return query(
        "SELECT ts, detector, score, severity, features FROM anomaly_signals "
        "WHERE ts > ? AND run_id IS NULL ORDER BY ts",
        (time.time() - hours * 3600,),
    )


@ttl_cache(60.0)
def fault_injections(hours: float = 24.0) -> list[dict]:
    return query(
        "SELECT * FROM fault_injections WHERE ts_start > ? ORDER BY ts_start",
        (time.time() - hours * 3600,),
    )


@ttl_cache(60.0)
def eval_runs(limit: int = 40) -> list[dict]:
    return query("SELECT * FROM eval_runs ORDER BY ts DESC, detector LIMIT ?", (limit,))


@ttl_cache(10.0)
def incidents(days: float = 7.0, limit: int = 200) -> list[dict]:
    return query(
        "SELECT * FROM incidents WHERE ts_start > ? ORDER BY ts_start DESC LIMIT ?",
        (time.time() - days * 86400, limit),
    )


# 답 대기로 세는 기간. 이보다 오래된 알림은 "그때 실제로 느렸나"를 사용자가 기억하지
# 못한다 — 기억이 아니라 짐작으로 붙인 라벨은 문턱을 고칠 근거가 되지 못하므로,
# 모을 수 있는 양보다 답할 수 있는 양에 맞춘다.
LABEL_WINDOW_DAYS = 14.0


@ttl_cache(10.0)
def unlabeled_notified(days: float = LABEL_WINDOW_DAYS) -> list[dict]:
    """답을 안 준 알림. **알림이 나간 것만 센다.**

    사건 전체를 물으면 아무도 답하지 않는다(2026-08-14 기준 사건 173건 · 알림 50건 ·
    라벨 0건). 실제로 사용자를 방해한 것은 발송된 알림이고, 문턱과 억제를 고칠 근거도
    거기서 나온다 — 알림이 안 나간 사건은 맞았든 틀렸든 아무도 성가시게 하지 않았다.

    최신순인 것은 화면이 "가장 최근 것부터 답하기"로 쓰기 때문이다. 기억이 남아 있는
    쪽부터 물어야 라벨이 실제 판단이 된다.
    """
    return query(
        "SELECT id, ts_start, severity, title FROM incidents"
        " WHERE notified = 1 AND user_label IS NULL AND ts_start > ?"
        " ORDER BY ts_start DESC",
        (time.time() - days * 86400,),
    )


@ttl_cache(5.0)
def health() -> dict:
    """**"지금 괜찮은가"의 답 한 줄.** 창 맨 위가 이것만 쓴다.

    사용자가 창을 여는 이유는 "지금 괜찮은가"이지 "CPU 가 몇 %인가"가 아니다.
    그 답은 이미 `incidents` 에 문장으로 들어 있는데(`title` 이 "디스크 병목 —
    chrome 68%" 형태다) 지금까지는 사건 탭을 열어야만 보였다.

    돌려주는 것은 넷이다 — 진행 중인 사건, 마지막으로 끝난 사건의 시각, 최신 표본의
    시각, 그리고 답을 안 준 알림 수. 셋째가 있어야 **"조용한 것"과 "죽은 것"을 가른다**:
    수집이 멈추면 사건도 안 생기므로 둘 다 똑같이 조용해 보인다.

    **넷째가 여기 있는 이유**는 이 줄이 창에서 유일하게 항상 보이는 자리이기 때문이다.
    라벨 경로는 08-09 에 뚫렸는데 5일 뒤에도 0건이었다 — 창을 열어도 "답할 것이 있다"는
    신호가 어디에도 없었고, 라벨 자리까지는 탭·선택·스크롤 세 단계가 걸린다.
    """
    open_rows = query(
        "SELECT id, ts_start, severity, title, bottleneck FROM incidents"
        " WHERE ts_end IS NULL ORDER BY ts_start DESC LIMIT 1"
    )
    closed = query(
        "SELECT ts_end FROM incidents WHERE ts_end IS NOT NULL"
        " ORDER BY ts_end DESC LIMIT 1"
    )
    sample = query("SELECT MAX(ts) AS ts FROM metrics_raw")
    return {
        "open": open_rows[0] if open_rows else None,
        "last_end_ts": closed[0]["ts_end"] if closed else None,
        "sample_ts": sample[0]["ts"] if sample else None,
        "unlabeled": len(unlabeled_notified()),
    }


@ttl_cache(10.0)
def incident_signals(incident_id: int) -> list[dict]:
    return query(
        "SELECT * FROM incident_signals WHERE incident_id = ? ORDER BY ts", (incident_id,)
    )


def set_user_label(incident_id: int, label: str | None) -> None:
    """피드백 저장. **여기만 쓰기를 한다.**

    대시보드는 읽기 전용이라는 규칙의 유일한 예외다. 피드백은 사용자가 화면에서
    주는 것이라 다른 경로가 없다. 그래서 이 함수만 별도로 쓰기 연결을 열고,
    닫는다 — 조회 계층(`query`)은 계속 읽기 전용으로 둔다.
    """
    conn = sqlite3.connect(str(db_path()), timeout=5.0)
    try:
        conn.execute(
            "UPDATE incidents SET user_label = ?, labeled_at = ? WHERE id = ?",
            (label, time.time() if label else None, incident_id),
        )
        conn.commit()
    finally:
        conn.close()
    # 방금 쓴 값이 곧바로 보이게 캐시를 비운다. **`st.cache_data` 시절에는 `.clear()`
    # 였다** — `ttl_cache` 로 바꾸면서 이름이 `cache_clear` 가 됐고, 그대로 두었으면
    # 피드백을 누를 때마다 AttributeError 가 났다(2026-08-03, 사건 페이지 이식 중 발견).
    #
    # **답 대기 수도 같이 비운다.** 방금 답한 것이 맨 윗줄의 "답하기 N건"에 안 빠지면
    # 사용자는 답이 저장되지 않았다고 읽는다 — 목록은 즉시 바뀌는데 카운트만 최대
    # 10초 남아 있는, 예외 없이 값만 어긋나는 종류다.
    incidents.cache_clear()
    unlabeled_notified.cache_clear()
    health.cache_clear()


# ------------------------------------------------------------ 프로그램 사용시간


def _day_cutoff(days: int) -> str:
    """`days` 일 전의 로컬 날짜 문자열. 저장된 `day` 와 같은 기준이어야 한다."""
    return (date.today() - timedelta(days=max(0, days - 1))).isoformat()


# 하루에 한 번 접히는 값이라 캐시를 길게 잡는다. 창을 열 때마다 다시 물을 이유가 없다.
@ttl_cache(300.0)
def program_usage(days: int = 30, limit: int = 100, user_only: bool = False) -> list[dict]:
    """최근 `days` 일 누적 사용시간 상위.

    **`observed_s` 를 함께 돌려준다.** "38시간"만 보면 많은지 적은지 알 수 없다 —
    그동안 PC 가 몇 시간 켜져 있었는지가 있어야 뜻이 생긴다.

    `user_only` 는 **사람이 직접 쓰는 프로그램만** 남긴다(포어그라운드 이력이 있는
    것). 그러지 않으면 상위가 전부 배경 서비스다 — svchost 238h · conhost 209h ·
    runtimebroker 200h. 정의대로 동작한 결과지만 "내가 무엇을 얼마나 했나"의 답은
    아니다. 판정은 `program_info.foreground_seen` 에 있고 `collector/proginfo` 가 채운다.
    """
    where = "u.day >= ?"
    params: list[Any] = [_day_cutoff(days)]
    if user_only:
        # **`INNER JOIN` 으로 바꾸지 않는다.** 아래 LEFT JOIN 이 그대로 있어야
        # 설명이 없는 프로그램(버전 리소스가 없는 exe)도 남는다.
        where += " AND i.foreground_seen = 1"
        excluded = usage_exclude()
        if excluded:
            where += f" AND u.name NOT IN ({', '.join('?' * len(excluded))})"
            params.extend(excluded)
    params.append(limit)
    return query(
        "SELECT u.name AS name, SUM(u.seconds) AS seconds, SUM(u.launches) AS launches,"
        "       COUNT(*) AS days, i.description AS description, i.company AS company"
        " FROM program_usage_daily u"
        # **LEFT JOIN 이다.** 설명은 나중에 채워지는 표시용 값이라, 아직 없다고
        # 사용시간 행이 사라지면 안 된다.
        " LEFT JOIN program_info i ON i.name = u.name"
        f" WHERE {where}"
        " GROUP BY u.name ORDER BY seconds DESC LIMIT ?",
        params,
    )


@ttl_cache(60.0)
def usage_exclude() -> tuple[str, ...]:
    """사용시간 표에서 뺄 이름들 (`config` 의 `usage.exclude`).

    **설정을 못 읽어도 표는 떠야 한다.** 조회 계층이 설정 오류로 죽으면 창 전체가
    빈손이 된다 — 그 경우 필터가 조금 헐거워질 뿐이고, 그편이 낫다.

    캐시가 짧은 이유(60초)는 사용자가 YAML 을 고치고 창을 다시 열지 않아도 되게
    하기 위해서다. 조회 자체가 파일 한 번 읽기라 비싸지 않다.
    """
    try:
        from ..config.loader import load_settings

        return tuple(load_settings().usage.exclude)
    except Exception:
        return ()


@ttl_cache(300.0)
def program_descriptions() -> dict[str, str]:
    """이름 → 사람 말 설명. 프로세스 표처럼 사용시간 밖에서도 쓴다.

    **캐시가 길다**(5분). 하루에 몇 개 늘까 말까 한 값이라 자주 물을 이유가 없다.
    """
    return {
        row["name"]: row["description"]
        for row in query(
            "SELECT name, description FROM program_info WHERE description IS NOT NULL"
        )
    }


@ttl_cache(300.0)
def program_usage_observed(days: int = 30) -> float:
    """같은 창에서 Argus 가 관측한 총 시간(초). 위 값들의 분모다.

    날짜별 `observed_s` 는 그 날의 모든 행에 같은 값이 들어 있으므로 `MAX` 로 하나만
    집는다. 그냥 `SUM` 하면 프로그램 종수만큼 곱해진다.
    """
    rows = query(
        "SELECT SUM(o) AS total FROM (SELECT MAX(observed_s) AS o FROM program_usage_daily "
        "WHERE day >= ? GROUP BY day)",
        (_day_cutoff(days),),
    )
    return float(rows[0]["total"] or 0.0) if rows else 0.0


@ttl_cache(60.0)
def system_events(hours: float = 24.0) -> list[dict]:
    return query(
        "SELECT * FROM system_events WHERE ts > ? ORDER BY ts DESC",
        (time.time() - hours * 3600,),
    )

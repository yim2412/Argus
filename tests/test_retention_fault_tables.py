"""주입 구간에서는 채점이 읽는 **모든** 테이블이 함께 살아남는다.

`FAULT_PROTECTED` 에 처음에는 프로세스 쪽 둘만 있었다. 그 결과가 2026-08-02 에 드러났다 —
07-30 주입 배치의 `process_metrics` 는 보호되어 그대로인데 `metrics_raw` 는 지워져,
`analyze_incident()` 가 병목을 관측하지 못하고 자원을 기본값으로 되돌렸다. **제품 경로 귀인이
0/7 = 0%** 로 나왔고, 탐지가 퇴행한 것처럼 보였지만 실제로는 채점할 데이터의 절반이 없었다.

이 실패는 조용하다. 정리는 정상 종료하고, 로그의 `fault_windows_held` 도 정상이며, 라벨과
프로세스 행은 멀쩡히 남아 있다. **며칠 뒤 채점을 돌릴 때에야 수치로 나타난다** — 그때는
이미 데이터가 없어서 원인과 결과를 잇기 어렵다. 그래서 테이블 목록을 여기서 고정한다.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from argus.config.loader import RetentionSettings
from argus.storage.hot import Database
from argus.storage.retention import Retention

GUARD = 900.0
PLANNED = 720.0

# 채점이 주입 구간에서 읽는 것. (테이블, 그 행을 만드는 INSERT 의 추가 컬럼)
SCORED_TABLES = {
    "metrics_raw": ("(ts, cpu_total) VALUES (?, 50.0)",),
    "gpu_metrics": ("(ts, gpu_index, util_percent) VALUES (?, 0, 50.0)",),
    "process_metrics": ("(ts, pid, name, handles) VALUES (?, 4242, 'python', 900)",),
}


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _insert(database: Database, table: str, ts: float) -> None:
    (tail,) = SCORED_TABLES[table]
    with database._lock:  # noqa: SLF001
        database.conn.execute(f"INSERT INTO {table} {tail}", (ts,))
        database.conn.commit()


def _rows_at(database: Database, table: str, ts: float) -> int:
    return database.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE ts = ?", (ts,)
    ).fetchone()[0]


@pytest.fixture()
def scene(db: Database):
    """주입 구간 하나와, 그 안·밖·최근에 놓인 행 세 벌.

    구간과 구간 밖 행은 **둘 다 보존 기한을 넘겼다.** 보호가 없으면 함께 지워지므로,
    "안은 남고 밖은 지워진다"가 보호가 실제로 걸렸다는 증거가 된다.
    """
    now = time.time()
    start = now - 7200.0
    end = start + PLANNED
    inside = start + 300.0      # 주입 구간 안
    outside = now - 4000.0      # 보호 밖 + 기한 초과 → 지워져야 한다
    recent = now - 100.0        # 기한 안 → 남는다

    with db._lock:  # noqa: SLF001
        db.conn.execute(
            "INSERT INTO fault_injections (scenario, ts_start, ts_end, completed) "
            "VALUES ('handle_leak', ?, ?, 1)",
            (start, end),
        )
        # 롤업이 접기 전에는 원본을 지우지 않는 규칙이 있다. 접었다고 표시해 둬야
        # 정리가 실제로 DELETE 를 돌린다 — 안 그러면 전부 남아 테스트가 통과해 버린다.
        for name in ("metrics_1m", "process_5m", "net_activity_5m"):
            db.conn.execute(
                "INSERT INTO rollup_state (name, watermark_ts, updated_at) VALUES (?, ?, ?)",
                (name, now, now),
            )
        db.conn.commit()

    for table in SCORED_TABLES:
        for ts in (inside, outside, recent):
            _insert(db, table, ts)

    retention = Retention(
        db,
        RetentionSettings(raw_hours=1, process_hours=1, fault_guard_s=GUARD),
    )
    return retention, inside, outside, recent


def test_scored_tables_survive_inside_the_fault_window(db: Database, scene) -> None:
    """주입 구간 안의 행은 세 테이블 모두 살아남는다.

    `metrics_raw`·`gpu_metrics` 가 빠지면 여기서 걸린다 — 07-30 배치를 못 쓰게 만든 그것이다.
    """
    retention, inside, _outside, _recent = scene
    retention.purge_once()

    for table in SCORED_TABLES:
        assert _rows_at(db, table, inside) == 1, f"{table} 의 주입 구간 행이 지워졌다"


def test_expired_rows_outside_the_window_are_still_purged(db: Database, scene) -> None:
    """보호는 주입 구간에만 걸린다 — 밖의 기한 초과 행은 그대로 지워진다.

    이게 없으면 위 테스트는 "아무것도 안 지우는 정리"로도 통과한다.
    """
    retention, _inside, outside, recent = scene
    retention.purge_once()

    for table in SCORED_TABLES:
        assert _rows_at(db, table, outside) == 0, f"{table} 의 기한 초과 행이 남았다"
        assert _rows_at(db, table, recent) == 1, f"{table} 의 기한 안 행이 지워졌다"


def test_protected_tables_cover_what_scoring_reads() -> None:
    """보호 목록이 채점이 읽는 것을 덮는지 이름으로도 고정한다.

    위 두 테스트는 정리 동작을 보지만, 새 테이블이 채점에 들어올 때 그것을 목록에
    넣으라고 말해 주지는 못한다. 여기서 세 갈래(기여도·정답 PID·병목 분류)를 명시한다.
    """
    from argus.storage.retention import FAULT_PROTECTED

    for table in ("process_metrics", "process_events", "metrics_raw", "gpu_metrics"):
        assert table in FAULT_PROTECTED, f"{table} 이 주입 구간 보호에서 빠졌다"

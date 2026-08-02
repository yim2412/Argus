"""평가용 스냅샷 — 같은 입력으로 반복 채점할 수 있어야 한다.

리플레이 평가의 재생 구간이 실행마다 줄어든다(보존 정리가 앞부분을 지운다). 2026-08-02 에
세 번 돌렸더니 1401.6 → 1371.6 → 1346.6분이었고, `rules` 의 오탐이 22 → 21 → 20 으로 준
것이 **고친 효과인지 구간이 짧아진 효과인지 가릴 수 없었다.**

여기서 고정하는 것은 두 가지다.

    구간을 자른다      — 주입 구간 ± 여유만 담아 스냅샷이 원본만큼 커지지 않게
    조용한 시간을 담는다 — **이게 없으면 오탐률이 부풀려진다** (실측 80% → 100%)

두 번째가 특히 조용히 깨진다. 주입 구간만 담아도 스냅샷은 멀쩡히 만들어지고 채점도
정상 종료하며, 수치는 **더 좋게** 나온다 — 좋아 보이니 의심하지 않게 된다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import eval_snapshot  # noqa: E402

from argus.paths import ENV_DATA_DIR  # noqa: E402
from argus.storage.hot import Database  # noqa: E402


def _seed(db: Database, start: float, *, seconds: int, injections: list[tuple[float, float]]) -> None:
    db.insert_many(
        "metrics_raw",
        ("ts", "cpu_total", "mem_percent"),
        [(start + i, 20.0, 30.0) for i in range(seconds)],
    )
    db.insert_many(
        "process_metrics",
        ("ts", "pid", "name", "cpu_percent", "handles"),
        [(start + i, 100, "app", 5.0, 200) for i in range(seconds)],
    )
    db.insert_many(
        "net_connections",
        ("ts", "pid", "name", "raddr", "rport", "status"),
        [(start + i, 100, "app", "203.0.113.7", 443, "ESTABLISHED") for i in range(0, seconds, 10)],
    )
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [
            (i + 1, "handle_leak", lo, hi, 100, "{}", 0, 1)
            for i, (lo, hi) in enumerate(injections)
        ],
    )


@pytest.fixture()
def source_db(tmp_path: Path, monkeypatch):
    """`%APPDATA%` 대신 임시 폴더를 쓰는 원본 DB."""
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path))
    database = Database(tmp_path / "argus.db").open()
    yield database
    database.close()


# ------------------------------------------------------------- 구간 계산


def test_quiet_windows_do_not_overlap_the_injections() -> None:
    """조용한 구간은 주입과 겹치지 않는다. 겹치면 오탐 분모에 주입이 섞인다."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE metrics_raw (ts REAL)")
    conn.executemany("INSERT INTO metrics_raw VALUES (?)", [(float(i),) for i in range(0, 40000, 10)])

    injected = [(10000.0, 12000.0), (20000.0, 22000.0)]
    quiet = eval_snapshot._quiet_windows(conn, injected, hours=1.0)

    assert quiet, "조용한 구간을 하나도 못 찾았다"
    for lo, hi in quiet:
        for ilo, ihi in injected:
            assert hi <= ilo or lo >= ihi, f"주입 구간과 겹친다: ({lo}, {hi}) vs ({ilo}, {ihi})"
    assert sum(hi - lo for lo, hi in quiet) == pytest.approx(3600.0, abs=1.0)


def test_quiet_hours_zero_means_none() -> None:
    """0 을 주면 담지 않는다 — 다만 그 선택은 오탐률을 못 재게 만든다."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE metrics_raw (ts REAL)")
    conn.executemany("INSERT INTO metrics_raw VALUES (?)", [(float(i),) for i in range(1000)])
    assert eval_snapshot._quiet_windows(conn, [(100.0, 200.0)], hours=0.0) == []


# ------------------------------------------------------------- 스냅샷 생성


def _make(tmp_path: Path, **kwargs):
    import argparse

    args = argparse.Namespace(ids=None, out="t", force=True, quiet_hours=0.0)
    for key, value in kwargs.items():
        setattr(args, key, value)
    assert eval_snapshot.make(args) == 0
    return tmp_path / "eval_snapshots" / "t.db"


def test_snapshot_keeps_the_injection_and_drops_the_rest(source_db: Database, tmp_path: Path) -> None:
    """구간 안은 남고 밖은 사라진다.

    밖이 지워지는지도 함께 보는 이유: "아무것도 안 지우는" 구현이면 위쪽만으로는 통과한다.
    그러면 스냅샷이 원본만큼 커져 도구의 목적이 사라진다.
    """
    start = 1_000_000.0
    # 여유(fault_guard_s, 기본 900초)를 감안해 넉넉히 떨어뜨린다.
    _seed(source_db, start, seconds=8000, injections=[(start + 4000, start + 4600)])

    target = _make(tmp_path)

    snap = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    inside = snap.execute(
        "SELECT COUNT(*) FROM metrics_raw WHERE ts BETWEEN ? AND ?",
        (start + 4000, start + 4600),
    ).fetchone()[0]
    outside = snap.execute(
        "SELECT COUNT(*) FROM metrics_raw WHERE ts < ?", (start + 1000,)
    ).fetchone()[0]
    snap.close()

    assert inside == 601, f"주입 구간이 온전하지 않다: {inside}행"
    assert outside == 0, f"구간 밖이 남았다: {outside}행 — 스냅샷이 원본만큼 커진다"


def test_snapshot_carries_quiet_time_when_asked(source_db: Database, tmp_path: Path) -> None:
    """`--quiet-hours` 를 주면 주입 밖 시간이 함께 담긴다.

    **오탐률은 이 구간에서만 나온다.** 2026-08-02 실측에서 주입 구간만 담은 스냅샷은
    같은 탐지기를 정밀도 80.0%(FP 2) 대신 100.0%(FP 0)로 보고했다.
    """
    start = 1_000_000.0
    _seed(source_db, start, seconds=20000, injections=[(start + 15000, start + 15600)])

    bare = _make(tmp_path, out="bare", quiet_hours=0.0)
    with_quiet = _make(tmp_path, out="quiet", quiet_hours=2.0)

    def rows(path: Path) -> int:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM metrics_raw").fetchone()[0]
        finally:
            conn.close()

    bare_path = bare.with_name("bare.db")
    quiet_path = with_quiet.with_name("quiet.db")
    assert rows(quiet_path) > rows(bare_path) + 7000, (
        f"조용한 2시간이 담기지 않았다: {rows(bare_path)} → {rows(quiet_path)}행"
    )


def test_snapshot_never_carries_network_destinations(source_db: Database, tmp_path: Path) -> None:
    """네트워크 목적지는 담지 않는다 (규칙 5).

    채점이 쓰지 않는 데다, 스냅샷은 파일 하나라 원본 DB 보다 훨씬 쉽게 옮겨 다닌다.
    """
    start = 1_000_000.0
    _seed(source_db, start, seconds=8000, injections=[(start + 4000, start + 4600)])

    target = _make(tmp_path)

    snap = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    remaining = snap.execute("SELECT COUNT(*) FROM net_connections").fetchone()[0]
    snap.close()
    assert remaining == 0, f"네트워크 연결 {remaining}행이 스냅샷에 남았다"

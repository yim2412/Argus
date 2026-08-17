"""판정용 스냅샷(`--export-findings`)이 담을 것만 담고, 원본은 건드리지 않는다.

2026-08-17 에 두 번째 기계(내장그래픽 노트북)를 붙이면서 만들었다. 이 경로는 **남의
PC 에서 매일 자동으로 도는데 아무도 화면을 보지 않는다** — 조용히 틀리면 몇 주 뒤
"데이터가 이상한데 왜인지 모르겠다"로만 나타난다. 그래서 세 가지를 고정한다.

1. **개인정보가 새지 않는다.** `net_connections` 에는 네트워크 목적지가 들어 있다
   (설계 규칙 5). 스냅샷은 기계 밖으로 나가는 유일한 물건이라 여기가 마지막 관문이다.
2. **원본에 쓰지 않는다.** 상주가 그 DB 를 쓰는 중에 돈다.
3. **없는 표에 죽지 않는다.** 다른 기계는 스키마 버전이 다를 수 있다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from argus.storage.findings import (
    EXCLUDED_TABLES,
    INCLUDED_TABLES,
    export_findings,
    prune_snapshots,
    resolve_out_path,
)
from argus.storage.hot import Database


@pytest.fixture()
def source(tmp_path: Path):
    """실제 스키마로 만든 원본 DB. 담길 것과 빠질 것을 모두 채워 둔다."""
    db = Database(tmp_path / "src.db").open()
    with db._lock:  # noqa: SLF001
        # 담겨야 하는 것
        db.conn.execute(
            "INSERT INTO incidents (ts_start, severity, bottleneck, title) "
            "VALUES (1000.0, 'warning', 'CPU', 'CPU 병목 — test 40%')"
        )
        db.conn.execute(
            "INSERT INTO self_telemetry (ts, cpu_percent, rss_mb) VALUES (1000.0, 1.5, 120.0)"
        )
        # **빠져야 하는 것.** 목적지를 흉내 낸 값을 넣는다 — 이것이 스냅샷에 나타나면
        # 그대로 유출이다.
        db.conn.execute(
            "INSERT INTO net_connections (ts, pid, name, raddr, rport, status) "
            "VALUES (1000.0, 42, 'chrome', '203.0.113.77', 443, 'ESTABLISHED')"
        )
        db.conn.commit()
    yield db
    db.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _rows(path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return list(conn.execute(f'SELECT * FROM "{table}"'))
    finally:
        conn.close()


def test_개인정보_표는_담기지_않는다(source: Database, tmp_path: Path):
    """**막지 않았으면 무엇이 일어났을 것인가를 먼저 단언한다.**

    원본에 목적지 IP 가 실제로 들어 있음을 확인한 뒤에 스냅샷에 없음을 본다.
    이 앞단언이 없으면 원본이 비어 있어도 테스트가 통과해 — 보호를 통째로 뜯어내도
    초록불이 된다.
    """
    src_rows = _rows(source.path, "net_connections")
    assert src_rows, "원본에 네트워크 연결 행이 있어야 이 테스트가 의미를 가진다"
    assert any("203.0.113.77" in str(v) for v in src_rows[0]), "원본에 목적지 IP 가 있어야 한다"

    out = tmp_path / "snap.db"
    export_findings(out, source=source.path)

    assert "net_connections" not in _tables(out)
    # 표 이름만 보는 것으로 끝내지 않는다 — 다른 표에 목적지가 섞여 들어갔을 수도 있다.
    conn = sqlite3.connect(str(out))
    try:
        for (table,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall():
            for row in conn.execute(f'SELECT * FROM "{table}"'):
                assert not any(
                    "203.0.113.77" in str(v) for v in row
                ), f"{table} 에 목적지 IP 가 섞여 있다"
    finally:
        conn.close()


def test_원본_대용량_표도_빠진다(source: Database, tmp_path: Path):
    out = tmp_path / "snap.db"
    export_findings(out, source=source.path)
    tables = _tables(out)
    for excluded in EXCLUDED_TABLES:
        assert excluded not in tables, f"{excluded} 는 담기면 안 된다"


def test_판정용_표는_내용까지_담긴다(source: Database, tmp_path: Path):
    """존재만 보지 않는다. 빈 표를 만들어도 '담겼다'가 되기 때문이다."""
    out = tmp_path / "snap.db"
    result = export_findings(out, source=source.path)

    assert _rows(out, "incidents") == _rows(source.path, "incidents")
    assert _rows(out, "self_telemetry") == _rows(source.path, "self_telemetry")
    assert result["tables"]["incidents"] == 1
    assert result["tables"]["self_telemetry"] == 1
    assert result["rows_total"] >= 2


def test_원본에_쓰지_않는다(source: Database, tmp_path: Path):
    """상주가 쓰는 중인 DB 를 건드리면 안 된다.

    행 수뿐 아니라 **파일 내용 전체**를 해시로 비교한다. 행 수만 보면 값이 바뀌거나
    스키마가 건드려진 것을 놓친다.
    """
    import hashlib

    source.close()  # 해시를 재려면 WAL 이 본체에 접혀 있어야 한다
    before = hashlib.sha256(Path(source.path).read_bytes()).hexdigest()

    out = tmp_path / "snap.db"
    export_findings(out, source=source.path)

    after = hashlib.sha256(Path(source.path).read_bytes()).hexdigest()
    assert before == after, "원본 DB 파일이 변경됐다"


def test_없는_표는_missing_에_남고_죽지_않는다(source: Database, tmp_path: Path):
    """다른 기계는 스키마 버전이 다를 수 있다. 그때 조용히 넘기지도, 죽지도 않는다."""
    with source._lock:  # noqa: SLF001
        source.conn.execute("DROP TABLE eval_runs")
        source.conn.commit()

    out = tmp_path / "snap.db"
    result = export_findings(out, source=source.path)

    assert "eval_runs" in result["missing"]
    assert "eval_runs" not in _tables(out)
    # 나머지는 정상적으로 담긴다 — 표 하나가 없다고 스냅샷 전체를 버리지 않는다.
    assert result["tables"]["incidents"] == 1


def test_기계_정보가_함께_담긴다(source: Database, tmp_path: Path):
    """**파일 하나로 끝나야 한다.** 어느 기계 것인지 모르는 스냅샷은 비교에 못 쓴다."""
    out = tmp_path / "snap.db"
    export_findings(out, source=source.path)

    conn = sqlite3.connect(str(out))
    try:
        meta = dict(conn.execute("SELECT key, value FROM export_meta"))
    finally:
        conn.close()

    assert "machine_profile" in meta
    assert "exported_at" in meta
    assert meta["source_db"] == str(source.path)
    # 뺀 표의 이유가 함께 남는다 — 나중에 "왜 이게 없지?" 를 코드 없이 답할 수 있게.
    assert "net_connections" in meta["excluded"]


def test_다시_뽑으면_지난_회차가_섞이지_않는다(source: Database, tmp_path: Path):
    """이어붙이면 언제 것인지 모르는 스냅샷이 된다."""
    out = tmp_path / "snap.db"
    export_findings(out, source=source.path)
    first = result_rows = _rows(out, "incidents")
    assert len(first) == 1

    export_findings(out, source=source.path)
    assert len(_rows(out, "incidents")) == 1, "두 번 뽑았더니 행이 늘었다"
    assert _rows(out, "incidents") == result_rows


def test_오래된_스냅샷_정리는_최신_N개를_남긴다(tmp_path: Path):
    import os
    import time

    for i in range(5):
        p = tmp_path / f"snap-{i}.db"
        p.write_bytes(b"x")
        # mtime 으로 고르므로 확실히 벌려 둔다.
        os.utime(p, (time.time() + i, time.time() + i))

    removed = prune_snapshots(tmp_path, keep=2)

    assert len(removed) == 3
    left = sorted(p.name for p in tmp_path.glob("*.db"))
    assert left == ["snap-3.db", "snap-4.db"], f"최신 2개가 남아야 하는데 {left}"


def test_정리를_끄면_지우지_않는다(tmp_path: Path):
    for i in range(3):
        (tmp_path / f"snap-{i}.db").write_bytes(b"x")
    assert prune_snapshots(tmp_path, keep=0) == []
    assert len(list(tmp_path.glob("*.db"))) == 3


def test_담을_표와_뺄_표가_겹치지_않는다():
    """목록을 손으로 고치다 같은 표를 양쪽에 적는 사고를 막는다."""
    assert not (set(INCLUDED_TABLES) & set(EXCLUDED_TABLES))


def test_폴더를_주면_날짜가_든_이름을_짓는다(tmp_path: Path):
    """**파일명이 곧 생존 신호다.** 매일 같은 이름으로 덮어쓰면 그것을 잃는다."""
    import time as _time

    first = resolve_out_path(tmp_path)
    assert first.parent == tmp_path
    assert first.suffix == ".db"
    assert _time.strftime("%Y-%m-%d") in first.name, f"날짜가 없다: {first.name}"
    # 같은 날 두 번 불러도 같은 이름이어야 한다 — 하루치가 하나다.
    assert resolve_out_path(tmp_path) == first


def test_파일을_주면_그대로_쓴다(tmp_path: Path):
    """손으로 뽑을 때는 이름을 직접 정할 수 있어야 한다."""
    target = tmp_path / "내가정한이름.db"
    assert resolve_out_path(target) == target


def test_폴더_이름이_날짜를_덮어쓰지_않는다(tmp_path: Path):
    """`.db` 로 끝나지 않으면 폴더다. 아직 없는 폴더여도 그렇다.

    `is_dir()` 로 판단하면 **처음 실행에서만** 파일로 오인해, 그날 스냅샷 하나가
    폴더가 아니라 그 이름의 파일로 떨어진다. 다음 날부터는 정상이라 재현이 어렵다.
    """
    missing = tmp_path / "아직없는폴더"
    out = resolve_out_path(missing)
    assert out.parent == missing
    assert out.name.endswith(".db")

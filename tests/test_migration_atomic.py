"""마이그레이션은 파일 하나가 **통째로** 적용되거나 통째로 안 된다.

감사 F-012: `sqlite3.Connection.executescript` 는 실행 전에 COMMIT 하고 스크립트를 자동 커밋으로
돌린다 — 실패 뒤 `rollback()` 이 되돌릴 것이 없었다. 여러 문장짜리 마이그레이션(실제로 005·012·018·020)이
중간에 실패하면 앞 문장(ADD COLUMN)은 남고 `user_version` 은 안 올라가, **다음 기동부터
`duplicate column` 으로 영원히 DB 를 못 연다** — 마이그레이션을 고쳐 재배포해도. 배포 후
업데이트에서 사용자 PC 의 상주가 벽돌이 되는 경로다(수집·저장 규칙 4 가 막으려던 바로 그 일).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from argus.storage import hot


@pytest.fixture()
def migrations(tmp_path: Path, monkeypatch):
    folder = tmp_path / "migrations"
    folder.mkdir()
    (folder / "001_init.sql").write_text(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);\nCREATE TABLE t (a INTEGER);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hot, "migration_files", lambda: sorted((int(p.name[:3]), p) for p in folder.iterdir()))
    return folder


def _columns(db_file: Path) -> list[str]:
    with sqlite3.connect(db_file) as conn:
        return [r[1] for r in conn.execute("PRAGMA table_info(t)")]


def _version(db_file: Path) -> int:
    with sqlite3.connect(db_file) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_failed_migration_leaves_no_partial_change(tmp_path, migrations):
    (migrations / "002_bad.sql").write_text(
        "ALTER TABLE t ADD COLUMN b INTEGER;\nSELECT * FROM 없는_표;\n", encoding="utf-8"
    )
    db_file = tmp_path / "x.db"
    with pytest.raises(sqlite3.Error):
        hot.Database(db_file).open()
    assert _version(db_file) == 1, "실패한 002 의 버전이 올라갔다"
    assert "b" not in _columns(db_file), "실패한 마이그레이션의 앞 문장이 남았다 — 고쳐 배포해도 다시 못 돈다"


def test_fixed_migration_applies_after_a_failed_attempt(tmp_path, migrations):
    """사용자 PC 에서 일어날 순서 그대로: 실패 → 고친 판 배포 → 재기동이 된다."""
    bad = migrations / "002_bad.sql"
    bad.write_text("ALTER TABLE t ADD COLUMN b INTEGER;\nSELECT * FROM 없는_표;\n", encoding="utf-8")
    db_file = tmp_path / "x.db"
    with pytest.raises(sqlite3.Error):
        hot.Database(db_file).open()
    bad.write_text("ALTER TABLE t ADD COLUMN b INTEGER;\n", encoding="utf-8")
    db = hot.Database(db_file).open()
    db.close()
    assert _version(db_file) == 2
    assert _columns(db_file) == ["a", "b"]


def test_successful_multi_statement_migration_applies_every_statement(tmp_path, migrations):
    """대조 — 감싸기가 정상 경로를 망가뜨리지 않는다(문장 전부 + 버전)."""
    (migrations / "002_ok.sql").write_text(
        "ALTER TABLE t ADD COLUMN b INTEGER;\nALTER TABLE t ADD COLUMN c TEXT;\n"
        "CREATE INDEX idx_t_b ON t (b);\n",
        encoding="utf-8",
    )
    db_file = tmp_path / "x.db"
    hot.Database(db_file).open().close()
    assert _version(db_file) == 2
    assert _columns(db_file) == ["a", "b", "c"]


def test_db_from_a_newer_version_opens_with_a_visible_warning(tmp_path, migrations, caplog):
    """새 버전이 만든 DB 를 옛 코드로 열면 **막지 않고 드러낸다** (감사 F-013 — 사용자 결정: 경고만).

    처음엔 아무 검사 없이 돌았다. 막으면 되돌린 순간 모니터가 통째로 멈춘다.
    """
    import logging
    import time

    from argus.desktop.app import _health_line

    db_file = tmp_path / "x.db"
    hot.Database(db_file).open().close()
    assert hot.schema_ahead(1) is None, "대조: 같은 판이면 조용하다"
    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA user_version = 7")                # 더 새 Argus 가 만든 것처럼
    with caplog.at_level(logging.WARNING):
        hot.Database(db_file).open().close()                   # 기동이 막히면 안 된다
    assert any("더 새 버전" in r.getMessage() for r in caplog.records), "로그에 안 남았다"
    note = hot.schema_ahead(7)
    assert note and "7 > 1" in note
    now = time.time()
    text, detail, _c, _id = _health_line({"sample_ts": now - 1, "open": None, "last_end_ts": None,
                                          "unlabeled": 0, "broken": [], "schema_note": note}, now)
    assert text == "설정 확인이 필요합니다" and "더 새 버전" in detail

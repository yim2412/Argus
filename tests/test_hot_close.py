"""커넥션 닫기 — 정리 최적화가 프로세스의 성패를 뒤집지 않는다.

2026-08-14 02:19 에 웜 내보내기 자식이 `PRAGMA optimize` 의 `database is locked`
하나로 종료 코드 1 이 됐고, 부모는 "웜 내보내기 자식이 실패했다"로 남겼다.
**그 회차는 내보낼 날짜조차 없어 실제로는 아무 일도 하지 않은 회차였다.**

`PRAGMA optimize` 는 ANALYZE 를 돌릴 수 있어 쓰기 트랜잭션을 잡는데, 상주 본체가
백필 중이면 `busy_timeout` 10초를 넘긴다(같은 날 25초짜리 쓰기가 관측됐다).
데이터에는 영향이 없는 정리 작업이므로 실패해도 닫기는 끝나야 한다.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from argus.storage.hot import Database


class _Spy:
    """진짜 커넥션 앞의 얇은 대역.

    `sqlite3.Connection` 은 속성이 읽기 전용이라 `execute` 를 직접 바꿀 수 없다.
    **커넥션 자체를 가짜로 만들지는 않는다** — `commit`·`close` 가 진짜로 돌아야
    "닫혔는가"를 실제로 잰다.
    """

    def __init__(self, conn, fail_optimize: Exception | None = None) -> None:
        self._conn = conn
        self._fail = fail_optimize
        self.seen: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.seen.append(sql)
        if self._fail is not None and "optimize" in sql.lower():
            raise self._fail
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _spy_on(db: Database, error: Exception | None = None) -> _Spy:
    spy = _Spy(db._conn, error)
    db._conn = spy
    return spy


def test_close_survives_a_locked_optimize(tmp_path, caplog) -> None:
    """**락 때문에 최적화가 실패해도 닫기는 끝난다.**

    여기서 예외가 새면 그것을 부른 프로세스가 통째로 실패하고, 상위는 성공한
    작업까지 실패로 읽는다 — 08-14 의 웜 내보내기가 정확히 그랬다.
    """
    db = Database(tmp_path / "t.db").open()
    _spy_on(db, sqlite3.OperationalError("database is locked"))

    with caplog.at_level(logging.DEBUG, logger="argus.storage.hot"):
        db.close()  # 예외가 새면 여기서 실패한다

    assert db._conn is None, "최적화가 실패했다고 커넥션을 열어 둔 채 남겼다"
    assert any("최적화" in r.message for r in caplog.records), (
        "조용히 넘어갔다 — 잦아지면 락 경합 자체를 봐야 하는데 흔적이 없다(설계 규칙 4)"
    )


def test_close_still_reports_real_failures(tmp_path) -> None:
    """**삼키는 것은 `OperationalError` 뿐이다.**

    닫기 경로에서 나는 모든 예외를 삼키면 진짜 고장이 조용해진다 — 그것이
    이 프로젝트가 반복해서 다친 자리다(규칙 4). 범위를 넓히면 여기서 걸린다.
    """
    db = Database(tmp_path / "t.db").open()
    _spy_on(db, MemoryError("이건 락이 아니다"))

    with pytest.raises(MemoryError):
        db.close()

    assert db._conn is None, "예외가 나도 커넥션은 닫혀야 한다(finally)"


def test_close_optimizes_when_it_can(tmp_path) -> None:
    """**평소에는 최적화가 실제로 돈다.** 예외 처리를 붙이면서 통째로 빠지면
    이 테스트가 아니라 성능이 조용히 나빠진다 — 그건 몇 달 뒤에나 보인다."""
    db = Database(tmp_path / "t.db").open()
    spy = _spy_on(db)
    db.close()

    assert any("optimize" in sql.lower() for sql in spy.seen), "정리 최적화를 아예 안 돌렸다"

"""대시보드 검증 — 프로세스·HTTP·반환값으로만 확인한다.

**마우스를 움직이는 자동화를 쓰지 않는다**(CLAUDE.md). 창이 떴는지·오류가 없는지·
데이터가 맞는지는 프로세스와 응답으로 알 수 있다.

여기서 잡으려는 실패는 둘이다.
- import 시점 오류(오타·잘못된 참조) — 페이지를 열어야만 터지므로 눈으로는 늦게 발견된다
- 조회 계층이 쓰기를 시도하는 것 — 대시보드는 읽기 전용이어야 한다
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "argus" / "dashboard"


def test_pages_exist() -> None:
    pages = sorted(p.name for p in (DASHBOARD / "pages").glob("*.py"))
    assert pages == [
        "1_Live.py",
        "2_Timeline.py",
        "3_Processes.py",
        "4_Health.py",
        "5_Incidents.py",
    ]


def test_modules_import() -> None:
    """페이지가 아닌 모듈은 streamlit 런타임 없이도 import 되어야 한다."""
    pytest.importorskip("streamlit")
    pytest.importorskip("plotly")

    from argus.dashboard import common, data, theme  # noqa: F401

    assert len(theme.SERIES) == 4
    assert theme.layout()["paper_bgcolor"] == theme.SURFACE
    assert theme.line("x", [1], [2], slot=0)["line"]["width"] == 2


def test_pages_compile() -> None:
    """페이지 파일의 문법·참조 오류를 실행 없이 잡는다.

    streamlit 페이지는 실행하면 `st.set_page_config` 부터 런타임을 요구하므로
    컴파일까지만 확인한다. 오타는 여기서 다 걸린다.
    """
    for page in (DASHBOARD / "pages").glob("*.py"):
        source = page.read_text(encoding="utf-8")
        compile(source, str(page), "exec")


def test_query_layer_is_read_only(tmp_path: Path, monkeypatch) -> None:
    """조회 계층은 쓰기를 할 수 없어야 한다.

    대시보드가 관측 대상을 바꾸면 그건 더 이상 관측이 아니다.
    """
    pytest.importorskip("streamlit")

    from argus.storage.hot import Database

    db_file = tmp_path / "t.db"
    Database(db_file).open().close()

    import argus.dashboard.data as data_module

    monkeypatch.setattr(data_module, "db_path", lambda: db_file)

    # 읽기는 된다
    assert data_module.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"] == 0

    # 쓰기는 막힌다
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO self_telemetry (ts) VALUES (1)")
    conn.close()


def test_missing_table_returns_empty(tmp_path: Path, monkeypatch) -> None:
    """없는 테이블을 물어도 화면 하나가 비는 데서 그쳐야 한다.

    마이그레이션 이전 DB 를 열었을 때 대시보드 전체가 죽으면, 정작 무엇이
    잘못됐는지 볼 방법이 사라진다.
    """
    pytest.importorskip("streamlit")

    from argus.storage.hot import Database

    db_file = tmp_path / "t.db"
    Database(db_file).open().close()

    import argus.dashboard.data as data_module

    monkeypatch.setattr(data_module, "db_path", lambda: db_file)
    assert data_module.query("SELECT * FROM table_that_does_not_exist") == []


@pytest.mark.parametrize(
    "page",
    [
        "app.py",
        "pages/1_Live.py",
        "pages/2_Timeline.py",
        "pages/3_Processes.py",
        "pages/4_Health.py",
        "pages/5_Incidents.py",
    ],
)
def test_pages_run_without_exception(page: str, tmp_path: Path, monkeypatch) -> None:
    """페이지를 **실제로 실행**해 예외가 없는지 본다.

    HTTP 200 은 증거가 되지 않는다 — Streamlit 은 SPA 라 페이지 스크립트가 터져도
    셸 HTML 은 정상으로 돌아온다. `AppTest` 는 스크립트를 헤드리스로 돌리고 예외를
    수집하므로, 마우스를 움직이지 않고도 렌더링 실패를 잡는다.

    빈 DB 로 돌리는 이유: 데이터가 없을 때가 가장 잘 깨진다(첫 실행 직후가 그렇다).
    """
    at = pytest.importorskip("streamlit.testing.v1", reason="streamlit 필요").AppTest
    pytest.importorskip("plotly")

    from argus.storage.hot import Database

    db_file = tmp_path / "argus.db"
    Database(db_file).open().close()
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))

    app = at.from_file(str(DASHBOARD / page), default_timeout=30)
    app.run()
    assert not app.exception, f"{page}: {[e.value for e in app.exception]}"


@pytest.mark.skipif(sys.platform != "win32", reason="개발 환경 기준 경로")
def test_streamlit_config_is_dark() -> None:
    """테마가 다크로 고정돼 있어야 한다.

    팔레트는 다크 서피스에서 전 항목 통과, 라이트에서는 3:1 미만 슬롯이 생긴다.
    설정이 바뀌면 검증하지 않은 색으로 그리게 된다.
    """
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'base = "dark"' in config
    assert "#3987e5" in config  # 검증된 계열 1번

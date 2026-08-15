"""조회 계층(`argus/dashboard/`) 검증.

**Streamlit 판을 지운 뒤(2026-08-09) 남은 것은 조회와 색뿐이다.** 그전까지 이 파일은
페이지를 헤드리스로 실행해 렌더링 예외를 잡았는데, 그 대상이 없어졌다. 창(PySide6)
쪽 검증은 `tests/test_desktop.py` 에 있다.

여기서 잡으려는 실패는 둘이다.
- **조회 계층이 쓰기를 시도하는 것** — 관측 대상을 바꾸면 그건 더 이상 관측이 아니다
- **없는 테이블 하나가 화면 전체를 죽이는 것** — 마이그레이션 이전 DB 를 열었을 때다
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_query_layer_needs_no_ui_framework() -> None:
    """**조회 계층은 자기를 그리는 것이 무엇인지 몰라야 한다.**

    2026-08-03 까지 캐시가 `st.cache_data` 라 `data.py` 가 Streamlit 없이는 import 도
    안 됐다. 그 상태로 UI 를 갈아 끼웠다면 조회 코드까지 따라 옮겨야 했을 것이고,
    지금 이 삭제도 그만큼 커졌을 것이다.
    """
    from argus.dashboard import data, theme

    for module in (data, theme):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for framework in ("import streamlit", "import plotly", "import PySide6"):
            assert framework not in source, f"{module.__name__} 이 {framework} 에 묶였다"


def test_theme_keeps_the_verified_palette() -> None:
    """색은 눈으로 고른 것이 아니라 검증기를 통과한 값이다(모듈 주석에 명령과 결과).

    슬롯이 줄거나 순서가 바뀌면 색각 분리 검증이 무효가 된다.
    """
    from argus.dashboard import theme

    assert theme.SERIES[0] == "#3987e5"
    assert len(theme.SERIES) == 4
    assert set(theme.STATUS) == {"good", "warning", "serious", "critical"}


def test_query_layer_is_read_only(tmp_path: Path, monkeypatch) -> None:
    """조회 계층은 쓰기를 할 수 없어야 한다."""
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

    마이그레이션 이전 DB 를 열었을 때 창 전체가 죽으면, 정작 무엇이 잘못됐는지
    볼 방법이 사라진다.
    """
    from argus.storage.hot import Database

    db_file = tmp_path / "t.db"
    Database(db_file).open().close()

    import argus.dashboard.data as data_module

    monkeypatch.setattr(data_module, "db_path", lambda: db_file)
    assert data_module.query("SELECT * FROM table_that_does_not_exist") == []


def test_incidents_carry_attributable(tmp_path: Path, monkeypatch) -> None:
    """`attributable` 은 저장되지 않는다 — 병목 종류에서 파생시켜 화면에 넘긴다.

    **배선 테스트다.** 화면이 이 필드를 못 받으면 `incident.get("attributable")` 이
    조용히 `None` 이 되어 **모든 사건이 "참고"로** 보인다. 예외가 안 나므로 실행만
    해서는 안 드러난다.

    막지 않았으면 무엇이 일어났을 것인가: 발열 사건의 CPU 상위 표가 "원인 후보"로
    발표된다(실측 `#59` — 1위가 관측자 자신인 `pythonw` 22%).
    """
    import time

    from argus.storage.hot import Database

    db_file = tmp_path / "t.db"
    db = Database(db_file).open()
    now = time.time()
    db.insert_many(
        "incidents",
        ("ts_start", "ts_end", "severity", "title", "bottleneck", "notified"),
        [
            (now - 60, now - 50, "info", "발열 스로틀링 — GPU 90°C", "THERMAL", 0),
            (now - 40, now - 30, "warning", "CPU 병목 — chrome 52%", "CPU", 1),
        ],
    )
    db.close()

    import argus.dashboard.data as data_module

    monkeypatch.setattr(data_module, "db_path", lambda: db_file)
    data_module.incidents.cache_clear()
    rows = {r["bottleneck"]: r for r in data_module.incidents(days=1.0)}
    data_module.incidents.cache_clear()

    assert rows["THERMAL"]["attributable"] is False, "발열의 CPU 상위를 원인으로 넘겼다"
    assert rows["CPU"]["attributable"] is True, "CPU 병목까지 참고로 낮췄다"


def test_attributable_reads_the_single_table() -> None:
    """판정은 `_RESOURCE_BY_KIND` 한 곳에서만 나온다.

    표를 복사하면 조용히 갈린다 — 2026-07-30 에 표만 고치고 다른 자리가 dataclass
    기본값을 쓰는 바람에 "병목 없음 — cpu_eater 100%" 가 계속 나왔다. **기본값이
    아닌 값으로 잰다**: 표를 뒤집었을 때 함수도 따라 뒤집혀야 한다.
    """
    from argus.explain import bottleneck as bn

    assert bn.is_attributable("THERMAL") is False
    assert bn.is_attributable("CPU") is True
    assert bn.is_attributable("cpu") is True, "종류 문자열의 대소문자에 걸리면 안 된다"
    assert bn.is_attributable(None) is False
    assert bn.is_attributable("WHAT_IS_THIS") is False, "모르는 종류는 겸손한 쪽으로"

    original = bn._RESOURCE_BY_KIND["CPU"]
    bn._RESOURCE_BY_KIND["CPU"] = ("cpu", False)
    try:
        assert bn.is_attributable("CPU") is False, "표를 안 읽고 자기 판단을 갖고 있다"
    finally:
        bn._RESOURCE_BY_KIND["CPU"] = original

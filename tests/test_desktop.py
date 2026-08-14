"""네이티브 창(PySide6)과 그 조회 계층.

**창을 띄우지 않는다.** 화면에 보이는지는 사람이 봐야 하고, 마우스를 움직이는
자동화는 쓰지 않는다(CLAUDE.md). 여기서 고정하는 것은 UI 없이 확인되는 것들이다 —
캐시 의미, 모니터 배치 규칙, 그리고 **조회 계층이 Streamlit 없이 도는가**.

마지막 항목이 이 파일의 핵심이다. `data.py` 가 UI 프레임워크에 묶여 있으면 창을
바꿀 때마다 조회 코드까지 따라 옮겨야 한다.
"""

from __future__ import annotations

import sys
import time

import pytest

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:  # PySide6 가 없으면 Qt 테스트는 건너뛴다
    QtCore = None
    QtWidgets = None


# ---------------------------------------------------------------- 조회 계층 독립성

def test_data_layer_does_not_require_streamlit() -> None:
    """**조회 계층은 UI 를 모른다.**

    2026-08-03 까지 캐시가 `st.cache_data` 라 `data.py` 가 Streamlit 없이는 import 조차
    되지 않았다. 네이티브 창으로 옮기는 순간 이 계층이 발목을 잡았을 것이다.
    """
    for module in [m for m in list(sys.modules) if m.startswith("streamlit")]:
        del sys.modules[module]

    import argus.dashboard.data  # noqa: F401

    assert "streamlit" not in sys.modules, "조회 계층이 Streamlit 을 끌어들인다"


# ---------------------------------------------------------------- TTL 캐시

def test_ttl_cache_reuses_within_window() -> None:
    from argus.dashboard.data import ttl_cache

    calls = []

    @ttl_cache(60.0)
    def fetch(n: int) -> int:
        calls.append(n)
        return n * 2

    assert fetch(1) == 2
    assert fetch(1) == 2
    assert calls == [1], "창 안인데 다시 조회했다"

    assert fetch(2) == 4
    assert calls == [1, 2], "인자가 다르면 따로 캐시해야 한다"


def test_ttl_cache_expires() -> None:
    """**시간 기반 만료가 없으면 대시보드가 영원히 옛 값을 보여준다.**

    `functools.lru_cache` 를 쓰지 않는 이유가 이것이다.
    """
    from argus.dashboard.data import ttl_cache

    calls = []

    @ttl_cache(0.05)
    def fetch() -> int:
        calls.append(1)
        return len(calls)

    assert fetch() == 1
    time.sleep(0.08)
    assert fetch() == 2, "TTL 이 지났는데 옛 값을 돌려줬다"


def test_realtime_ttl_matches_collection_period() -> None:
    """실시간 조회 캐시가 수집 주기(1초)보다 길면 갱신이 그만큼 느려진다.

    예광탄 실측: TTL 2초일 때 **12초에 6개**만 그렸다. 창은 1초마다 물었는데 캐시가
    절반을 옛 값으로 돌려준 것이다. 눈으로만 봤으면 "좀 굼뜬가" 로 넘어갔을 문제다.
    """
    from argus.dashboard import data

    assert data.latest_metrics.cache_ttl <= 1.0, (
        f"실시간 지표 캐시가 {data.latest_metrics.cache_ttl}초 — 1초 주기 갱신을 막는다"
    )
    assert data.latest_gpu.cache_ttl <= 1.0


# ---------------------------------------------------------------- 실시간 페이지
#
# **창을 띄우지 않는다.** `QT_QPA_PLATFORM=offscreen` 으로 위젯만 만들고 슬롯을 직접
# 부른다 — 화면도 마우스도 쓰지 않으면서 "무엇이 몇 번 불렸는가"는 전부 확인된다.


@pytest.fixture(scope="module")
def qapp():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


# **Qt 객체를 파이썬 GC 로부터 지킨다. 그리고 끝까지 놓지 않는다.**
#
# 테스트 함수가 끝나면 지역 변수인 위젯이 GC 대상이 되는데, 그 위젯이 `QThread` 를
# 물고 있으면 인터프리터가 통째로 죽는다(실측: pytest exit 9, 같은 코드가 단독
# 실행에서는 멀쩡했다).
#
# **세션 끝에 비우는 것도 안 된다** — 그때 한꺼번에 소멸하면서 같은 크래시가 났다.
# C++ 쪽 소멸을 프로세스 종료에 맡긴다. 테스트 프로세스는 곧 끝나므로 누수가 아니다.
_KEEP: list = []


def _keep(widget):
    _KEEP.append(widget)
    return widget


def _page(qapp):
    from argus.desktop.pages.realtime import RealtimePage

    page = RealtimePage()
    page.stop()  # 폴러를 멈추고 슬롯만 직접 부른다 (DB 를 건드리지 않는다)
    return _keep(page)


def test_backfill_is_not_counted_as_live_samples(qapp) -> None:
    """**백필과 실시간을 합쳐 세면 판정이 무너진다.**

    첫 측정에서 608개가 나왔는데 그중 600개가 백필이었다. "창을 열었다"와 "갱신이
    돈다"는 다른 사실인데 한 숫자에 섞여 있었고, 실시간이 8개뿐이라는 것이 그
    숫자에 가려졌다.
    """
    page = _page(qapp)
    rows = [
        {"ts": 1000.0 + i, "cpu_total": 20.0, "cpu_max_core": 40.0, "mem_percent": 50.0}
        for i in range(600)
    ]

    page._on_backfill({"metrics": rows, "gpu": []})
    assert page.backfill_count == 600
    assert page.sample_count == 0, "백필을 실시간 표본으로 셌다"

    page._on_sample({"metrics": {"ts": 1600.0, "cpu_total": 30.0}, "gpu": []})
    assert page.sample_count == 1


def test_repeated_timestamp_is_ignored(qapp) -> None:
    """수집이 멈추면 같은 행이 계속 온다. 다시 그리면 차트가 제자리에서 늘어난다."""
    page = _page(qapp)
    page._on_sample({"metrics": {"ts": 500.0, "cpu_total": 10.0}, "gpu": []})
    page._on_sample({"metrics": {"ts": 500.0, "cpu_total": 10.0}, "gpu": []})
    page._on_sample({"metrics": {"ts": 499.0, "cpu_total": 10.0}, "gpu": []})
    assert page.sample_count == 1, "같은(또는 더 오래된) 표본을 다시 세었다"


def test_missing_gpu_does_not_break_the_page(qapp) -> None:
    """GPU 가 없는 PC 에서도 나머지는 그려져야 한다(하드웨어를 가정하지 않는다)."""
    page = _page(qapp)
    page._on_sample({"metrics": {"ts": 1.0, "cpu_total": 5.0}, "gpu": []})
    assert page.sample_count == 1
    assert "없음" in page._tiles["gpu"]._value.text()


# ---------------------------------------------------------------- 표 위젯
#
# 프로세스와 사건이 함께 쓴다. 여기서 깨지면 두 페이지가 같이 깨진다.


def test_column_formats_and_handles_missing(qapp) -> None:
    from argus.desktop.widgets import Column

    cpu = Column("cpu", "CPU", fmt=".2f", suffix="%")
    assert cpu.display({"cpu": 12.345}) == "12.35%"
    assert cpu.display({"cpu": None}) == "—", "값이 없을 때 0 으로 보이면 안 된다"
    assert cpu.display({}) == "—"

    name = Column("name", "이름")
    assert name.display({"name": "chrome"}) == "chrome"


def test_table_preserves_query_order_by_default(qapp) -> None:
    """**표는 조회가 준 순서를 지켜야 한다.**

    `setSortingEnabled(True)` 가 기본 인디케이터(0번 열 내림차순)로 즉시 정렬을 걸어,
    프로세스 표가 CPU 순이 아니라 **이름 역순**으로 뜨고 있었다. 조회는 이미
    `ORDER BY cpu DESC` 로 오는데 표가 그걸 뒤집은 것이다.
    """
    from argus.desktop.widgets import Column, DataTable

    table = _keep(DataTable([Column("name", "이름"), Column("cpu", "CPU", fmt=".1f")]))
    # **이름이 오름차순인 데이터를 쓴다.** 기본 정렬(0번 열 내림차순)이 걸리면 순서가
    # 뒤집히므로 차이가 드러난다. 처음에는 이름이 이미 내림차순인 데이터를 써서,
    # 규칙을 지워도 결과가 같아 아무것도 검증하지 못했다.
    table.set_rows([{"name": "a", "cpu": 9.0}, {"name": "b", "cpu": 1.0}])

    order = [table.model().index(i, 0).data() for i in range(table.model().rowCount())]
    assert order == ["a", "b"], f"조회 순서를 뒤집었다: {order}"


def test_table_sorts_by_underlying_value_not_text(qapp) -> None:
    """**표시 문자열로 정렬하면 9 MB 가 10 MB 뒤에 온다.**

    사용량 표에서 그건 치명적이다 — 가장 많이 쓰는 프로그램을 찾으려고 정렬하는데
    답이 틀린다.
    """
    from PySide6 import QtCore

    from argus.desktop.widgets import Column, DataTable

    table = _keep(DataTable([Column("name", "이름"), Column("rss", "메모리", fmt=",.0f")]))
    table.set_rows(
        [{"name": "a", "rss": 9.0}, {"name": "b", "rss": 10.0}, {"name": "c", "rss": 100.0}]
    )
    table.sortByColumn(1, QtCore.Qt.AscendingOrder)

    order = []
    for row_index in range(table.model().rowCount()):
        order.append(table.model().index(row_index, 0).data())
    assert order == ["a", "b", "c"], f"숫자가 사전순으로 정렬됐다: {order}"


def test_table_keeps_selection_across_refresh(qapp) -> None:
    """**5초마다 갱신되는 표에서 선택이 풀리면** 보고 있던 프로그램의 상세가 사라진다."""
    from argus.desktop.widgets import Column, DataTable

    table = _keep(DataTable([Column("name", "이름"), Column("cpu", "CPU", fmt=".1f")]))
    table.set_rows([{"name": "a", "cpu": 1.0}, {"name": "b", "cpu": 2.0}])
    table.selectRow(1)
    assert table.selected_row()["name"] == "b"

    # 값이 바뀌고 순서도 바뀐 새 스냅샷
    table.set_rows([{"name": "b", "cpu": 9.0}, {"name": "a", "cpu": 1.0}])
    kept = table.selected_row()
    assert kept is not None and kept["name"] == "b", "갱신 후 선택이 풀렸다"


# ---------------------------------------------------------------- 프로세스 페이지


def _process_page(qapp):
    from argus.desktop.pages.processes import ProcessPage

    page = ProcessPage()
    page.stop()
    return _keep(page)


def test_process_page_fills_table_and_ranking(qapp) -> None:
    page = _process_page(qapp)
    page._on_rows(
        [
            {"name": "chrome", "pids": 12, "cpu": 30.0, "cpu_max": 60.0, "rss": 2000.0,
             "handles": 4000, "foreground": 1},
            {"name": "idle-app", "pids": 1, "cpu": 0.0, "cpu_max": 0.0, "rss": 10.0,
             "handles": 100, "foreground": 0},
        ]
    )
    assert page.load_count == 1
    assert page._table.model().rowCount() == 2
    assert "chrome" in page._foreground.text(), "포어그라운드를 표시하지 않았다"


def test_stale_series_result_is_discarded(qapp) -> None:
    """**늦게 도착한 이전 선택의 결과를 그리면 안 된다.**

    표를 빠르게 클릭하면 조회가 순서대로 끝나지 않는다. 그때 지난 결과를 그리면
    선택한 것과 다른 프로그램의 차트가 보인다.
    """
    page = _process_page(qapp)
    page._selected = "chrome"
    page._on_series("firefox", [{"ts": 1.0, "cpu": 5.0, "rss": 10.0, "handles": 1}])
    assert page._growth.text() == "", "이전 선택의 결과를 반영했다"


def test_growth_text_stays_quiet_on_short_windows(qapp) -> None:
    """관측 구간이 짧으면 증가율은 잡음이다 — 아예 말하지 않는다."""
    from argus.desktop.pages.processes import _growth_text

    short = [{"ts": 0.0, "rss": 100.0}, {"ts": 60.0, "rss": 200.0}]  # 1분
    assert _growth_text(short) == ""

    long = [{"ts": 0.0, "rss": 100.0}, {"ts": 3600.0, "rss": 200.0}]  # 1시간
    assert "+100.0 MB/시간" in _growth_text(long)


# ---------------------------------------------------------------- 사건 페이지


def test_feedback_clears_the_incident_cache(monkeypatch, tmp_path) -> None:
    """**피드백을 저장하면 캐시를 비워야 한다 — 그리고 그 이름이 맞아야 한다.**

    `st.cache_data` 시절에는 `.clear()` 였는데 `ttl_cache` 로 바꾸면서 `cache_clear`
    가 됐다. 호출부를 같이 고치지 않아 피드백 버튼이 `AttributeError` 로 죽는 상태였다
    (2026-08-03, 사건 페이지 이식 중 발견).

    **`cache_clear` 가 있는지만 보면 안 된다.** 처음에 그렇게 썼다가 mutation 에서
    "안 잡힘"이 나왔다 — 존재 여부는 호출 여부를 말해 주지 않는다. 비우지 않으면
    방금 남긴 피드백이 최대 10초 동안 화면에 반영되지 않는다.
    """
    import sqlite3

    from argus.dashboard import data

    database = tmp_path / "t.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY, user_label TEXT, labeled_at REAL)"
        )
        conn.execute("INSERT INTO incidents (id) VALUES (1)")

    calls: list[int] = []
    monkeypatch.setattr(data, "db_path", lambda: database)
    monkeypatch.setattr(data.incidents, "cache_clear", lambda: calls.append(1))

    data.set_user_label(1, "normal")

    assert calls, "피드백을 저장하고도 캐시를 비우지 않았다"
    with sqlite3.connect(database) as conn:
        stored = conn.execute("SELECT user_label FROM incidents WHERE id = 1").fetchone()[0]
    assert stored == "normal", "라벨이 저장되지 않았다"


def _incident_page(qapp):
    from argus.desktop.pages.incidents import IncidentPage

    page = IncidentPage()
    page.stop()
    return _keep(page)


def test_incident_list_row_formats_duration() -> None:
    from argus.desktop.pages.incidents import _list_row

    closed = _list_row({"id": 1, "ts_start": 1000.0, "ts_end": 1180.0,
                        "severity": "warning", "title": "디스크 병목"})
    assert closed["span"] == "3분"
    assert closed["severity_ko"] == "경고"

    short = _list_row({"id": 2, "ts_start": 1000.0, "ts_end": 1040.0,
                       "severity": "info", "title": "짧은 사건"})
    assert short["span"] == "40초", "1분 미만은 초로 보여야 읽힌다"

    ongoing = _list_row({"id": 3, "ts_start": 1000.0, "ts_end": None,
                         "severity": "critical", "title": "진행 중"})
    assert ongoing["span"] == "진행 중"


def test_contributor_hides_lead_for_minor_shares() -> None:
    """**기여가 작으면 선행 시간을 붙이지 않는다.**

    상승폭이 작으면 "오르기 시작한 시점"이 잡음에 좌우된다 — 실측에서 기여도 5% 짜리가
    "255초 선행"으로 나왔다. 용의자가 아닌 것의 선행성은 잡음이다.
    """
    from argus.desktop.pages.incidents import _contributor_row

    major = _contributor_row({"name": "chrome", "share": 0.68, "delta": 300.0,
                              "pids": [1, 2], "lead_s": 40.0})
    assert major["lead"] == "40초"
    assert major["share_pct"] == pytest.approx(68.0)
    assert major["pid_count"] == 2

    minor = _contributor_row({"name": "noise", "share": 0.05, "delta": 3.0,
                              "pids": [9], "lead_s": 255.0})
    assert minor["lead"] == "", "기여도 5% 인데 선행 시간을 붙였다"


def test_incident_detail_renders_markdown_report(qapp) -> None:
    """리포트가 이미 마크다운이라 그대로 그린다 — 이식 전 유일한 미지수였다."""
    page = _incident_page(qapp)
    page._rows = [
        {
            "id": 7,
            "ts_start": 1000.0,
            "ts_end": 1200.0,
            "severity": "warning",
            "title": "디스크 병목 — chrome 68%",
            "explanation_md": "## 무슨 일이\n\n디스크 응답이 **8ms → 71ms** 로 올랐다.",
            "contributors": '[{"name": "chrome", "share": 0.68, "delta": 300.0, "pids": [1]}]',
            "detectors": '["rules"]',
            "signal_count": 12,
        }
    ]
    page._render_detail(page._rows[0])

    text = page._report.toPlainText()
    assert "디스크 응답이" in text
    assert "**" not in text, "마크다운이 그대로 문자로 남았다 — 렌더링되지 않았다"
    assert page._contributors.model().rowCount() == 1
    assert page._normal_box.isEnabled(), "사건을 골랐는데 피드백을 못 준다"


def test_empty_incident_list_explains_why(qapp) -> None:
    """**빈 화면 대신 왜 비었는지 말한다.** 사건이 없는 것은 대개 정상이다."""
    page = _incident_page(qapp)
    page._on_rows([])
    # `isVisible()` 은 부모 창이 `show()` 되어야 True 다 — 창을 띄우지 않는 테스트에서는
    # 항상 False 라 아무것도 검증하지 못한다. 명시적 숨김 여부를 본다.
    assert not page._notice.isHidden(), "사건이 없는데 안내를 숨겼다"
    assert "정상" in page._notice.text()

    page._on_rows([{"id": 1, "ts_start": 1000.0, "ts_end": 1100.0,
                    "severity": "info", "title": "무언가"}])
    assert page._notice.isHidden(), "사건이 있는데 안내가 남았다"


# ---------------------------------------------------------------- 타임라인


def _timeline_page(qapp):
    from argus.desktop.pages.timeline import TimelinePage

    page = TimelinePage()
    page.stop()
    return _keep(page)


def test_overlays_draw_bands_and_marks(qapp) -> None:
    """**주입 구간과 탐지 신호를 지표 위에 겹쳐야** "무엇을 넣었고 무엇이 울렸나"가 보인다."""
    from argus.desktop.widgets import HistoryChart

    chart = _keep(HistoryChart("t", ["a"]))
    chart.set_overlays(
        [{"lo": 100.0, "hi": 200.0, "strong": True}, {"lo": 300.0, "hi": 400.0, "strong": False}],
        [150.0, 350.0, 500.0],
    )
    assert chart.overlay_count == 5, "밴드 2 + 신호선 3 이어야 한다"

    # 다시 그리면 이전 것이 남지 않아야 한다 — 구간을 바꿀 때마다 쌓이면 화면이 덮인다.
    chart.set_overlays([], [])
    assert chart.overlay_count == 0, "오버레이가 누적됐다"


def test_incomplete_injection_is_drawn_faintly(qapp) -> None:
    """**증상이 관측되지 않은 주입은 흐리게.**

    채점에서 빠지는 구간인데 같은 진하기로 그리면 "탐지 실패"처럼 보인다.
    """
    import pyqtgraph as pg

    from argus.desktop.widgets import HistoryChart

    chart = _keep(HistoryChart("t", ["a"]))
    chart.set_overlays(
        [{"lo": 0.0, "hi": 10.0, "strong": True}, {"lo": 20.0, "hi": 30.0, "strong": False}], []
    )
    regions = [o for o in chart._overlays if isinstance(o, pg.LinearRegionItem)]
    alphas = [r.brush.color().alphaF() for r in regions]
    assert alphas[0] > alphas[1], f"증상 없는 주입이 더 진하거나 같다: {alphas}"


def test_timeline_foreground_share_sums_to_hundred(qapp) -> None:
    """비중은 관측된 분(分)의 비율이다 — 이 표가 레짐 추론의 입력이 된다."""
    page = _timeline_page(qapp)
    page._fill_foreground(
        [{"foreground_proc": "chrome"}] * 3
        + [{"foreground_proc": "code"}]
        + [{"foreground_proc": None}]  # 기록 없는 분은 세지 않는다
    )
    model = page._foreground.model()
    assert model.rowCount() == 2
    shares = [model.index(i, 2).data() for i in range(model.rowCount())]
    assert shares[0] == "75%", f"비중 계산이 틀렸다: {shares}"


def test_timeline_marks_unfinished_injection(qapp) -> None:
    page = _timeline_page(qapp)
    page._fill_faults(
        [
            {"scenario": "handle_leak", "ts_start": 1000.0, "ts_end": 1720.0, "completed": 1},
            {"scenario": "cpu_spin", "ts_start": 2000.0, "ts_end": None, "completed": 0},
        ]
    )
    model = page._faults.model()
    rows = [
        {model.headerData(c, QtCore.Qt.Horizontal): model.index(r, c).data() for c in range(4)}
        for r in range(model.rowCount())
    ]
    lengths = [r["길이"] for r in rows]
    assert "미완" in lengths, f"끝나지 않은 주입을 표시하지 않았다: {lengths}"
    observed = [r["증상 관측"] for r in rows]
    assert any("채점 제외" in o for o in observed), observed


def test_timeline_empty_explains_rollup_delay(qapp) -> None:
    page = _timeline_page(qapp)
    page._on_loaded({"rows": [], "faults": [], "signals": []})
    assert not page._notice.isHidden()
    assert "롤업" in page._notice.text(), "왜 비었는지 말하지 않았다"


# ---------------------------------------------------------------- 자기 상태


def _selfstate_page(qapp):
    from argus.desktop.pages.selfstate import SelfStatePage

    page = SelfStatePage()
    page.stop()
    return _keep(page)


def test_growth_is_measured_on_private_not_rss() -> None:
    """**누수 판정의 정본은 `private` 이다.**

    RSS 는 Windows 의 워킹셋 트림에 따라 내려가 누수를 가린다 — 2026-07-27 에 RSS 가
    63 → 18MB 로 내려간 것이 반납이 아니라 트림이었다. 여기서도 RSS 는 줄고 private 은
    느는 상황을 준다. RSS 로 재면 음수가 나온다.
    """
    from argus.desktop.pages.selfstate import _private_growth

    rows = [
        {"ts": 0.0, "private_mb": 100.0, "rss_mb": 200.0},
        {"ts": 3600.0, "private_mb": 110.0, "rss_mb": 50.0},
    ]
    text = _private_growth(rows)
    assert "+10.00 MB/시간" in text, f"private 기준이 아니다: {text}"


def test_growth_stays_quiet_on_short_windows() -> None:
    from argus.desktop.pages.selfstate import _private_growth

    assert _private_growth([{"ts": 0.0, "private_mb": 100.0},
                            {"ts": 300.0, "private_mb": 200.0}]) == ""
    assert _private_growth([{"ts": 0.0, "rss_mb": 100.0}]) == "", "private 이 없으면 침묵"


def test_selfstate_alerts_on_drops_and_throttle(qapp) -> None:
    """**규칙 1 이 깨지고 있다는 신호만 띄운다.** 평소에는 조용해야 한다."""
    page = _selfstate_page(qapp)

    quiet = [{"ts": 1.0, "cpu_percent": 0.2, "rss_mb": 70.0, "private_mb": 71.0,
              "drop_count": 0, "handles": 400, "throttle_level": 0}]
    page._on_loaded({"rows": quiet, "storage": {}})
    # `isVisible()` 은 창을 띄우지 않으면 항상 False 라 아무것도 검증하지 못한다.
    assert page._alert.isHidden() and page._alert.text() == "", "평소인데 경고를 띄웠다"

    noisy = [
        {"ts": 1.0, "cpu_percent": 3.0, "rss_mb": 320.0, "private_mb": 330.0,
         "drop_count": 0, "handles": 900, "throttle_level": 2},
        {"ts": 2.0, "cpu_percent": 3.1, "rss_mb": 330.0, "private_mb": 340.0,
         "drop_count": 152, "handles": 950, "throttle_level": 1},
    ]
    page._on_loaded({"rows": noisy, "storage": {}})
    assert not page._alert.isHidden()
    assert "유실" in page._alert.text() and "스로틀" in page._alert.text()


def test_selfstate_shows_cpu_against_budget(qapp) -> None:
    """예산(2%) 대비 몇 %인지를 함께 보여 준다 — 절대값만으로는 판단이 안 선다."""
    page = _selfstate_page(qapp)
    page._on_loaded(
        {
            "rows": [{"ts": 1.0, "cpu_percent": 1.0, "rss_mb": 80.0, "private_mb": 80.0,
                      "drop_count": 0, "handles": 400, "throttle_level": 0}],
            "storage": {},
        }
    )
    assert "50%" in page._tiles["cpu"]._note.text(), page._tiles["cpu"]._note.text()


def test_system_event_cause_is_translated(qapp) -> None:
    """**사건 이름만으로는 "왜"가 안 보인다.**

    절전인지 재부팅인지 강제 종료인지가 사후 진단의 전부다. `detail` 안의 추정 원인을
    끌어올리고, 못 읽으면 조용히 빈 칸으로 둔다 — 진단 보조 하나 때문에 표 전체가
    비면 안 된다.
    """
    from argus.desktop.pages.selfstate import _cause_ko

    assert _cause_ko('{"likely_cause": "reboot_or_power_loss"}') == "재부팅·전원 차단"
    assert _cause_ko('{"likely_cause": "suspend_or_stall"}') == "절전·정지"
    assert _cause_ko("깨진 json") == ""
    assert _cause_ko(None) == ""
    # 모르는 원인은 원문이라도 보여 준다 — 번역표에 없다고 숨기면 진단이 막힌다.
    assert _cause_ko('{"likely_cause": "새로운_원인"}') == "새로운_원인"


def test_selfstate_fills_events_and_scoreboard(qapp) -> None:
    page = _selfstate_page(qapp)
    page._on_loaded(
        {
            "rows": [],
            "storage": {},
            "events": [
                {"ts": 1000.0, "event": "unclean_shutdown", "gap_seconds": 120.0,
                 "detail": '{"likely_cause": "process_killed_or_crash"}'}
            ],
            "runs": [
                {"ts": 2000.0, "detector": "procleak", "f1": 0.889,
                 "precision_pct": 80.0, "recall_pct": 100.0, "fp_per_hour": 0.32}
            ],
        }
    )
    assert page._events.model().rowCount() == 1
    assert page._runs.model().rowCount() == 1
    assert page._events.model().index(0, 3).data() == "강제 종료·크래시"


def test_missing_rollup_state_is_surfaced(qapp) -> None:
    """**롤업이 멈추면 원본 정리도 함께 멈춘다.** 그 사실이 화면에 드러나야 한다."""
    page = _selfstate_page(qapp)
    page._fill_storage({"db_bytes": 1048576, "tables": []})
    assert page._storage_tiles["lag"]._value.text() == "—"
    assert "실행되지 않음" in page._storage_tiles["lag"]._note.text()


# ---------------------------------------------------------------- 모니터 배치

@pytest.mark.parametrize(
    "value,expected_fragment",
    [("", "기본 위치"), ("abc", "못 읽었다"), ("99", "없음")],
)
def test_screen_placement_falls_back_quietly(monkeypatch, value, expected_fragment) -> None:
    """**개발 편의 기능이 실행을 막으면 안 된다.**

    지정이 없거나 그런 모니터가 없으면 기본 위치로 간다. 예외를 던지면 창이 아예
    안 뜬다.
    """
    pytest.importorskip("PySide6")
    from argus.desktop.app import ENV_SCREEN, place_on_configured_screen

    monkeypatch.setenv(ENV_SCREEN, value)

    class _FakeWindow:
        def rect(self):
            return None

        def move(self, *_args):
            raise AssertionError("기본 위치여야 하는데 창을 옮겼다")

    result = place_on_configured_screen(_FakeWindow())
    assert expected_fragment in result, result


# ---------------------------------------------------------------- 설정 페이지

def _settings_page(qapp, tmp_path):
    """실사용 `runtime.yaml` 을 건드리지 않도록 경로를 갈아 끼운 설정 페이지."""
    from argus.desktop.pages import settings as settings_mod
    from argus.runtime.livecfg import LiveConfig

    page = settings_mod.SettingsPage()
    page._timer.stop()  # 폴링은 직접 부른다 — 테스트가 시계에 기대면 안 된다
    page._live = LiveConfig(path=tmp_path / "runtime.yaml", defaults={"notify": True})
    page._sync_from_file()
    return _keep(page)


def test_settings_toggle_writes_the_file(qapp, tmp_path) -> None:
    """**창은 별도 프로세스라 파일이 유일한 통로다.**

    메모리만 바꾸면 상주는 영영 모른다 — 화면에서는 꺼졌는데 알림은 계속 온다.
    """
    page = _settings_page(qapp, tmp_path)
    assert page.notify_box.isChecked() is True

    page.notify_box.setChecked(False)

    assert page._live.path.exists(), "체크를 껐는데 파일에 남지 않았다"
    from argus.runtime.livecfg import LiveConfig

    assert LiveConfig(path=page._live.path, defaults={"notify": True}).notify_enabled is False


def test_settings_follows_the_tray(qapp, tmp_path) -> None:
    """**트레이에서 바꾼 것을 창이 따라와야 한다.**

    한쪽만 하면 두 화면이 서로 다른 값을 보여 주고, 그때 사용자는 무엇이 참인지
    알 수 없다.
    """
    page = _settings_page(qapp, tmp_path)
    from argus.runtime.livecfg import LiveConfig

    LiveConfig(path=page._live.path, defaults={"notify": True}).set("notify", False)
    page._sync_from_file()

    assert page.notify_box.isChecked() is False, "트레이에서 끈 것이 창에 반영되지 않았다"


def test_settings_sync_does_not_rewrite_the_file(qapp, tmp_path) -> None:
    """파일 갱신으로 체크박스를 바꿀 때 **되받아 쓰지 않는다.**

    두 프로세스가 서로의 쓰기에 반응하면 값이 진동한다. 조용히 깨지는 종류다 —
    동작은 하는데 파일이 계속 갱신된다.
    """
    page = _settings_page(qapp, tmp_path)
    from argus.runtime.livecfg import LiveConfig

    LiveConfig(path=page._live.path, defaults={"notify": True}).set("notify", False)
    page._sync_from_file()
    stamp = page._live.path.stat().st_mtime_ns

    page._sync_from_file()
    page._sync_from_file()

    assert page._live.path.stat().st_mtime_ns == stamp, "동기화가 파일을 다시 썼다"


def test_settings_reveals_which_source_won(qapp, tmp_path) -> None:
    """UI 값이 `settings.yaml` 을 이기므로, 고쳤는데 안 먹는 이유가 보여야 한다(규칙 4)."""
    page = _settings_page(qapp, tmp_path)
    assert "settings.yaml" in page.source_label.text()

    page.notify_box.setChecked(False)
    assert "여기서 바꾼 것" in page.source_label.text()


# ---------------------------------------------------------------- 레이아웃
#
# **여기 있는 것은 전부 눈으로 먼저 잡은 결함이다.** 2026-08-06 에 페이지를 그림으로
# 뽑아 보니 차트가 55px 로 눌리고, 범례가 데이터를 덮고, 표가 헤더와 한 줄만 남고,
# 한 행이 다른 행의 세 배가 되어 있었다. 전부 **예외 없이 조용히** 그렇게 됐다 —
# 갱신 표본 수(`--seconds`)는 그 상태에서도 전부 정상이었다.
#
# 그림은 사람이 봐야 하지만, 한 번 본 것을 다시 안 보게 하는 것은 테스트의 몫이다.

#: 읽을 수 있다고 볼 최소 높이(px). **`MIN_PLOT_HEIGHT` 를 가져다 쓰지 않는다** —
#: 기댓값을 검증 대상에서 가져오면 그 상수를 1 로 바꿔도 양쪽이 함께 1 이 되어 통과한다.
#: 2026-08-06 mutation 에서 실제로 그랬다(6개 중 이것만 안 잡혔다). 값의 근거는 실측이다:
#: 눌렸을 때가 55px 였고 그 상태로는 축 눈금과 선을 구분할 수 없었다.
READABLE_PLOT_PX = 120


def test_charts_keep_a_readable_minimum_height(qapp) -> None:
    """차트가 읽을 수 없을 만큼 눌리지 않는다."""
    from argus.desktop.widgets import HistoryChart, TimeSeriesChart

    live = _keep(TimeSeriesChart("t", ["a", "b"]))
    assert live._plot.minimumHeight() >= READABLE_PLOT_PX

    # 두 차트가 같은 기본값을 공유한다. 한쪽만 재면 다른 쪽이 바뀌어도 조용하다.
    history = _keep(HistoryChart("t", ["a"]))
    assert history._plot.minimumHeight() >= READABLE_PLOT_PX


def test_legend_lives_outside_the_plot(qapp) -> None:
    """**범례는 플롯 안에 없다.**

    안에 두면 차트가 작아질 때 데이터 위로 올라오고, 하필 그때가 가장 읽기 어렵다.
    """
    from argus.desktop.widgets import TimeSeriesChart

    chart = _keep(TimeSeriesChart("t", ["아무개", "다른것"]))
    assert chart._plot.plotItem.legend is None, "범례가 플롯 안에 있다"

    # 대신 제목 줄에 이름이 있어야 한다 — 없으면 계열을 구분할 방법이 사라진다.
    texts = {w.text() for w in chart.findChildren(QtWidgets.QLabel)}
    assert {"아무개", "다른것"} <= texts, f"제목 줄에 계열 이름이 없다: {texts}"


def test_grid_rows_share_height_even_with_a_note(qapp) -> None:
    """**주석이 붙은 차트가 한 행에만 있어도 두 행은 같은 높이다.**

    `wordWrap` 라벨은 `heightForWidth` 를 갖는데, 그런 위젯이 `QGridLayout` 의 한
    행에만 있으면 Qt 가 `setRowStretch` 를 사실상 무시한다. 실측: 주석이 있으면
    518 대 154, 없으면 336 대 336 이었다. **그림으로 보기 전에는 아무 신호가 없다.**
    """
    from argus.desktop.widgets import TimeSeriesChart

    host = _keep(QtWidgets.QWidget())
    grid = QtWidgets.QGridLayout(host)
    top_left = TimeSeriesChart("A", ["x"])
    top_right = TimeSeriesChart("B", ["x"], note="주석이 붙은 쪽")
    bottom_left = TimeSeriesChart("C", ["x"])
    bottom_right = TimeSeriesChart("D", ["x"])
    grid.addWidget(top_left, 0, 0)
    grid.addWidget(top_right, 0, 1)
    grid.addWidget(bottom_left, 1, 0)
    grid.addWidget(bottom_right, 1, 1)
    grid.setRowStretch(0, 1)
    grid.setRowStretch(1, 1)

    host.resize(1200, 700)
    host.show()
    qapp.processEvents()
    qapp.processEvents()

    top, bottom = top_left.height(), bottom_left.height()
    host.hide()
    assert abs(top - bottom) <= 8, f"행 높이가 갈렸다: 위 {top}, 아래 {bottom}"


def test_stat_tile_does_not_grow_vertically(qapp) -> None:
    """타일은 글자 세 줄이 전부다. 남는 공간을 받아 부풀면 화면 절반을 먹는다."""
    from argus.desktop.widgets import StatTile

    tile = _keep(StatTile("CPU"))
    assert tile.sizePolicy().verticalPolicy() == QtWidgets.QSizePolicy.Fixed


def test_table_shows_several_rows(qapp) -> None:
    """**표가 헤더와 한 줄만 남지 않는다.**

    최소 높이가 없으면 레이아웃이 남는 공간을 다른 위젯에 주고 표를 끝까지 누른다.
    그러면 표가 있다는 사실만 보이고 내용은 스크롤해야 한다.
    """
    from argus.desktop.widgets import Column, DataTable

    table = _keep(DataTable([Column("a", "가"), Column("b", "나")], min_rows=5))
    header = table.horizontalHeader().sizeHint().height()
    row = max(24, table.verticalHeader().defaultSectionSize())
    assert table.minimumHeight() >= header + row * 5


def test_table_max_rows_never_undercuts_min_rows(qapp) -> None:
    """상한이 하한보다 작으면 마지막 행이 반쯤 잘린다 — 실제로 그랬다(170px vs 5행)."""
    from argus.desktop.widgets import Column, DataTable

    table = _keep(DataTable([Column("a", "가")], min_rows=5, max_rows=2))
    assert table.maximumHeight() >= table.minimumHeight()


#: 창이 열려야 하는 크기(px). **`_WANTED_H` 를 가져다 쓰지 않는다** — 기댓값을 검증
#: 대상에서 가져오면 그 상수를 무엇으로 바꿔도 양쪽이 함께 바뀌어 통과한다
#: (`READABLE_PLOT_PX` 와 같은 이유). HD 는 사용자가 정한 값이므로 여기 박아 둔다.
HD_HEIGHT_PX = 720


def test_no_single_page_dictates_the_window_minimum_height(qapp) -> None:
    """**한 페이지가 창 전체의 하한을 정하지 못한다.**

    `QStackedWidget` 은 담긴 페이지 전부의 최소 높이 중 최댓값을 자기 최소 높이로
    삼는다. 스크롤 영역이 없으면 가장 빽빽한 페이지가 그대로 창의 하한이 되고,
    나머지 페이지까지 그 크기를 끌고 다닌다 — 2026-08-12 실측: 자기 상태 페이지의
    1179px 때문에 `resize(1280, 720)` 이 무시되고 창이 1280x1255 로 떴다.
    **예외도 경고도 없었다.** 크기를 요청했는데 안 먹는 것뿐이라 `--seconds` 스모크는
    그 상태에서도 전부 정상이었다.
    """
    from argus.desktop.app import MainWindow

    window = _keep(MainWindow())
    try:
        window.resize(1280, HD_HEIGHT_PX)
        window.show()
        qapp.processEvents()
        qapp.processEvents()
        height = window.height()
        hint = window.minimumSizeHint().height()
        window.hide()
    finally:
        # **페이지를 손으로 나열하지 않는다.** 탭이 늘면 이 목록이 조용히 뒤처지고,
        # 남은 QThread 가 파괴되면서 프로세스가 죽는다 — 테스트는 전부 통과한 채
        # 종료 코드만 비0 이 되므로 원인이 보이지 않는다(2026-08-13).
        window.stop_all()

    assert height == HD_HEIGHT_PX, f"{HD_HEIGHT_PX}px 를 요청했는데 {height}px 로 열렸다"
    assert hint <= HD_HEIGHT_PX, f"창 최소 높이가 {hint}px — 어떤 페이지가 하한을 밀고 있다"


def test_nav_sections_do_not_shift_the_pages(qapp) -> None:
    """**구분 머리가 들어가면 탭 행 번호와 페이지 인덱스가 어긋난다.**

    행 번호를 그대로 `setCurrentIndex` 에 넘기면 머리글 개수만큼 밀린 페이지가
    뜬다 — 예외도 빈 화면도 없이 **그냥 다른 페이지가 보인다.** 머리글 자체도
    선택되면 안 된다(고를 수 있으면 눌러서 빈 화면에 닿는다).
    """
    from argus.desktop.app import MainWindow

    window = _keep(MainWindow())
    try:
        nav = window._nav
        # 머리글은 고를 수 없다 — Qt 가 키보드 이동에서도 알아서 건너뛴다.
        headers = [i for i in range(nav.count())
                   if nav.item(i).flags() == QtCore.Qt.NoItemFlags]
        assert headers, "구분 머리가 하나도 없다"

        for page in (window.realtime, window.processes, window.incidents,
                     window.usage, window.timeline, window.selfstate, window.settings):
            nav.setCurrentRow(window._nav_row_of[page])
            shown = window._stack.currentWidget()
            inner = shown.widget() if isinstance(shown, QtWidgets.QScrollArea) else shown
            assert inner is page, (
                f"{nav.currentItem().text()} 를 골랐는데 다른 페이지가 떴다"
            )
    finally:
        # **페이지를 손으로 나열하지 않는다.** 탭이 늘면 이 목록이 조용히 뒤처지고,
        # 남은 QThread 가 파괴되면서 프로세스가 죽는다 — 테스트는 전부 통과한 채
        # 종료 코드만 비0 이 되므로 원인이 보이지 않는다(2026-08-13).
        window.stop_all()


def test_every_tab_gets_stopped(qapp) -> None:
    """**등록된 탭은 전부 멈춘다 — 목록을 손으로 유지하지 않는다.**

    2026-08-13 에 일일 리포트 탭을 붙였는데 종료 경로의 페이지 목록이 그대로여서,
    그 탭의 `QThread` 가 살아 있는 채로 파괴되며 프로세스가 죽었다(0xC0000409).
    **증상이 지독하다**: 461개가 전부 통과한 뒤 종료 코드만 비0 이라 실패한 테스트가
    하나도 없고, mutation sweep 은 "무력화 전부터 테스트가 실패한다"며 기준선에서
    멈춘다 — 코드가 아니라 종료 경로가 문제인데 그 사실이 어디에도 안 보인다.

    그래서 `stop_all()` 이 **탭 목록에서** 도는지를 잰다. 이름을 나열하는 구현으로
    되돌리면 이 테스트가 새 탭을 잡아낸다.
    """
    from argus.desktop.app import MainWindow

    window = _keep(MainWindow())
    try:
        tabs = set(window._nav_row_of)
        registered = set(window._pages)
        assert tabs <= registered, (
            f"탭에는 있는데 정리 목록에 없는 페이지: {[type(p).__name__ for p in tabs - registered]}"
        )

        window.stop_all()
        still_running = [
            type(page).__name__
            for page in window._pages
            if getattr(getattr(page, "_poller", None), "isRunning", bool)()
        ]
        assert not still_running, f"멈추지 않은 폴러: {still_running}"
    finally:
        window.stop_all()


def test_collapsible_body_leaves_the_minimum_height(qapp) -> None:
    """**접힌 것은 최소 높이에서 빠져야 한다.**

    `setMaximumHeight(0)` 으로 눌러 접는 흔한 구현은 위젯이 살아 있어 최소 높이에
    계속 잡힌다. 그러면 화면에서는 접혔는데 페이지가 요구하는 높이는 그대로라,
    접는 목적(창을 작게 열기) 자체가 사라진다 — 보기에는 멀쩡해서 안 잡힌다.
    """
    from argus.desktop.widgets import Collapsible

    body = QtWidgets.QWidget()
    body.setMinimumHeight(400)  # 접기 전후 차이가 드러날 만큼 큰 내용
    host = _keep(QtWidgets.QWidget())
    box = QtWidgets.QVBoxLayout(host)
    panel = Collapsible("자세히", body)
    box.addWidget(panel)

    collapsed = host.minimumSizeHint().height()
    assert body.isHidden(), "기본이 접힘이 아니다"
    assert collapsed < 400, f"접었는데 최소 높이 {collapsed}px 가 내용을 그대로 물고 있다"

    panel._button.setChecked(True)
    # 숨김 해제는 레이아웃을 무효화만 한다 — 다시 계산시켜야 값이 바뀐다. **안쪽부터**
    # 해야 한다(바깥만 부르면 49px 그대로다). 실사용에서는 이벤트 루프가 알아서 한다.
    panel.layout().activate()
    box.activate()
    assert not body.isHidden(), "펼쳤는데 내용이 안 보인다"
    assert host.minimumSizeHint().height() >= 400, "펼쳤는데 자리를 요구하지 않는다"


def test_saved_size_wins_over_the_default(qapp, tmp_path, monkeypatch) -> None:
    """**손으로 맞춘 크기가 코드 기본값보다 정확한 의도다.**

    기본값을 그대로 쓰면 사용자는 창을 열 때마다 같은 조정을 반복한다.
    """
    from argus.desktop import app

    state = tmp_path / "window.json"
    monkeypatch.setattr(app, "window_state_path", lambda: state)

    app.save_window_state(1600, 900)
    assert app.load_window_state() == {"width": 1600, "height": 900}

    width, height = app._initial_size()
    available = QtWidgets.QApplication.primaryScreen().availableGeometry()
    assert (width, height) == (min(1600, available.width()), min(900, available.height()))


def test_unusable_saved_size_is_ignored(qapp, tmp_path, monkeypatch) -> None:
    """**최소화 중에 닫히면 8x8 같은 값이 잡힌다.**

    그걸 그대로 복원하면 다음 실행에서 아무것도 안 보이는 창이 뜨고, 사용자는
    창을 못 쓰게 된다 — 되돌릴 방법도 화면 안에 없다.
    """
    from argus.desktop import app

    state = tmp_path / "window.json"
    monkeypatch.setattr(app, "window_state_path", lambda: state)

    app.save_window_state(8, 8)
    assert not state.exists(), "못 쓸 크기를 파일에 남겼다"

    state.write_text('{"width": 8, "height": 8}', encoding="utf-8")
    assert app.load_window_state() is None, "못 쓸 크기를 읽어들였다"

    state.write_text("깨진 json", encoding="utf-8")
    assert app.load_window_state() is None, "깨진 파일에 예외가 났다"


def test_window_never_opens_larger_than_the_screen(qapp) -> None:
    """**하드웨어를 가정하지 않는다**(설계 규칙 2).

    원하는 크기를 고정하면 작은 화면에서 창이 화면 밖으로 나가고, 그러면 스크롤조차
    못 한다.
    """
    from argus.desktop.app import _initial_size

    width, height = _initial_size()
    available = QtWidgets.QApplication.primaryScreen().availableGeometry()
    assert width <= available.width() and height <= available.height()


# ---------------------------------------------------------------- 첫 실행 안내
#
# **"데이터 없음"과 "고장남"을 구분해 주는 화면이다**(설계 규칙 4). Streamlit 홈에만
# 있던 것을 2026-08-09 에 창으로 옮겼다 — 그전까지 네이티브는 페이지마다
# "…기다리는 중"만 띄웠고, 배포 사용자가 처음 보는 화면이 정확히 그것이다.


def test_first_run_notice_names_the_path_it_looked_at(tmp_path, monkeypatch) -> None:
    """**경로가 안내의 본체다.**

    "수집이 시작되지 않았습니다"만으로는 상주를 안 켠 것인지 창이 엉뚱한 곳을 보는
    것인지 가릴 수 없다. 셋을 가르는 정보는 찾은 위치뿐이다.
    """
    from argus.dashboard import data
    from argus.desktop import app

    missing = tmp_path / "없는곳" / "argus.db"
    monkeypatch.setattr(data, "db_path", lambda: missing)

    notice = app.first_run_notice()
    assert notice is not None
    assert str(missing) in notice


def test_first_run_notice_is_silent_once_the_db_exists(tmp_path, monkeypatch) -> None:
    """DB 가 생기면 사라져야 한다 — 창을 열어 둔 채 상주를 켜는 경우가 있다."""
    from argus.dashboard import data
    from argus.desktop import app

    present = tmp_path / "argus.db"
    present.write_bytes(b"")
    monkeypatch.setattr(data, "db_path", lambda: present)

    assert app.first_run_notice() is None


def test_first_run_notice_tells_frozen_users_a_command_they_can_run(
    tmp_path, monkeypatch
) -> None:
    """**배포판 사용자에게 `python -m argus` 는 실행할 수 없는 명령이다.**

    같은 문구를 양쪽에 쓰면 exe 사용자는 안내를 따라도 아무 데도 못 간다.
    """
    from argus.dashboard import data
    from argus.desktop import app

    monkeypatch.setattr(data, "db_path", lambda: tmp_path / "argus.db")

    monkeypatch.setattr(app, "is_frozen", lambda: True)
    frozen = app.first_run_notice()
    monkeypatch.setattr(app, "is_frozen", lambda: False)
    source = app.first_run_notice()

    assert frozen is not None and source is not None
    assert "python -m argus" not in frozen, "exe 사용자에게 소스 실행 명령을 안내했다"
    assert "python -m argus" in source
    assert frozen != source


def test_banner_hides_itself_when_there_is_nothing_to_say(qapp) -> None:
    """**`isVisible()` 이 아니라 `isHidden()` 으로 본다**(CLAUDE.md).

    부모 창을 띄우지 않는 테스트에서 `isVisible()` 은 항상 False 라 아무것도
    검증하지 못한다.
    """
    from argus.desktop.app import _Banner

    banner = _keep(_Banner())
    assert banner.isHidden()

    banner.update_text("무슨 일이 있었다")
    assert not banner.isHidden()
    assert banner.text() == "무슨 일이 있었다"

    banner.update_text(None)
    assert banner.isHidden()


# ---------------------------------------------------------------- 상태 한 줄
#
# 창 맨 위의 "지금 괜찮은가". **이 줄이 틀리면 나머지가 다 맞아도 소용없다** —
# 사용자는 이것만 보고 창을 닫는다.


def test_status_says_collection_stopped_before_saying_normal() -> None:
    """**수집이 멈추면 사건도 안 생긴다** — "정상"과 "죽음"이 똑같이 조용해 보인다.

    순서가 뒤집혀 사건부터 보면, 상주가 죽은 PC 에 초록불이 켜진다. 사용자는
    모니터가 죽은 것을 모른 채 안심한다(설계 규칙 4: 조용히 실패하지 않는다).
    """
    from argus.dashboard import theme
    from argus.desktop.app import _health_line

    now = 10_000.0
    dead = {"open": None, "last_end_ts": None, "sample_ts": now - 600}
    text, detail, colour, incident = _health_line(dead, now)
    assert "멈췄" in text, f"수집이 10분 끊겼는데 '{text}' 라고 했다"
    assert colour == theme.STATUS["critical"]
    assert "10분" in detail

    fresh = {"open": None, "last_end_ts": None, "sample_ts": now - 3}
    assert _health_line(fresh, now)[0] == "정상"


def test_status_does_not_cry_stopped_while_throttled() -> None:
    """**스로틀은 정상 동작이다**(예산 초과 시 수집 주기 ×10).

    그때마다 "수집 멈춤"이 뜨면 그것이 오탐이고, 오탐 3번이면 사용자는 이 줄을
    믿지 않게 된다 — 그러면 맨 윗줄을 만든 의미가 사라진다.
    """
    from argus.desktop.app import _health_line

    now = 10_000.0
    throttled = {"open": None, "last_end_ts": None, "sample_ts": now - 11}  # 1초 주기 x10
    assert _health_line(throttled, now)[0] == "정상"


def test_status_shows_the_open_incident_with_its_severity_colour() -> None:
    """진행 중인 사건이 있으면 **그 문장이 곧 답이다.**

    `incidents.title` 은 이미 "디스크 병목 — chrome 68%" 형태다. 여기서 다시
    문장을 만들지 않는다 — 두 곳에서 만들면 두 곳이 갈린다.
    """
    from argus.dashboard import theme
    from argus.desktop.app import _health_line

    now = 10_000.0
    health = {
        "open": {"id": 42, "ts_start": now - 720, "severity": "critical",
                 "title": "디스크 병목 — chrome 68%"},
        "last_end_ts": now - 99_999,
        "sample_ts": now - 2,
    }
    text, detail, colour, incident = _health_line(health, now)
    assert text == "디스크 병목 — chrome 68%"
    assert colour == theme.STATUS["critical"], "심각한 사건인데 경고색이다"
    assert "12분" in detail
    assert incident == 42, "사건을 열 수 있어야 한다 — 못 열면 그냥 불안만 준다"


def test_status_line_offers_the_incident_only_when_there_is_one(qapp) -> None:
    """**배선 확인.** 판정이 맞아도 버튼이 안 뜨면 사용자는 사건에 닿지 못한다."""
    from argus.desktop.app import _StatusLine

    line = _keep(_StatusLine())
    now = time.time()
    line.update_health({"open": None, "last_end_ts": None, "sample_ts": now})
    assert line._open_btn.isHidden(), "사건이 없는데 '사건 보기'가 떠 있다"
    assert line.text == "정상"

    line.update_health(
        {
            "open": {"id": 7, "ts_start": now - 60, "severity": "warning", "title": "CPU 병목"},
            "last_end_ts": None,
            "sample_ts": now,
        }
    )
    assert not line._open_btn.isHidden()
    assert line.text == "CPU 병목"


def test_status_says_nothing_was_collected_yet() -> None:
    """표본이 아예 없는 것은 고장이 아니라 **아직 안 켠 것**이다. 빨간불이 아니다."""
    from argus.dashboard import theme
    from argus.desktop.app import _health_line

    text, _detail, colour, _id = _health_line(
        {"open": None, "last_end_ts": None, "sample_ts": None}, 10_000.0
    )
    assert "없습니다" in text
    assert colour != theme.STATUS["critical"], "안 켠 것을 고장으로 표시했다"


def test_only_the_bottleneck_tile_is_marked(qapp) -> None:
    """**맨 윗줄이 "메모리 병목"이라 했으면 메모리 타일이 그 근거로 보여야 한다.**

    전부 칠하면 아무것도 강조하지 않은 것과 같고, 엉뚱한 타일을 칠하면 사용자를
    잘못된 곳으로 보낸다 — 2026-08-02 에 제품이 8건 모두 엉뚱한 프로세스를 지목한
    것과 같은 종류의 실패다.
    """
    page = _page(qapp)
    page.mark_bottleneck(
        {"open": {"id": 1, "bottleneck": "MEMORY", "severity": "critical"}}
    )
    marked = [name for name, tile in page._tiles.items() if tile.marked]
    assert marked == ["mem"], f"메모리 병목인데 강조된 타일이 {marked}"

    page.mark_bottleneck({"open": None})
    assert not any(t.marked for t in page._tiles.values()), "사건이 끝났는데 강조가 남았다"


def test_unmappable_bottleneck_marks_nothing(qapp) -> None:
    """**NONE 은 강조하지 않는다.** 병목이 없다는 판정에 어느 타일을 칠할 수는 없다.

    모르는 이름도 같다 — 탐지기에 종류가 하나 늘었을 때 UI 가 엉뚱한 타일을
    칠하느니 조용한 편이 낫다.
    """
    page = _page(qapp)
    for name in ("NONE", "새로운_병목", ""):
        page.mark_bottleneck({"open": {"id": 1, "bottleneck": name, "severity": "warning"}})
        marked = [k for k, t in page._tiles.items() if t.marked]
        assert marked == [], f"{name!r} 인데 {marked} 를 칠했다"


def test_marked_tile_says_it_in_words_not_only_colour(qapp) -> None:
    """**색만으로 뜻을 지지 않는다**(theme 규칙).

    색각 이상이 있어도, 흑백으로 캡처해도 읽혀야 한다.
    """
    from argus.desktop.widgets import StatTile

    tile = _keep(StatTile("메모리"))
    tile.mark("warning")
    assert "병목" in tile._caption.text(), tile._caption.text()

    tile.mark(None)
    assert tile._caption.text() == "메모리", "해제했는데 말이 남았다"


# ---------------------------------------------------------------- 알림 → 사건
#
# 알림을 눌러서 들어온 사람은 **평가하러 온 것**이다. 그 사건을 찾는 일이 남아 있으면
# 거기서 그만둔다 — 실제로 14일간 사건 158건 · 알림 11건에 피드백이 0건이었다.


def _rows(*ids: int) -> list[dict]:
    return [
        {
            "id": i,
            "ts_start": 1000.0 + i,
            "ts_end": 1200.0 + i,
            "severity": "warning",
            "title": f"사건 {i}",
            "contributors": "[]",
            "detectors": "[]",
            "signal_count": 1,
        }
        for i in ids
    ]


def test_focus_selects_that_incident_not_the_first(qapp) -> None:
    """**목록 첫 줄로 보내면 안 된다.** 그건 "못 찾았다"와 구분되지 않는다."""
    page = _incident_page(qapp)
    page.focus_incident(7)
    page._on_rows(_rows(9, 8, 7, 6))

    assert page._selected_id == 7, f"엉뚱한 사건이 열렸다: {page._selected_id}"


def test_focus_widens_the_range_when_the_incident_is_older(qapp) -> None:
    """기본 구간은 7일인데 알림은 그보다 오래된 것을 가리킬 수 있다.

    (창을 며칠 만에 열면 그렇다.) 빈손으로 두면 사용자는 자기가 무엇을 눌렀는지도
    모른 채 목록 앞에 서게 된다.
    """
    page = _incident_page(qapp)
    before = int(page._days.currentData())
    page.focus_incident(99)
    page._on_rows(_rows(3, 2, 1))  # 99 가 없다

    assert int(page._days.currentData()) > before, "구간 밖 사건인데 넓히지 않았다"
    assert page._pending_id == 99, "다시 찾을 대상을 잃어버렸다"

    page._on_rows(_rows(99, 3, 2, 1))
    assert page._selected_id == 99


def test_focus_says_so_when_the_incident_is_gone(qapp) -> None:
    """**조용히 목록 앞으로 보내지 않는다**(설계 규칙 4).

    30일 안에도 없으면 보존 정리로 사라진 것이다. 그때 아무 말 없이 다른 사건을
    펴 두면, 사용자는 그것을 방금 누른 알림으로 알고 **틀린 라벨**을 남긴다 —
    없는 것보다 나쁜 데이터다.
    """
    page = _incident_page(qapp)
    page._days.setCurrentIndex(page._days.count() - 1)  # 이미 최대 구간
    page.focus_incident(1234)
    page._on_rows(_rows(3, 2, 1))

    assert page._selected_id != 1234
    assert "1234" in page._detail_head.text(), "못 찾았다는 말을 하지 않았다"
    assert page._pending_id is None, "찾을 수 없는 것을 계속 기다린다"


def test_cli_passes_the_incident_through(monkeypatch) -> None:
    """**진입점이 인자를 흘리면 알림 클릭이 조용히 평범한 창 열기가 된다.**

    exe 도 이 경로를 탄다 — 여기가 끊기면 배포판에서만 안 되는 상태가 된다.
    """
    import sys

    from argus.desktop import app

    seen: dict = {}
    monkeypatch.setattr(app, "main", lambda seconds=None, incident_id=None: seen.update(
        seconds=seconds, incident_id=incident_id
    ) or 0)
    monkeypatch.setattr(sys, "argv", ["argus-ui", "--incident", "156"])

    app.cli()

    assert seen.get("incident_id") == 156, f"사건 id 가 전달되지 않았다: {seen}"


# ------------------------------------------------------- 답 대기 알림 (라벨 유입)
#
# **2026-08-14 에 만든 경로다.** 라벨 UI 는 08-09 에 이미 있었는데 5일 뒤에도 라벨이
# 0건이었다(사건 173 · 알림 50 · 라벨 0). 경로가 없었던 게 아니라 그리로 갈 이유가
# 화면에 없었다. 여기서 고정하는 것은 "무엇을 물을 것인가"와 "언제 묻지 않을 것인가"다.


def _label_db(tmp_path, rows, injections=()):
    """작은 DB. `rows` 는 (id, 며칠 전, notified, label), `injections` 는 (며칠 전, 며칠 전)."""
    import sqlite3

    database = tmp_path / "labels.db"
    now = time.time()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY, ts_start REAL, ts_end REAL,"
            " severity TEXT, title TEXT, notified INTEGER, user_label TEXT, labeled_at REAL,"
            " auto_label TEXT)"
        )
        conn.execute(
            "CREATE TABLE fault_injections (id INTEGER PRIMARY KEY, scenario TEXT,"
            " ts_start REAL, ts_end REAL)"
        )
        for incident_id, days_ago, notified, label in rows:
            start = now - days_ago * 86400
            conn.execute(
                "INSERT INTO incidents (id, ts_start, ts_end, severity, title, notified,"
                " user_label) VALUES (?, ?, ?, 'warning', ?, ?, ?)",
                (incident_id, start, start + 60, f"사건 {incident_id}", notified, label),
            )
        for start_days, end_days in injections:
            conn.execute(
                "INSERT INTO fault_injections (scenario, ts_start, ts_end)"
                " VALUES ('handle_leak', ?, ?)",
                (now - start_days * 86400, None if end_days is None else now - end_days * 86400),
            )
    return database


def test_pending_answers_count_only_the_notifications_we_sent(monkeypatch, tmp_path) -> None:
    """**답 대기는 "알림이 나갔고 답이 없는" 것뿐이다.**

    셋을 가른다. 알림이 안 나간 사건은 아무도 성가시게 하지 않았으니 물을 것이 없고,
    이미 답한 것은 다시 묻지 않으며, 오래된 것은 사용자가 기억하지 못한다 — 짐작으로
    붙인 라벨은 문턱을 고칠 근거가 못 되므로 없는 것만 못하다.

    **기간을 기본값(14일)이 아닌 값으로도 잰다.** 기본값으로만 재면 인자가 무시되어도
    통과한다 — 코드 기본값과 시험값이 같아 신호가 없던 실패를 2026-08-04 에 네 번 겪었다.
    """
    from argus.dashboard import data

    database = _label_db(
        tmp_path,
        [
            (1, 0.5, 1, None),      # 오늘 알림, 답 없음 → 센다
            (2, 3.0, 1, None),      # 사흘 전 알림, 답 없음 → 센다
            (3, 1.0, 0, None),      # 알림이 안 나갔다 → 물을 것이 없다
            (4, 1.0, 1, "normal"),  # 이미 답했다
            (5, 1.0, 1, "real"),    # 이미 답했다
            (6, 20.0, 1, None),     # 20일 전 — 기억이 없다
        ],
    )
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()

    pending = [row["id"] for row in data.unlabeled_notified()]
    assert pending == [1, 2], f"답 대기가 {pending} 다 — 알림·답·기간 중 하나를 안 가렸다"

    data.unlabeled_notified.cache_clear()
    narrow = [row["id"] for row in data.unlabeled_notified(days=1.0)]
    assert narrow == [1], f"기간 1일을 줬는데 {narrow} 를 세었다 — 인자가 흐르지 않는다"


def test_pending_answers_are_asked_newest_first(monkeypatch, tmp_path) -> None:
    """**최근 것부터 묻는다.** 오늘 아침 알림은 답할 수 있어도 열흘 전 것은 짐작이 된다.

    맨 윗줄의 "답하기"가 이 순서의 첫 줄로 데려가므로, 순서가 뒤집히면 사용자는 매번
    가장 기억나지 않는 것을 먼저 받는다.
    """
    from argus.dashboard import data

    database = _label_db(tmp_path, [(1, 9.0, 1, None), (2, 0.2, 1, None), (3, 4.0, 1, None)])
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()

    assert [r["id"] for r in data.unlabeled_notified()] == [2, 3, 1]


def test_health_carries_the_pending_answer_count(monkeypatch, tmp_path) -> None:
    """**배선 확인.** 세는 것이 맞아도 맨 윗줄까지 오지 않으면 사용자는 못 본다.

    `health()` 는 창에서 유일하게 항상 보이는 줄이 읽는 값이다. 여기 빠지면 답 대기는
    사건 탭을 연 사람만 알게 되고, 그것이 08-09~08-14 의 상태였다.
    """
    import sqlite3

    from argus.dashboard import data

    database = _label_db(tmp_path, [(1, 0.5, 1, None), (2, 0.6, 1, None), (3, 0.7, 0, None)])
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE metrics_raw (ts REAL)")
        conn.execute("INSERT INTO metrics_raw (ts) VALUES (?)", (time.time(),))
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()
    data.health.cache_clear()

    assert data.health()["unlabeled"] == 2


def test_answering_clears_the_pending_count_too(monkeypatch, tmp_path) -> None:
    """**답한 것이 곧바로 카운트에서 빠져야 한다.**

    목록은 `_reload` 로 즉시 바뀌는데 맨 윗줄의 "N건"만 최대 10초 남으면, 사용자는
    답이 저장되지 않았다고 읽고 다시 누른다. 예외가 아니라 값만 어긋나는 종류라
    조용히 지나간다.
    """
    from argus.dashboard import data

    database = _label_db(tmp_path, [(1, 0.5, 1, None)])
    monkeypatch.setattr(data, "db_path", lambda: database)

    cleared: list[str] = []
    monkeypatch.setattr(data.unlabeled_notified, "cache_clear",
                        lambda: cleared.append("unlabeled"))
    monkeypatch.setattr(data.health, "cache_clear", lambda: cleared.append("health"))

    data.set_user_label(1, "normal")

    assert "unlabeled" in cleared, "답을 저장하고도 답 대기 캐시를 비우지 않았다"
    assert "health" in cleared, "맨 윗줄이 읽는 캐시를 비우지 않았다"


def test_answer_prompt_is_quiet_when_there_is_nothing_to_ask() -> None:
    """**0건에 버튼을 남기지 않는다.** 늘 떠 있는 것은 배경이 되어 눈에 걸리지 않는다.

    수집이 멈춘 상태에서도 묻지 않는다 — 그때 화면이 시켜야 할 일은 "상주를 확인하라"
    하나뿐이고, 옆에 라벨 요청을 나란히 두면 어느 쪽이 급한지 흐려진다(설계 규칙 4).
    """
    from argus.desktop.app import _label_prompt

    now = 10_000.0
    assert _label_prompt({"unlabeled": 0, "sample_ts": now - 2}, now) is None
    assert _label_prompt({"sample_ts": now - 2}, now) is None, "값이 없으면 조용해야 한다"
    assert _label_prompt({"unlabeled": 3, "sample_ts": None}, now) is None, "첫 실행"
    assert _label_prompt({"unlabeled": 3, "sample_ts": now - 600}, now) is None, "수집 멈춤"

    prompt = _label_prompt({"unlabeled": 3, "sample_ts": now - 2}, now)
    assert prompt is not None and "3" in prompt, f"밀린 3건을 말하지 않았다: {prompt!r}"


def test_status_line_shows_the_answer_button_even_when_all_is_well(qapp) -> None:
    """**배선 확인.** 판정이 맞아도 버튼이 안 뜨면 라벨은 계속 0건이다.

    "정상"일 때 보이는 것이 요점이다 — 답할 알림은 대개 이미 끝난 사건이라, 진행 중인
    사건이 있을 때만 뜨는 `사건 보기` 옆자리로는 영영 닿지 않는다.
    """
    from argus.desktop.app import _StatusLine

    line = _keep(_StatusLine())
    now = time.time()
    line.update_health({"open": None, "last_end_ts": None, "sample_ts": now, "unlabeled": 4})
    assert line.text == "정상"
    assert "4" in line.label_text, f"정상일 때 답하기가 안 보인다: {line.label_text!r}"

    line.update_health({"open": None, "last_end_ts": None, "sample_ts": now, "unlabeled": 0})
    assert line.label_text == "", "답할 것이 없는데 버튼이 남았다"


def test_incident_list_marks_unanswered_notifications() -> None:
    """**목록에서 답 안 준 것이 보여야 한다.**

    상세를 열기 전에는 어느 것이 남았는지 알 수 없어 하나씩 눌러 봐야 했다.
    **알림이 안 나간 사건은 빈칸이다** — 답을 안 준 것과 물은 적이 없는 것은 다르고,
    173건 전부에 물음표를 세우면 밀린 50건이 그 안에 묻힌다.
    """
    from argus.desktop.pages.incidents import _list_row

    base = {"ts_start": 1000.0, "ts_end": 1100.0, "severity": "warning", "title": "t"}
    assert _list_row({**base, "id": 1, "notified": 1})["answer"] == "?"
    assert _list_row({**base, "id": 2, "notified": 0})["answer"] == ""
    assert _list_row({**base, "id": 3, "notified": 1, "user_label": "normal"})["answer"] == "정상"
    assert _list_row({**base, "id": 4, "notified": 1, "user_label": "real"})["answer"] == "비정상"


def test_incident_tile_asks_instead_of_reporting_nothing(qapp) -> None:
    """**"피드백 없음"은 결과처럼 읽힌다.** 밀린 수를 세어 요구로 바꾼다.

    이 문구가 08-09~08-14 동안 화면에 있던 전부였고, 그동안 라벨은 0건이었다.
    """
    page = _incident_page(qapp)
    rows = _rows(1, 2, 3)
    for row, notified in zip(rows, (1, 1, 0)):
        row["notified"] = notified

    page._update_summary(rows)
    detail = page._tiles["fp"].note
    assert "2" in detail and "알려주세요" in detail, f"답을 청하지 않는다: {detail!r}"

    rows[0]["user_label"] = "normal"
    page._update_summary(rows)
    detail = page._tiles["fp"].note
    assert "1건 답 대기" in detail, f"남은 하나를 말하지 않는다: {detail!r}"


def test_answer_button_goes_to_the_newest_unanswered(qapp, monkeypatch) -> None:
    """**"답하기"는 화면 구간이 아니라 답 대기 창에서 고른다.**

    목록의 기본 구간은 7일인데 답 대기는 14일이다. 화면에 있는 것만 보면 8일 전
    알림은 영영 안 물어보게 된다.
    """
    from argus.dashboard import data
    from argus.desktop.pages import incidents as page_mod

    page = _incident_page(qapp)
    monkeypatch.setattr(page_mod.data, "unlabeled_notified", lambda *a, **k: [{"id": 9}])
    page.focus_unlabeled()
    assert page._pending_id == 9, "가장 최근 답 대기 알림을 고르지 않았다"

    monkeypatch.setattr(page_mod.data, "unlabeled_notified", lambda *a, **k: [])
    page.focus_unlabeled()
    assert "없습니다" in page._detail_head.text(), "답할 것이 없다는 말을 하지 않았다"
    assert data.LABEL_WINDOW_DAYS  # 기간을 화면 문구가 쓴다 — 상수가 사라지면 여기서 걸린다



def _selected_page(qapp, monkeypatch, rows):
    """사건 하나를 고른 상태의 페이지 + 저장 호출 기록."""
    from argus.desktop.pages import incidents as page_mod

    page = _incident_page(qapp)
    saved: list[tuple] = []
    monkeypatch.setattr(page_mod.data, "set_user_label",
                        lambda incident_id, label: saved.append((incident_id, label)))
    monkeypatch.setattr(page, "_reload", lambda: None)
    page._on_rows(rows)
    return page, saved


def test_answer_boxes_cannot_both_be_on(qapp, monkeypatch) -> None:
    """**한 사건이 정상이면서 비정상일 수는 없다.**

    상자를 쓰기로 한 이상 켜진 것이 곧 답이다. 둘 다 켜진 채 남으면 화면이 사용자가
    주지 않은 답을 보이고, 다음에 열었을 때 무엇을 답했는지 알 수 없게 된다.

    **켠 것을 다시 누르면 꺼지고, 그것이 취소다** — 상자에서 자연스러운 동작이다.
    """
    page, saved = _selected_page(qapp, monkeypatch, _rows(1))
    assert page._selected_id == 1

    page._normal_box.click()
    assert saved[-1] == (1, "normal")
    assert page._normal_box.isChecked() and not page._real_box.isChecked()

    page._real_box.click()
    assert saved[-1] == (1, "real")
    assert page._real_box.isChecked() and not page._normal_box.isChecked(), (
        "정상과 비정상이 함께 켜져 있다"
    )

    page._real_box.click()
    assert saved[-1] == (1, None), "켠 것을 다시 눌렀는데 취소가 되지 않았다"
    assert not page._real_box.isChecked() and not page._normal_box.isChecked()


def test_selecting_an_incident_does_not_rewrite_its_label(qapp, monkeypatch) -> None:
    """**고르기만 해서는 DB 를 건드리지 않는다.**

    상자 상태를 세우는 신호로 `toggled`/`stateChanged` 를 쓰면 코드가 값을 비출 때도
    울린다. 그러면 사건을 훑는 것만으로 라벨이 다시 쓰이고 `labeled_at` 이 갱신되어,
    "언제 답했나"가 조용히 망가진다 — 예외가 아니라 값만 틀어지는 종류다.
    """
    rows = _rows(1, 2)
    rows[0]["user_label"] = "normal"
    rows[1]["user_label"] = "real"

    page, saved = _selected_page(qapp, monkeypatch, rows)
    assert page._normal_box.isChecked(), "저장된 답이 상자에 안 비쳤다"

    page._table.selectRow(1)
    assert page._real_box.isChecked() and not page._normal_box.isChecked()
    assert saved == [], f"사건을 고르기만 했는데 DB 를 {len(saved)}번 썼다"


def test_failed_save_does_not_leave_the_box_lying(qapp, monkeypatch) -> None:
    """**저장에 실패하면 상자를 되돌린다.**

    켜진 채로 두면 사용자는 답을 남겼다고 믿고 떠난다. 그러면 라벨은 없는데 다시
    물어볼 기회까지 사라진다 — 조용히 실패하지 않는다(설계 규칙 4).
    """
    from argus.desktop.pages import incidents as page_mod

    page = _incident_page(qapp)
    monkeypatch.setattr(page, "_reload", lambda: None)
    page._on_rows(_rows(1))

    def boom(incident_id, label):
        raise RuntimeError("디스크가 가득 찼다")

    monkeypatch.setattr(page_mod.data, "set_user_label", boom)
    page._normal_box.click()

    assert not page._normal_box.isChecked(), "저장이 실패했는데 답한 것처럼 보인다"
    assert "저장하지 못했" in page._detail_meta.text()



def test_injected_windows_are_not_asked_about(monkeypatch, tmp_path) -> None:
    """**내가 만든 부하에 대한 알림은 묻지 않는다.**

    2026-08-14 에 답 대기 28건 중 5건이 08-02 의 `handle_leak` 배치 구간이었다 —
    `메모리 압박 — python 24%`(주입기 자신)까지 "이 알림이 쓸모 있었나"로 묻고
    있었다. 답할 수 없는 질문이고, 답한다 해도 실사용 문턱의 근거가 아니다.

    **경계가 요점이다.** 겹치는 것만 빠지고 인접한 것은 남아야 한다 — 주입 하나가
    그날 알림을 통째로 삼키면 실제로 답할 것까지 사라진다.
    """
    from argus.dashboard import data

    database = _label_db(
        tmp_path,
        [
            (1, 2.0, 1, None),   # 주입 한복판
            (2, 3.0, 1, None),   # 주입과 무관한 날
            (3, 1.5, 1, None),   # 주입이 끝난 뒤
        ],
        injections=[(2.2, 1.8)],  # 2.2일 전 ~ 1.8일 전
    )
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()

    pending = [row["id"] for row in data.unlabeled_notified()]
    assert 1 not in pending, "주입 구간의 알림을 답하라고 내밀고 있다"
    assert pending == [3, 2], f"주입 밖의 알림까지 사라졌다: {pending}"


def test_open_injection_does_not_swallow_everything_after_it(monkeypatch, tmp_path) -> None:
    """**닫히지 않은 주입을 무한한 구간으로 읽지 않는다.**

    전원이 끊기면 주입기의 `finally` 가 돌지 못해 `ts_end` 가 비어 남는다
    (2026-07-30 실제). 그것을 "그 뒤로 계속 주입 중"으로 읽으면 이후 알림이
    전부 답 대기에서 사라지고, 그 사실은 아무 데도 보이지 않는다.
    """
    from argus.dashboard import data

    database = _label_db(
        tmp_path,
        [(1, 1.0, 1, None), (2, 0.5, 1, None)],
        injections=[(3.0, None)],  # 사흘 전에 시작하고 닫히지 않았다
    )
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()

    assert [r["id"] for r in data.unlabeled_notified()] == [2, 1]


def test_incident_list_marks_injection_instead_of_asking(qapp) -> None:
    """**답 대기에서 뺐으면 왜 안 묻는지가 목록에 보여야 한다.**

    물음표를 달면 거짓말이고, 빈칸으로 두면 알림이 안 나간 사건과 구분되지 않는다.
    """
    from argus.desktop.pages.incidents import _list_row

    base = {"ts_start": 1000.0, "ts_end": 1100.0, "severity": "warning", "title": "t"}
    assert _list_row({**base, "id": 1, "notified": 1, "during_injection": 1})["answer"] == "주입"
    assert _list_row({**base, "id": 2, "notified": 1, "during_injection": 0})["answer"] == "?"


def test_injected_incidents_are_not_counted_as_pending(qapp) -> None:
    """타일의 "답 대기 N건"도 같은 수를 세야 한다. **두 곳이 갈리면 어느 쪽이
    맞는지 알 수 없고, 사용자는 없는 일을 하라는 말을 듣는다.**"""
    page = _incident_page(qapp)
    rows = _rows(1, 2, 3)
    for row, notified, injected in zip(rows, (1, 1, 1), (0, 1, 1)):
        row["notified"] = notified
        row["during_injection"] = injected

    page._update_summary(rows)
    detail = page._tiles["fp"].note
    assert "1" in detail and "알려주세요" in detail, f"주입 건까지 세었다: {detail!r}"



def test_unknown_is_not_asked_again(monkeypatch, tmp_path) -> None:
    """**"모르겠음"도 답이다 — 다시 묻지 않는다.**

    아무것도 안 누르고 지나가면 답 대기에 그대로 남아, "답하기" 를 누를 때마다 같은
    사건으로 되돌아온다. 애매한 것이 가장 최근이면 거기서 막힌다.
    """
    from argus.dashboard import data

    database = _label_db(
        tmp_path,
        [(1, 0.5, 1, None), (2, 0.6, 1, "unknown"), (3, 0.7, 1, "normal")],
    )
    monkeypatch.setattr(data, "db_path", lambda: database)
    data.unlabeled_notified.cache_clear()

    assert [r["id"] for r in data.unlabeled_notified()] == [1], "모르겠음을 다시 묻고 있다"


def test_three_answer_boxes_are_mutually_exclusive(qapp, monkeypatch) -> None:
    """상자가 셋이 돼도 **켜지는 것은 언제나 하나뿐이다.**"""
    page, saved = _selected_page(qapp, monkeypatch, _rows(1))

    page._unknown_box.click()
    assert saved[-1] == (1, "unknown")
    assert page._unknown_box.isChecked()
    assert not page._normal_box.isChecked() and not page._real_box.isChecked()

    page._normal_box.click()
    assert saved[-1] == (1, "normal")
    assert not page._unknown_box.isChecked(), "모르겠음이 켜진 채 남았다"

    page._normal_box.click()
    assert saved[-1] == (1, None)
    assert not any(
        b.isChecked() for b in (page._normal_box, page._real_box, page._unknown_box)
    )


def test_unknown_does_not_dilute_the_false_positive_rate(qapp) -> None:
    """**"모르겠음"은 오탐 비율의 분모가 아니다.**

    판단이 아니라 판단할 수 없었다는 표시다. 분모에 넣으면 답을 모을수록 오탐
    비율이 낮아져 **문제가 작아 보인다** — 고칠 근거를 모으는 일이 근거를 흐린다.
    """
    page = _incident_page(qapp)
    rows = _rows(1, 2, 3, 4)
    for row, label in zip(rows, ("normal", "real", "unknown", "unknown")):
        row["notified"] = 1
        row["user_label"] = label

    page._update_summary(rows)
    assert page._tiles["fp"].value == "50%", (
        f"정상 1 · 비정상 1 · 모르겠음 2 인데 {page._tiles['fp'].value} 라고 한다"
    )


def test_unknown_shows_in_the_list(qapp) -> None:
    """목록에서도 답한 것으로 보여야 한다 — 안 그러면 다시 열어 보게 된다."""
    from argus.desktop.pages.incidents import _list_row

    base = {"ts_start": 1000.0, "ts_end": 1100.0, "severity": "warning", "title": "t"}
    assert _list_row({**base, "id": 1, "notified": 1, "user_label": "unknown"})["answer"] == "모름"



def test_machine_answers_are_marked_apart_from_human_ones(qapp) -> None:
    """**목록만 보고 누가 답했는지 알 수 있어야 한다.**

    같은 글자로 적으면 "내가 언제 이걸 정상이라고 했지"가 된다. 사람 답이 있으면
    그쪽이 이기고, 기계 답에는 점을 붙인다.
    """
    from argus.desktop.pages.incidents import _list_row

    base = {"ts_start": 1000.0, "ts_end": 1100.0, "severity": "warning", "title": "t", "notified": 1}
    assert _list_row({**base, "id": 1, "auto_label": "normal"})["answer"] == "·정상"
    assert _list_row({**base, "id": 2, "auto_label": "real"})["answer"] == "·비정상"
    assert (
        _list_row({**base, "id": 3, "auto_label": "normal", "user_label": "real"})["answer"]
        == "비정상"
    ), "사람 답이 있는데 기계 답을 보이고 있다"


def test_machine_answers_do_not_hide_that_they_are_machine(qapp) -> None:
    """타일이 몇 건이 기계 것인지 말해야 한다. 섞어 놓고 한 숫자만 보이면
    "오탐 33%"가 사람의 판단인지 내 규칙의 메아리인지 구분할 수 없다."""
    page = _incident_page(qapp)
    rows = _rows(1, 2, 3)
    for row, human, auto in zip(rows, ("normal", None, None), (None, "normal", "real")):
        row["notified"] = 1
        row["user_label"] = human
        row["auto_label"] = auto

    page._update_summary(rows)
    note = page._tiles["fp"].note
    assert "사람 1건" in note and "자동 2건" in note, note
    assert page._tiles["fp"].value == "67%", page._tiles["fp"].value


def test_machine_answered_notifications_leave_the_pending_queue(qapp) -> None:
    """자동 라벨을 넣은 이유가 밀린 답이다. 판정이 붙었는데도 계속 물으면
    아무것도 줄지 않는다."""
    page = _incident_page(qapp)
    rows = _rows(1, 2)
    for row, auto in zip(rows, ("normal", None)):
        row["notified"] = 1
        row["auto_label"] = auto

    page._update_summary(rows)
    assert "1건" in page._tiles["fp"].note, page._tiles["fp"].note


def test_machine_answer_shows_its_reason(qapp) -> None:
    """**근거 없는 판정은 뒤집을 수 없다.** 뒤집을 수 없으면 사람 답을 모으는 길이 막힌다."""
    page = _incident_page(qapp)
    row = _rows(1)[0]
    row.update({"notified": 1, "auto_label": "normal", "auto_label_reason": "원인이 직접 띄운 앱이다"})
    page._render_detail(row)
    assert "자동: 정상" in page._detail_meta.text()
    assert "원인이 직접 띄운 앱이다" in page._detail_meta.text()

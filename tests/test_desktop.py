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
    from PySide6 import QtCore
except ImportError:  # PySide6 가 없으면 Qt 테스트는 건너뛴다
    QtCore = None


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
    assert page._normal_btn.isEnabled(), "사건을 골랐는데 피드백을 못 준다"


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

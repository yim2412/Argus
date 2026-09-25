"""컴포넌트 건강 — 멈춘 구성요소가 창에 보인다 (감사 F-014).

처음엔 컴포넌트가 setup 에서 죽거나 tick 이 계속 실패해도 흔적은 로그와 `crash_*.json` 뿐이었고
둘 다 읽는 화면이 없었다. 융합·탐지·롤업이 죽으면 새 사건이 안 생겨 창에는 "정상"으로 보였다 —
수집은 살아 있으니 "수집이 멈췄습니다" 판정으로도 안 갈렸다.
"""

from __future__ import annotations

import threading
import time

import pytest

from argus.config.loader import ComponentHealthSettings
from argus.runtime.supervisor import CallableComponent, Component, Supervisor, write_health_file

# failing_after=2: 실패하면 백오프(1초·2초…)가 걸려 1.5초 안에 연속 실패가 두 번뿐이다
FAST = ComponentHealthSettings(publish_s=0.2, failing_after=2, stale_factor=2.0, stale_min_s=0.5)


class _BadSetup(Component):
    name = "bad_setup"
    interval_s = 0.05

    def setup(self) -> None:
        raise RuntimeError("주입: 기동 실패")

    def tick(self) -> None:  # pragma: no cover — 안 불린다
        pass


def _boom() -> None:
    raise RuntimeError("주입: 매 틱 실패")


@pytest.fixture()
def running():
    release = threading.Event()
    sup = Supervisor(wake_granularity_s=0.05, health_settings=FAST)
    sup.add(_BadSetup())
    sup.add(CallableComponent("failing", _boom, interval_s=0.01))
    sup.add(CallableComponent("fine", lambda: None, interval_s=0.05))
    sup.add(CallableComponent("hung", lambda: release.wait(10), interval_s=0.05))  # tick 이 안 돌아온다
    sup.start()
    yield sup
    release.set()
    sup.stop(timeout=5)


def test_statuses_tell_dead_from_quiet(running):
    time.sleep(1.5)
    status = {n: d["status"] for n, d in running.health_snapshot()["components"].items()}
    assert status["fine"] == "ok", "대조: 멀쩡한 컴포넌트까지 문제로 보이면 이 표는 아무것도 안 가른다"
    assert status["bad_setup"] == "setup_failed"
    assert status["failing"] == "failing"
    assert status["hung"] == "stale", "tick 이 안 돌아오는 컴포넌트가 멈춤으로 안 보인다 — 예외가 없어 실패 수로는 안 보인다"


def test_wait_loop_publishes_to_the_sink():
    got: list[dict] = []
    sup = Supervisor(wake_granularity_s=0.05, health_sink=got.append, health_settings=FAST)
    sup.add(CallableComponent("fine", lambda: None, interval_s=0.05))
    sup.start()
    waiter = threading.Thread(target=sup.wait)
    waiter.start()
    time.sleep(0.9)
    sup.request_stop()
    waiter.join(5)
    sup.stop(timeout=5)
    assert got, "메인 대기 루프가 건강 표를 넘기지 않았다 — 창에 아무것도 안 간다"
    assert got[-1]["components"]["fine"]["status"] == "ok"


def test_sink_failure_does_not_stop_the_resident():
    def broken(_snap):
        raise OSError("주입: 디스크 가득 참")

    sup = Supervisor(wake_granularity_s=0.05, health_sink=broken, health_settings=FAST)
    sup.publish_health()                                         # 예외가 밖으로 나오면 안 된다


def test_window_reads_what_the_resident_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    from argus.dashboard import data
    from argus.paths import components_health_path

    assert data.broken_components() == [], "파일이 없으면 빈 목록이어야 한다(창이 죽으면 안 된다)"
    write_health_file({"written_at": 1.0, "components": {
        "fusion": {"status": "failing"}, "collector": {"status": "ok"}}}, components_health_path())
    assert data.broken_components() == [{"name": "fusion", "status": "failing"}]
    components_health_path().write_text("{깨진 json", encoding="utf-8")
    assert data.broken_components() == []


def test_status_line_says_components_stopped_before_incidents():
    from argus.desktop.app import _health_line

    now = time.time()
    base = {"sample_ts": now - 1, "open": None, "last_end_ts": None, "unlabeled": 0}
    assert _health_line({**base, "broken": []}, now)[0] == "정상", "대조"
    text, detail, _c, _id = _health_line({**base, "broken": [{"name": "fusion", "status": "stale"}]}, now)
    assert text == "구성요소가 멈췄습니다" and "fusion(응답 없음)" in detail
    # 수집이 통째로 멈췄으면 그쪽이 먼저다
    assert _health_line({**base, "sample_ts": now - 600, "broken": [{"name": "fusion", "status": "stale"}]}, now)[0] \
        == "수집이 멈췄습니다"

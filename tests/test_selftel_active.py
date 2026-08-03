"""예산 초과 시점에 누가 돌고 있었는지 남기는가.

2026-08-04 에 예산(300MB)을 넘긴 시점의 RSS 는 447/469/553/588MB 였는데 `self_telemetry`
평균은 65MB, `private_mb` 는 501MB 로 평탄했다. **순간값만 튄다** — 5초 표본이 봉우리를
놓치고 있다는 뜻이다. 더 자주 찍는 것은 관측자를 무겁게 하므로(설계 규칙 1), 대신
표본 시점에 tick 중이던 컴포넌트를 남겨 스로틀 표본만 모아 분포로 본다.

이 계측이 조용히 끊기면 **컬럼이 전부 NULL 인 채로 며칠이 지나고**, 원인 판정은
그만큼 미뤄진다. 예외가 아니라 값이 비는 종류라 여기서 따로 잡는다.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from argus.runtime.selftel import SelfTelemetry  # noqa: E402
from argus.runtime.supervisor import CallableComponent, Supervisor  # noqa: E402


def _supervisor_with(component: CallableComponent) -> Supervisor:
    sup = Supervisor(wake_granularity_s=0.05)
    sup.add(component)
    return sup


def test_active_components_reports_running_tick() -> None:
    inside = threading.Event()
    release = threading.Event()

    def slow() -> None:
        inside.set()
        release.wait(2.0)

    sup = _supervisor_with(CallableComponent("slow", slow, interval_s=0.05))
    sup.start()
    try:
        assert inside.wait(2.0), "컴포넌트가 tick 에 들어가지 않았다"
        assert "slow" in sup.active_components(), "tick 중인 컴포넌트가 잡히지 않는다"
    finally:
        release.set()
        sup.stop(timeout=5.0)


def test_active_components_clears_after_failing_tick() -> None:
    """**예외로 끝난 tick 도 지워야 한다.**

    `finally` 없이 정상 경로에서만 지우면, 한 번 터진 컴포넌트가 영원히 "실행 중"으로
    남아 모든 표본에 그 이름이 박힌다. 그러면 계측이 범인을 가리키는 게 아니라
    **아무나 가리킨다** — 없는 것보다 나쁘다.
    """
    ticked = threading.Event()

    def boom() -> None:
        ticked.set()
        raise RuntimeError("의도된 실패")

    sup = _supervisor_with(CallableComponent("boom", boom, interval_s=0.05))
    sup.start()
    try:
        assert ticked.wait(2.0)
        time.sleep(0.2)  # 백오프 대기 중 — tick 밖이다
        assert "boom" not in sup.active_components(), "예외로 끝난 tick 이 실행 중으로 남았다"
    finally:
        sup.stop(timeout=5.0)


def test_active_components_sorted_by_how_long_they_have_run() -> None:
    """오래 돈 것이 앞. 상위 셋만 남기므로 순서가 곧 무엇을 버리느냐다."""
    sup = Supervisor(wake_granularity_s=0.05)
    sup._active = {"late": 200.0, "early": 100.0, "middle": 150.0}
    assert sup.active_components() == ["early", "middle", "late"]


# ---------------------------------------------------------------- 배선


class _FakeDB:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.columns: list[str] = []

    def insert_many(self, table: str, columns: list[str], rows: list[tuple]) -> None:
        self.columns = columns
        self.rows.extend(rows)


class _FakeGuard:
    level = 0

    class _Mem:
        private_mb = 1.0
        peak_wset_mb = 2.0
        page_faults = 3

    def last(self) -> tuple[float, float]:
        return (0.1, 50.0)

    def last_memory(self) -> "_FakeGuard._Mem":
        return self._Mem()


def _write_one(active_fn) -> dict:
    db = _FakeDB()
    tel = SelfTelemetry(db, _FakeGuard(), interval_s=1.0, active_fn=active_fn)  # type: ignore[arg-type]
    tel.tick()
    return dict(zip(db.columns, db.rows[0]))


def test_active_column_is_written() -> None:
    row = _write_one(lambda: ["rollup", "network"])
    assert row["active"] == "rollup,network", "실행 중 컴포넌트가 행에 실리지 않는다"


def test_active_column_excludes_self_and_caps_length() -> None:
    """자기 자신은 빼고 상위 셋까지만. 매 5초 행마다 문자열이 길어질 이유가 없다."""
    row = _write_one(lambda: ["self_telemetry", "a", "b", "c", "d"])
    assert row["active"] == "a,b,c"


def test_active_lookup_failure_does_not_stop_telemetry() -> None:
    """**계측 부가 정보가 계측 자체를 죽이면 안 된다.**

    자기 상태 기록은 예산 초과·누수 판정의 유일한 근거다. 여기가 한 줄 때문에
    멈추면 규칙 1(관측자는 가벼워야 한다)을 확인할 방법이 없어진다.
    """

    def broken() -> list[str]:
        raise RuntimeError("수퍼바이저가 사라졌다")

    row = _write_one(broken)
    assert row["active"] is None
    assert row["rss_mb"] == 50.0, "행 자체는 정상적으로 기록되어야 한다"


def test_active_is_none_without_supervisor() -> None:
    """스모크·테스트처럼 수퍼바이저 없이 쓰는 경로도 있다."""
    assert _write_one(None)["active"] is None


def test_migration_adds_the_column() -> None:
    """**로직과 배선을 따로 잰다** — 컬럼이 없으면 INSERT 가 통째로 실패한다."""
    sql = (ROOT / "argus" / "storage" / "migrations" / "013_selftel_active.sql").read_text(
        encoding="utf-8"
    )
    assert "ALTER TABLE self_telemetry ADD COLUMN active" in sql

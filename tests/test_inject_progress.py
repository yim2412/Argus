"""주입 진행 도구 — **진행을 재는 도구가 추측 보고를 하면 안 된다**(전역 규칙 1).

2026-08-15 에 이 도구를 그냥 돌려 봤더니, 13일 전에 끝난 배치를 두고 개별 회차는
전부 `완료 100%` 인데 전체는 `85% · 남음 약 17분 43초` 라고 말했다. 열린 라벨은
0건이었다. 겹쳐서 강제 중단한 두 회차(`#51`·`#52`, 채점 제외로 닫은 것)를 "덜 끝난
회차"로 세고 있었다.

**도구가 스스로 틀린 진행을 말하면 그 도구로 판정한 모든 회차가 의심스러워진다.**
`PLAN.md` 는 이 도구로 배치를 판정하라고 적어 두었고, 근사값으로 재려던 시도가 이미
세 번 틀렸다.

여기서 잡는 것 셋이다.
- 끝난 배치에 **"남음"을 적지 않는다**
- 끝난 회차를 **덜 진행된 것으로 세지 않는다** (개별 줄과 전체가 어긋나면 안 된다)
- 배치가 **언제 것인지** 헤더에 있다 (지금 도는 것으로 읽히면 안 된다)
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from argus.storage.hot import Database


def _module():
    """`tools/inject_progress.py` 를 모듈로 연다. 패키지가 아니라 경로로 로드한다."""
    path = Path(__file__).resolve().parent.parent / "tools" / "inject_progress.py"
    spec = importlib.util.spec_from_file_location("inject_progress", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


PLANNED_S = 720.0
_PARAMS = json.dumps({"limits": {"duration_s": PLANNED_S}})


def _inject(db: Database, *, start: float, end: float | None, pid: int = 4242) -> None:
    db.insert_many(
        "fault_injections",
        ("scenario", "ts_start", "ts_end", "pid", "completed", "params"),
        [("handle_leak", start, end, pid, 1 if end else 0, _PARAMS)],
    )


def test_finished_batch_reports_no_remaining_time(db: Database) -> None:
    """**끝난 배치에 "남음"을 적지 않는다.**

    막지 않았으면 무엇이 일어났을 것인가: 13일 전에 끝난 배치가 `남음 약 17분 43초`
    로 발표된다 — 읽는 사람은 뭔가 더 돌 것이 있다고 읽는다(2026-08-15 실측).

    **일찍 끝난 회차를 함께 넣는 것이 이 테스트의 핵심이다.** 전부 계획대로 끝난
    배치만 재면 분모와 분자가 우연히 같아져, 중단 회차를 덜 진행된 것으로 세는
    버그가 그대로 통과한다.
    """
    module = _module()
    now = time.time()
    base = now - 86400 * 13
    _inject(db, start=base, end=base + PLANNED_S)  # 계획대로 끝났다
    _inject(db, start=base + 900, end=base + 900 + 193)  # 겹쳐서 강제 중단 (193초)

    out = module.render(db)

    assert "남음" not in out, f"끝난 배치에 남은 시간을 적었다:\n{out}"
    assert "배치 종료" in out, out
    assert "100%" in out.splitlines()[-1], (
        f"개별 회차는 완료인데 전체가 100% 가 아니다 — 중단 회차를 덜 진행된 것으로 셌다:\n{out}"
    )


def test_running_batch_still_reports_remaining_time(db: Database) -> None:
    """**진행 중이면 남은 시간이 나와야 한다.**

    앞 테스트만 있으면 "남음"을 통째로 지워도 통과한다 — 그러면 이 도구의 본래
    쓸모(배치를 지켜보는 것)가 사라진다.
    """
    module = _module()
    now = time.time()
    _inject(db, start=now - 120, end=None)  # 2분째 진행 중

    out = module.render(db)

    assert "남음" in out, f"진행 중인데 남은 시간이 없다:\n{out}"
    assert "진행 중" in out, out
    assert "배치 종료" not in out, out


def test_header_names_the_batch_date(db: Database) -> None:
    """배치가 **언제 것인지** 헤더에 있어야 한다.

    막지 않았으면: 헤더의 유일한 시각이 "지금"이라, 13일 전 배치를 보면서 지금
    도는 것으로 읽는다. 실제로 그렇게 읽혔다.
    """
    module = _module()
    base = time.time() - 86400 * 13
    _inject(db, start=base, end=base + PLANNED_S)

    header = module.render(db).splitlines()[0]

    assert time.strftime("%m-%d", time.localtime(base)) in header, header
    assert "시작" in header, header

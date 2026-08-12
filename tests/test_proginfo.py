"""프로그램 설명 — "svchost 가 무엇인가".

**여기서 조용히 깨지는 것 셋을 고정한다.**

1. 로케일. 언어 코드를 박아 두면 한글 Windows 의 시스템 파일에서 전부 빈손이 되는데,
   예외가 아니라 **빈 설명**으로 나와 본인 PC 에서는 안 보인다(수집 규칙 5 와 같은 함정).
2. 재조회. 이미 읽은 것을 다시 열면 관측자가 매 회차 수백 개 파일을 연다(설계 규칙 1).
3. 실패 누적. 못 읽는 이름(실측 29%)을 영원히 다시 열면 같은 낭비가 매번 생긴다.
"""

from __future__ import annotations

import sys

import pytest

from argus.collector.proginfo import MAX_ATTEMPTS, ProgramInfoCollector, describe
from argus.storage.hot import Database

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 버전 리소스")


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.db").open()
    yield database
    database.close()


def _events(db: Database, rows: list[tuple[float, str, str | None]]) -> None:
    db.insert_many(
        "process_events",
        ("ts", "event", "pid", "ppid", "name", "exe", "username"),
        [(ts, "start", 1, 0, name, exe, None) for ts, name, exe in rows],
    )


# ---------------------------------------------------------------- 읽기


def test_reads_system_file_description() -> None:
    """**언어를 파일에서 읽어야 한다.**

    `040904b0`(미국 영어)을 박아 두는 흔한 구현은 한글 Windows 의 시스템 파일에서
    빈손이 된다. `cmd.exe` 는 이 PC 에서 "Windows 명령 처리기"로 나오므로, 언어를
    고정한 구현이라면 이 단언이 깨진다.
    """
    found = describe(r"C:\Windows\System32\cmd.exe")
    assert found, "시스템 파일의 버전 리소스를 못 읽었다"
    assert found.get("FileDescription"), f"설명이 비었다: {found}"
    assert found.get("CompanyName") == "Microsoft Corporation"


def test_missing_or_resourceless_file_is_not_an_error(tmp_path) -> None:
    """**실패가 정상 상황이다** — 실측 405종 중 116종(29%)이 버전 리소스가 없었다.

    프로그램 설명 하나 때문에 수집이 멈추면 안 된다(수집 규칙 1).
    """
    assert describe(str(tmp_path / "없는파일.exe")) is None

    plain = tmp_path / "plain.exe"
    plain.write_bytes(b"not really an exe")
    assert describe(str(plain)) is None

    assert describe("") is None


# ---------------------------------------------------------------- 채우기


def test_fills_from_the_latest_known_path(db) -> None:
    """경로는 `process_events` 에 이미 있다 — 새 수집기를 만들지 않는다."""
    _events(db, [(100.0, "cmd", r"C:\Windows\System32\cmd.exe")])

    assert ProgramInfoCollector(db).run_once() == 1

    row = db.query("SELECT * FROM program_info WHERE name = 'cmd'")[0]
    assert row["description"], "설명을 채우지 못했다"
    assert row["company"] == "Microsoft Corporation"


def test_already_described_names_are_not_reopened(db) -> None:
    """**이미 읽은 것을 다시 열지 않는다.**

    매 회차 수백 개 파일을 다시 여는 것은 순수한 낭비다 — exe 가 업데이트돼도
    설명은 거의 바뀌지 않는다.
    """
    _events(db, [(100.0, "cmd", r"C:\Windows\System32\cmd.exe")])
    collector = ProgramInfoCollector(db)
    collector.run_once()

    assert collector._pending() == [], "이미 설명이 있는 이름을 다시 대기열에 넣었다"
    assert collector.run_once() == 0


def test_unreadable_name_is_retried_but_not_forever(db, tmp_path) -> None:
    """못 읽는 이름을 영원히 다시 열면 같은 낭비가 매 회차 생긴다.

    그래도 한 번에 포기하지 않는 이유는 실패가 일시적일 수 있어서다(업데이트 중
    잠긴 순간). 상한을 둔다.
    """
    ghost = tmp_path / "ghost.exe"
    ghost.write_bytes(b"x")
    _events(db, [(100.0, "ghost", str(ghost))])

    collector = ProgramInfoCollector(db)
    for _ in range(MAX_ATTEMPTS):
        assert collector.run_once() == 1, "상한 전에 재시도를 멈췄다"

    assert collector._pending() == [], f"{MAX_ATTEMPTS}번 실패했는데 계속 다시 연다"
    row = db.query("SELECT attempts, description FROM program_info WHERE name = 'ghost'")[0]
    assert row["description"] is None
    assert row["attempts"] == MAX_ATTEMPTS


def test_batch_limits_work_per_tick(db) -> None:
    """**한 회차에 다 하지 않는다**(설계 규칙 1).

    첫 실행에서 405개 = 3.6초가 걸린다. 관측자가 그만큼 한 스레드를 붙들 이유가 없다.
    """
    _events(
        db,
        [(100.0 + i, f"prog{i}", rf"C:\Windows\System32\cmd.exe") for i in range(10)],
    )

    collector = ProgramInfoCollector(db, batch=4)
    assert collector.run_once() == 4, "배치 상한을 넘겨 읽었다"
    assert collector.run_once() == 4
    assert collector.run_once() == 2, "남은 것을 마저 읽지 않았다"
    assert collector.run_once() == 0


def test_only_the_most_recent_path_is_used(db) -> None:
    """같은 이름의 옛 경로가 남아 있어도 최근 것으로 읽는다.

    자동 업데이트로 경로가 갈리면(Discord 는 16일에 세 경로) 옛 경로는 이미 없다.
    """
    _events(
        db,
        [
            (100.0, "cmd", r"C:\없어진경로\cmd.exe"),
            (200.0, "cmd", r"C:\Windows\System32\cmd.exe"),
        ],
    )

    ProgramInfoCollector(db).run_once()
    row = db.query("SELECT description FROM program_info WHERE name = 'cmd'")[0]
    assert row["description"], "옛 경로를 읽어 빈손이 됐다"

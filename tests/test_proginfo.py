"""프로그램 설명 — "svchost 가 무엇인가".

**여기서 조용히 깨지는 것 셋을 고정한다.**

1. 로케일. 언어 코드를 박아 두면 한글 Windows 의 시스템 파일에서 전부 빈손이 되는데,
   예외가 아니라 **빈 설명**으로 나와 본인 PC 에서는 안 보인다(수집 규칙 5 와 같은 함정).
2. 재조회. 이미 읽은 것을 다시 열면 관측자가 매 회차 수백 개 파일을 연다(설계 규칙 1).
3. 실패 누적. 못 읽는 이름(실측 29%)을 영원히 다시 열면 같은 낭비가 매번 생긴다.
"""

from __future__ import annotations

import sys
from datetime import date

import pytest

from argus.collector.proginfo import MAX_ATTEMPTS, ProgramInfoCollector, describe
from argus.storage.hot import Database

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 버전 리소스")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """**데이터 폴더째 격리한다.**

    DB 만 `tmp_path` 로 돌려도 부족하다 — 웜 백필은 `warm_dir()` 을 보고, 그것은
    `%APPDATA%\\Argus\\warm` 을 가리킨다. 처음에 그러고 돌렸더니 테스트가 실제
    파티션을 읽어 이 PC 의 게임 목록이 단언에 튀어나왔다.
    """
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path))
    # **파일명을 `db_path()` 와 맞춘다.** 조회 계층(`dashboard.data`)은 데이터 폴더의
    # `argus.db` 를 여는데, 여기서 `t.db` 를 만들면 둘이 서로 다른 DB 를 보고
    # 조회는 늘 빈손이 된다 — 실패가 "필터가 안 먹는다"처럼 보여 한참 헤맨다.
    database = Database(tmp_path / "argus.db").open()
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


# ---------------------------------------------------------------- 포어그라운드


def _buckets(db: Database, rows: list[tuple[str, float]]) -> None:
    """`process_5m` 에 (이름, 포어그라운드 비율) 몇 개."""
    db.insert_many(
        "process_5m",
        ("ts_5m", "name", "sample_count", "pid_count", "foreground_ratio"),
        [(1000.0 + i * 300, name, 10, 1, ratio) for i, (name, ratio) in enumerate(rows)],
    )


def test_only_foreground_programs_are_marked(db) -> None:
    """**배경 서비스와 사람이 쓰는 프로그램을 가른다.**

    가르지 않으면 사용시간 상위가 전부 svchost·conhost·runtimebroker 다 —
    정의대로 동작한 결과지만 "내가 무엇을 얼마나 했나"의 답은 아니다.
    """
    _buckets(db, [("chrome", 1.0), ("svchost", 0.0), ("league of legends", 0.61)])

    ProgramInfoCollector(db).mark_foreground()

    marked = {
        row["name"]
        for row in db.query("SELECT name FROM program_info WHERE foreground_seen = 1")
    }
    assert marked == {"chrome", "league of legends"}, marked


def test_marking_adds_rows_for_programs_without_a_description(db) -> None:
    """**UPDATE 가 아니라 UPSERT 다.**

    설명을 못 얻은 이름은 `program_info` 에 행 자체가 없다(exe 경로를 못 찾은
    것들 — 실측 427종 중 22종). UPDATE 만 하면 그것들이 조용히 빠지고, 하필 그
    중에 사용자가 쓰는 프로그램이 있을 수 있다.
    """
    _buckets(db, [("설명없는게임", 0.9)])
    assert db.query("SELECT COUNT(*) AS n FROM program_info")[0]["n"] == 0

    ProgramInfoCollector(db).mark_foreground()

    row = db.query("SELECT foreground_seen, description FROM program_info")[0]
    assert row["foreground_seen"] == 1
    assert row["description"] is None, "없던 설명을 지어내면 안 된다"


def test_mark_survives_the_original_rolling_off(db) -> None:
    """**한 번 참이면 계속 참이다.**

    포어그라운드 원본(`process_5m`)은 이틀이 지나면 웜으로 옮겨가 SQLite 에서
    사라진다. 매번 다시 판정하면 사흘 전에 한 게임이 목록에서 빠진다.
    """
    _buckets(db, [("어제한게임", 1.0)])
    collector = ProgramInfoCollector(db)
    collector.mark_foreground()

    with db._lock:  # noqa: SLF001
        db.conn.execute("DELETE FROM process_5m")  # 웜으로 옮겨간 상황
        db.conn.commit()
    collector.mark_foreground()

    row = db.query("SELECT foreground_seen FROM program_info WHERE name = '어제한게임'")[0]
    assert row["foreground_seen"] == 1, "원본이 사라지자 표시를 잃었다"


def test_warm_backfill_runs_only_once(db, monkeypatch) -> None:
    """웜(Parquet) 훑기는 **일회성 백필**이다.

    과거치를 되살리는 것뿐이고 그 뒤로는 핫이 매일 따라잡는다. 10분마다 Parquet
    전체를 읽을 이유가 없다(설계 규칙 1).
    """
    collector = ProgramInfoCollector(db)
    calls: list[int] = []
    monkeypatch.setattr(
        collector, "_foreground_names_warm", lambda: calls.append(1) or {"옛게임"}
    )

    collector.mark_foreground()
    collector.mark_foreground()
    collector.mark_foreground()

    assert len(calls) == 1, f"웜을 {len(calls)}번 훑었다"
    assert db.query("SELECT foreground_seen FROM program_info WHERE name = '옛게임'")[0][
        "foreground_seen"
    ] == 1


# ---------------------------------------------------------------- 제외 목록 배선


def _usage_rows(db: Database, rows: list[tuple[str, float]]) -> None:
    db.insert_many(
        "program_usage_daily",
        ("day", "name", "seconds", "launches", "observed_s"),
        # **날짜를 박아 두지 않는다.** 아래 테스트가 `days=1` 로 조회하는데 그 하한은
        # `date.today()` 다 — 고정 날짜로 두면 다음 날 자정에 픽스처 행이 통째로 잘려
        # 배선과 무관한 이유로 깨진다(2026-08-13 에 실제로 그렇게 됐다).
        [(date.today().isoformat(), name, seconds, 1, 100_000.0) for name, seconds in rows],
    )


def test_excluded_names_come_from_config_not_code(db, monkeypatch) -> None:
    """**제외 목록을 바꾸면 표가 바뀐다**(규칙 3).

    `defaults.yaml` 에 값을 적어 두는 것만으로는 배선이 확인되지 않는다 — 한 군데만
    끊겨도 조용히 코드 기본값으로 돈다. **그래서 기본값이 아닌 목록으로 잰다**:
    기본값으로 재면 코드와 YAML 이 우연히 같아 배선이 끊겨도 통과한다
    (2026-08-04 에 같은 유형을 네 번 겪었다).
    """
    from argus.dashboard import data

    _usage_rows(db, [("chrome", 3600.0), ("나만의도구", 7200.0)])
    with db._lock:  # noqa: SLF001
        db.conn.executemany(
            "INSERT INTO program_info (name, description, company, attempts,"
            " checked_at, foreground_seen) VALUES (?, NULL, NULL, 0, 0, 1)",
            [("chrome",), ("나만의도구",)],
        )
        db.conn.commit()

    # 기본 목록에 없는 이름을 골랐다 — 코드 기본값으로 돌면 이 단언이 깨진다.
    monkeypatch.setattr(data, "usage_exclude", lambda: ("나만의도구",))
    data.program_usage.cache_clear()

    names = [row["name"] for row in data.program_usage(days=1, user_only=True)]
    assert names == ["chrome"], f"제외 목록이 닿지 않았다: {names}"

    data.program_usage.cache_clear()
    everything = [row["name"] for row in data.program_usage(days=1, user_only=False)]
    assert set(everything) == {"chrome", "나만의도구"}, "필터를 껐는데도 걸렀다"
    data.program_usage.cache_clear()


def test_config_exclude_reaches_the_query(db) -> None:
    """`usage_exclude()` 가 실제 설정을 읽는가. 위 테스트는 그 함수를 갈아 끼운다."""
    from argus.dashboard import data

    data.usage_exclude.cache_clear()
    excluded = data.usage_exclude()
    assert "python" in excluded and "windowsterminal" in excluded, excluded
    assert "chrome" not in excluded, "쓰는 프로그램을 기본 제외에 넣었다"


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

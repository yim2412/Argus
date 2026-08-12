"""프로그램이 무엇인지 — exe 버전 리소스에서 사람 말 설명을 읽는다.

**"svchost 238시간"은 정보가 아니다.** 사용시간·프로세스 표에서 상위를 차지하는 것
대부분이 이름만으로는 무엇인지 알 수 없다. Windows 는 그 답을 이미 파일 안에 갖고
있다 — 탐색기 속성 창의 "파일 설명"이 그것이다.

**새 의존성이 없다.** `ctypes` 로 `version.dll` 을 직접 부른다(pywin32 의 같은 기능은
있지만, 이 하나 때문에 배포 의존성을 늘릴 이유가 없다). 로케일도 따라온다 — 한글
Windows 에서는 `dwm` 이 "데스크톱 창 관리자"로 나온다.

**경로는 저장하지 않는다.** 읽을 때만 쓰고 버린다(설계 규칙 5). 014 에서 사용시간
테이블이 같은 판단을 했다.

단독 실행:

    python -m argus.collector.proginfo
"""

from __future__ import annotations

import ctypes
import sys
import time

from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from ..storage.hot import Database

log = get_logger(__name__)

#: 한 회차에 읽을 개수. 실측(2026-08-12) 개당 8.8ms 라 40개면 약 350ms 다.
#: **한 번에 다 하지 않는 이유**는 첫 실행에서 405개 = 3.6초가 걸리기 때문이다 —
#: 관측자가 그만큼 한 스레드를 붙들 이유가 없다(설계 규칙 1). 나눠 읽어도 새 이름은
#: 드물게 생기므로 첫날 안에 다 채워진다.
BATCH = 40

#: 못 읽은 이름을 몇 번까지 다시 열어 보나. 실패의 대부분은 **파일이 이미 없는 것**
#: (설치 제거·임시 실행 파일)이라 다시 열어도 결과가 같다. 그래도 0 이 아닌 이유는
#: 실패가 일시적일 수 있어서다 — 업데이트 중이라 잠긴 순간에 걸리는 경우.
MAX_ATTEMPTS = 3

#: 웜까지 훑는 포어그라운드 백필을 이미 했는지. 일회성이다.
_WARM_BACKFILL_KEY = "foreground_warm_backfill"

_version_dll = None
if sys.platform == "win32":  # pragma: no branch - 배포 대상은 Windows 뿐이다
    try:
        _version_dll = ctypes.WinDLL("version")
    except OSError:  # pragma: no cover - 이 DLL 이 없는 Windows 는 없다
        _version_dll = None


def describe(exe_path: str) -> dict[str, str] | None:
    """exe 의 `FileDescription` · `CompanyName`. 못 읽으면 `None`.

    **실패가 정상 상황이다.** 파일이 이미 지워졌거나(임시 실행 파일), 버전 리소스가
    아예 없는 exe 가 흔하다 — 실측 405종 중 116종(29%)이 그랬다. 그러므로 이 함수는
    예외를 올리지 않는다. 프로그램 설명 하나 때문에 수집이 멈추면 안 된다.
    """
    if _version_dll is None or not exe_path:
        return None
    try:
        size = _version_dll.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not _version_dll.GetFileVersionInfoW(exe_path, 0, size, buffer):
            return None

        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        # **언어를 파일에서 읽어야 한다.** 흔한 구현처럼 `040904b0`(미국 영어)을
        # 박아 두면 한글 Windows 의 시스템 파일에서 전부 빈손이 된다 — 로케일별
        # 실패는 본인 PC 에서 안 보인다(수집 규칙 5 와 같은 함정).
        if not _version_dll.VerQueryValueW(
            buffer, r"\VarFileInfo\Translation", ctypes.byref(pointer), ctypes.byref(length)
        ):
            return None
        language, codepage = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))[0:2]

        found: dict[str, str] = {}
        for key in ("FileDescription", "CompanyName"):
            query = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\{key}"
            if _version_dll.VerQueryValueW(
                buffer, query, ctypes.byref(pointer), ctypes.byref(length)
            ) and length.value:
                # 길이에는 종료 널이 포함돼 있다. 빼지 않으면 문자열 끝에 \x00 이 붙는다.
                value = ctypes.wstring_at(pointer, length.value - 1).strip()
                if value:
                    found[key] = value
        return found or None
    except (OSError, ValueError):  # pragma: no cover - 방어. 경로가 이상한 경우
        return None


class ProgramInfoCollector(Component):
    """아직 설명이 없는 프로그램 이름을 조금씩 채운다.

    **경로는 `process_events` 가 이미 갖고 있다.** 새 수집기를 만들 이유가 없었다 —
    실측(2026-08-12) 이름 427종 중 405종(95%)의 exe 경로가 그 테이블에 있다.

    주기가 길다(10분). 새 프로그램이 뜨는 일은 드물고, 이 값은 하루 늦게 채워져도
    아무 문제가 없다.
    """

    name = "program_info"

    def __init__(self, db: Database, interval_s: float = 600.0, batch: int = BATCH) -> None:
        self.db = db
        self.interval_s = interval_s
        self._batch = batch

    def tick(self) -> None:
        self.run_once()
        self.mark_foreground()

    def mark_foreground(self) -> int:
        """포어그라운드에 있었던 프로그램에 표시한다. 새로 표시한 개수.

        **한 번 참이면 계속 참이다.** 포어그라운드 원본(`process_5m`)은 이틀이
        지나면 웜으로 옮겨가 SQLite 에서 사라지므로, 매번 다시 판정하면 사흘 전에
        한 게임이 목록에서 빠진다.

        핫은 매 회차 본다(싸다). **웜은 처음 한 번만 훑는다** — 과거치를 되살리는
        일회성 백필이고, 그 뒤로는 핫이 매일 따라잡는다. 10분마다 Parquet 전체를
        읽을 이유가 없다(설계 규칙 1).
        """
        names = self._foreground_names_hot()
        if not self._warm_backfilled():
            names |= self._foreground_names_warm()
            self._set_state(_WARM_BACKFILL_KEY, "1")

        if not names:
            return 0

        before = self._marked_count()
        now = time.time()
        # **UPDATE 가 아니라 UPSERT 다.** 설명을 아직 못 얻은 이름은 `program_info` 에
        # 행 자체가 없다(exe 경로를 못 찾은 22종이 그렇다). UPDATE 만 하면 그것들이
        # 조용히 빠지고, 하필 그 중에 사용자가 쓰는 프로그램이 있을 수 있다.
        with self.db._lock:  # noqa: SLF001
            self.db.conn.executemany(
                "INSERT INTO program_info"
                " (name, description, company, attempts, checked_at, foreground_seen)"
                " VALUES (?, NULL, NULL, 0, ?, 1)"
                " ON CONFLICT(name) DO UPDATE SET foreground_seen = 1",
                [(name, now) for name in sorted(names)],
            )
            self.db.conn.commit()
        marked = self._marked_count() - before
        if marked:
            log.info("포어그라운드 프로그램 %d개 표시", marked)
        return marked

    def _marked_count(self) -> int:
        return int(
            self.db.query("SELECT COUNT(*) AS n FROM program_info WHERE foreground_seen = 1")[0]["n"]
        )

    def _foreground_names_hot(self) -> set[str]:
        return {
            row["name"]
            for row in self.db.query(
                "SELECT DISTINCT name FROM process_5m WHERE foreground_ratio > 0"
            )
        }

    def _foreground_names_warm(self) -> set[str]:
        """웜(Parquet)까지 훑는 일회성 백필. 실패해도 조용히 넘어간다 —
        과거치가 조금 늦게 채워질 뿐이고 핫이 매일 따라잡는다."""
        try:
            from ..storage import history

            rows = history._by_day(  # noqa: SLF001 — 병합 규칙을 재구현하지 않는다
                "process",
                "SELECT strftime(to_timestamp(ts_5m), '%Y-%m-%d'), name"
                " FROM warm_process WHERE foreground_ratio > 0",
                "SELECT strftime('%Y-%m-%d', ts_5m, 'unixepoch', 'localtime'), name"
                " FROM process_5m WHERE foreground_ratio > 0",
            )
        except Exception as exc:
            log.debug("웜 포어그라운드 백필 실패 — 핫만 쓴다", extra={"error": str(exc)})
            return set()
        return {name for _day, name in rows}

    def _warm_backfilled(self) -> bool:
        rows = self.db.query(
            "SELECT value FROM program_info_state WHERE key = ?", (_WARM_BACKFILL_KEY,)
        )
        return bool(rows)

    def _set_state(self, key: str, value: str) -> None:
        self.db.insert_many("program_info_state", ("key", "value"), [(key, value)], replace=True)

    def run_once(self) -> int:
        """이번 회차에 채운 개수. 더 채울 것이 없으면 0."""
        pending = self._pending()
        if not pending:
            return 0

        now = time.time()
        rows = []
        for name, exe, attempts in pending:
            found = describe(exe) or {}
            rows.append(
                (
                    name,
                    found.get("FileDescription"),
                    found.get("CompanyName"),
                    attempts + 1,
                    now,
                )
            )

        # **한 번에 쓴다.** 40개를 따로 커밋하면 그동안 수집 쓰기가 40번 밀린다
        # (보존 정리를 청크로 나눈 것과 같은 이유).
        self.db.insert_many(
            "program_info",
            ("name", "description", "company", "attempts", "checked_at"),
            rows,
            replace=True,
        )
        named = sum(1 for row in rows if row[1])
        log.info("프로그램 설명 %d개 조회 (성공 %d)", len(rows), named)
        return len(rows)

    def _pending(self) -> list[tuple[str, str, int]]:
        """아직 설명을 못 얻은 이름과 그 최근 실행 경로.

        **이미 설명이 있는 것은 다시 열지 않는다.** exe 가 업데이트돼도 설명은 거의
        바뀌지 않으므로, 매번 다시 읽는 것은 순수한 낭비다.
        """
        return [
            (row["name"], row["exe"], int(row["attempts"] or 0))
            for row in self.db.query(
                "SELECT e.name AS name, e.exe AS exe,"
                "       COALESCE(i.attempts, 0) AS attempts"
                " FROM process_events e"
                " JOIN (SELECT name, MAX(ts) AS ts FROM process_events"
                "       WHERE exe IS NOT NULL GROUP BY name) latest"
                "   ON e.name = latest.name AND e.ts = latest.ts"
                " LEFT JOIN program_info i ON i.name = e.name"
                " WHERE e.exe IS NOT NULL"
                "   AND (i.name IS NULL OR (i.description IS NULL AND i.attempts < ?))"
                " GROUP BY e.name"
                # **최근에 본 것부터.** 순서를 안 주면 SQLite 가 주는 대로 = 사실상
                # 이름순이라, 첫 회차 40개가 전부 `a` 로 시작하는 옛 프로그램이 된다
                # (실측: 그렇게 채우고 나니 정작 svchost·chrome 이 비어 있었다).
                " ORDER BY latest.ts DESC"
                " LIMIT ?",
                (MAX_ATTEMPTS, self._batch),
            )
        ]


def main() -> int:  # pragma: no cover - 수동 스모크
    """단독 스모크. 다른 수집기와 같은 규약([OK]/[FAIL])을 따른다."""
    from ..paths import db_path

    if not db_path().exists():
        print(f"[FAIL] DB 가 없다: {db_path()}")
        return 1

    db = Database(db_path()).open()
    try:
        collector = ProgramInfoCollector(db)
        pending_before = len(collector._pending())
        filled = collector.run_once()
        rows = db.query(
            "SELECT name, description, company FROM program_info"
            " WHERE description IS NOT NULL ORDER BY checked_at DESC LIMIT 8"
        )
    finally:
        db.close()

    print(f"  대기 {pending_before}개 · 이번 회차 {filled}개")
    for row in rows:
        print(f"  {row['name']:<24} {row['description']}  ({row['company'] or '—'})")
    if filled == 0 and pending_before:
        print("[FAIL] 채울 것이 있는데 하나도 못 채웠다")
        return 1
    print("[OK] proginfo")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

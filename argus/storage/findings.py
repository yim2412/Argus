"""판정에 필요한 것만 담은 스냅샷을 뽑는다 (`--export-findings`).

**다른 기계에서 도는 Argus 를 이 PC 에서 판정하기 위한 것이다.** 2026-08-17 에
두 번째 기계(내장그래픽 노트북)를 붙이면서 만들었다 — 그때까지 모든 수치는
개발 PC(RTX 3080 / 64GB) 한 대에서 나온 것이었고, `PLAN.md` 의 여러 판정이
"근거가 한 대뿐"에 막혀 있었다.

세 가지를 동시에 푼다.

**① 용량.** 이 PC 22일치가 439MB 인데 그 99% 가 초 단위 원본이다
(`process_metrics` 248만 행 · `net_connections` 120만 행). 판정에 쓰는 것은
사건·신호·롤업·자기계측뿐이라 **몇 MB 로 끝난다.** 원본을 굳이 옮길 이유도 없다 —
장기 분석은 웜(Parquet)이 이미 담당하고, 원본은 24시간이면 지워진다.

**② 개인정보.** `net_connections` 에는 네트워크 목적지가 그대로 들어 있다
(설계 규칙 5). **안 담는 것이 가장 확실한 익명화다.** `net_activity_5m` 은
목적지가 아니라 개수(`distinct_remotes`)만 세므로 담아도 된다.

**③ WAL 일관성.** `.db` 파일만 복사하면 `-wal` 에 든 최신 커밋이 통째로 빠진다.
세 파일을 다 복사해도 그 사이 쓰기가 끼면 어긋난다. 여기서는 **읽기 트랜잭션
하나 안에서** 전부 복사하므로, 상주가 계속 쓰는 중에도 한 시점의 일관된 모습이
나온다(WAL 은 읽기가 쓰기를 막지 않는다).

**쓰지 않는다.** 상주가 도는 중에 별도 프로세스로 실행되므로 원본에 대해서는
읽기만 한다. 마이그레이션도 돌리지 않는다 — 상주보다 낮은 버전의 실행 파일이
남의 DB 스키마를 건드리는 일이 없어야 한다.
"""

from __future__ import annotations

import json
import platform
import sqlite3
import time
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..paths import db_path, machine_profile_path

log = get_logger(__name__)

# 담는 것. **판정에 실제로 쓰는 테이블만** 둔다.
#
# 순서에 의미는 없지만 묶어서 적는다 — 새 테이블이 생겼을 때 어느 묶음에
# 들어가는지 판단하는 자리가 된다.
INCLUDED_TABLES: tuple[str, ...] = (
    # 사건과 신호 — 판정의 본체
    "incidents",
    "incident_signals",
    "anomaly_signals",
    # 관측자 자신 — 예산(CPU 2% / RSS 300MB)과 자동 스로틀이 실제로 도는지.
    # **보존이 7일뿐이라 회수 주기를 정하는 것이 이 테이블이다.**
    "self_telemetry",
    # 롤업 — 원본이 지워진 뒤의 장기 시계열
    "metrics_1m",
    "process_5m",
    "net_activity_5m",
    "program_usage_daily",
    "daily_report",
    # 프로그램 지문·정보
    "process_fingerprint",
    "program_info",
    "program_info_state",
    # 사건 밖의 사실 — 절전·시각 변경·비정상 종료
    "system_events",
    # 결함 주입 라벨과 채점 이력 (노트북에서 주입할 일은 없지만, 있으면 담는다)
    "fault_injections",
    "eval_runs",
    # 진행 상태 — 롤업·웜이 어디까지 갔는지. 데이터가 비는 이유를 여기서 읽는다
    "meta",
    "rollup_state",
    "warm_exports",
)

# 일부러 뺀 것. **이유를 같이 적는다** — 나중에 "왜 이게 없지?" 를 물을 때
# 코드를 뒤지지 않게, 그리고 무심코 다시 넣지 않게.
EXCLUDED_TABLES: dict[str, str] = {
    "metrics_raw": "초 단위 원본. 용량의 대부분이고 24시간이면 지워진다 — 웜이 대신한다",
    "process_metrics": "초 단위 원본. 248만 행(이 PC 22일 기준)",
    "process_events": "프로세스 생성·종료 원본. 25만 행",
    "net_connections": "**네트워크 목적지가 들어 있다.** 설계 규칙 5 — 담지 않는 것이 익명화다",
}

# 목적지에만 만드는 표. 이 스냅샷이 무엇인지 스스로 말하게 한다.
#
# **파일 하나로 끝내기 위한 것이다.** `machine_profile.json` 을 따로 들고 다니면
# 짝이 어긋나고, 어긋난 순간 "이 수치가 어느 기계 것인가"를 알 수 없게 된다 —
# 두 기계를 비교하려고 만든 물건에서 그건 치명적이다.
_META_DDL = """
CREATE TABLE IF NOT EXISTS export_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r[0]) for r in rows}


def resolve_out_path(target: str | Path) -> Path:
    """대상이 폴더면 그 안에 날짜가 든 이름을 짓는다.

    **파일명이 곧 하트비트다.** 회수 폴더를 열었을 때 마지막 파일이 며칠 전인지가
    그 기계가 언제까지 살아 있었는지다 — 별도의 생존 신호를 만들 필요가 없다.
    그래서 매일 같은 이름으로 덮어쓰면 안 된다.

    호스트명을 넣는 이유는 기계가 셋 이상이 될 때다. 두 대일 때도 섞이면
    어느 쪽 것인지 파일을 열어 봐야 안다.
    """
    target = Path(target)
    # `.db` 로 끝나면 파일을 지정한 것으로 본다. 그 외에는 폴더로 본다 —
    # 아직 없는 폴더일 수 있으므로 `is_dir()` 만으로 판단하지 않는다.
    if target.suffix.lower() == ".db":
        return target

    host = platform.node() or "unknown"
    # 파일명에 못 쓰는 글자를 걷어낸다. 한글 호스트명은 그대로 두어도 되지만
    # 공유 폴더를 거치므로 보수적으로 간다.
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in host)[:32]
    stamp = time.strftime("%Y-%m-%d")
    return target / f"argus-{safe}-{stamp}.db"


def _machine_profile() -> str:
    """이 스냅샷이 어느 기계 것인지. 없으면 빈 객체 — 실패시키지 않는다."""
    try:
        return machine_profile_path().read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("machine_profile 을 읽지 못했다", extra={"error": str(exc)})
        return "{}"
    except UnicodeDecodeError as exc:
        # UTF-8 strict 디코딩은 CP949 보다 쉽게 터진다. 스냅샷 전체를 날릴 이유는 없다.
        log.warning("machine_profile 디코딩 실패", extra={"error": str(exc)})
        return "{}"


def export_findings(out_path: Path, source: Path | None = None) -> dict[str, Any]:
    """판정용 테이블만 새 SQLite 파일로 뽑는다.

    상주가 돌고 있어도 안전하다 — 원본은 읽기만 하고, 읽기 트랜잭션 하나 안에서
    전부 복사하므로 한 시점의 일관된 모습이 나온다.

    반환값은 무엇이 얼마나 담겼는지다. **호출자가 이것을 사람이 읽을 수 있게
    남겨야 한다** — 배포된 exe 는 콘솔이 없어 화면으로는 아무것도 안 보인다.
    """
    started = time.time()
    src = source or db_path()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 같은 이름이 남아 있으면 지운다. 이어붙이면 지난 회차의 행이 섞여
    # "언제 것인지 모르는 스냅샷"이 된다.
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(out_path) + suffix)
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(str(src), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        present = _table_names(conn)

        # 목적지를 붙인다. 파라미터 바인딩이 되는 자리다(경로에 따옴표가 들어가도 안전).
        conn.execute("ATTACH DATABASE ? AS snap", (str(out_path),))

        counts: dict[str, int] = {}
        missing: list[str] = []

        # **여기서부터 한 트랜잭션이다.** 첫 읽기에서 스냅샷이 고정되고 COMMIT
        # 까지 유지된다. 원본에는 쓰지 않으므로 상주의 수집은 계속 진행된다.
        conn.execute("BEGIN")
        try:
            conn.execute(_META_DDL.replace("export_meta", "snap.export_meta"))

            for table in INCLUDED_TABLES:
                if table not in present:
                    # 스키마 버전이 다른 기계일 수 있다. 없는 것은 건너뛰되
                    # **조용히 넘기지 않는다**(설계 규칙 4) — 결과에 남긴다.
                    missing.append(table)
                    continue

                ddl_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if ddl_row is None or not ddl_row[0]:
                    missing.append(table)
                    continue

                # 원본 DDL 을 그대로 쓰되 목적지 스키마로 돌린다. 컬럼 순서·타입·
                # 제약이 원본과 같아야 이 PC 의 조회 코드가 그대로 돈다.
                ddl = str(ddl_row[0])
                ddl = ddl.replace("CREATE TABLE ", "CREATE TABLE snap.", 1)
                conn.execute(ddl)
                conn.execute(f'INSERT INTO snap."{table}" SELECT * FROM main."{table}"')
                counts[table] = int(
                    conn.execute(f'SELECT COUNT(*) FROM snap."{table}"').fetchone()[0]
                )

            meta = {
                "exported_at": str(time.time()),
                "exported_at_local": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source_db": str(src),
                "schema_version": str(
                    conn.execute("PRAGMA user_version").fetchone()[0]
                ),
                "machine_profile": _machine_profile(),
                "excluded": json.dumps(EXCLUDED_TABLES, ensure_ascii=False),
                "missing": json.dumps(missing, ensure_ascii=False),
            }
            conn.executemany(
                "INSERT OR REPLACE INTO snap.export_meta (key, value) VALUES (?, ?)",
                list(meta.items()),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        conn.execute("DETACH DATABASE snap")
    finally:
        conn.close()

    size = out_path.stat().st_size if out_path.exists() else 0
    result = {
        "path": str(out_path),
        "size_bytes": size,
        "tables": counts,
        "missing": missing,
        "rows_total": sum(counts.values()),
        "elapsed_s": round(time.time() - started, 3),
    }
    log.info(
        "스냅샷을 내보냈다",
        extra={
            "path": str(out_path),
            "size_mb": round(size / (1024 * 1024), 2),
            "rows": result["rows_total"],
            "elapsed_s": result["elapsed_s"],
        },
    )
    return result


def prune_snapshots(directory: Path, keep: int) -> list[str]:
    """오래된 스냅샷을 지운다. 매일 쌓이므로 상한이 없으면 계속 는다.

    **이름이 아니라 mtime 으로 고른다.** 파일명 규칙이 나중에 바뀌어도 정리가
    계속 돌아야 한다.
    """
    if keep < 1:
        return []
    directory = Path(directory)
    if not directory.is_dir():
        return []

    files = sorted(
        (p for p in directory.glob("*.db") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed.append(stale.name)
        except OSError as exc:
            # 지우기 실패가 내보내기를 실패시키면 안 된다. 다음 회차가 다시 시도한다.
            log.warning("오래된 스냅샷 삭제 실패", extra={"file": stale.name, "error": str(exc)})
    if removed:
        log.info("오래된 스냅샷 정리", extra={"removed": len(removed)})
    return removed

"""웜의 초 단위 원본을 `Database` 와 **같은 얼굴**로 내놓는다 — 과거 구간 리플레이용.

`Replayer` 는 `db.query(sql, params)` 로 네 테이블을 읽는다. 그래서 읽는 곳만 바꿔
끼우면 **탐지 경로를 한 줄도 고치지 않고** 과거를 재생할 수 있다. 리플레이 전용
분기를 탐지기 안에 만들면 그 순간 "실시간에서만 되는 일"이 생기고 채점이 성립하지
않는다(CLAUDE.md — 실시간과 리플레이는 같은 경로를 쓴다).

**원본 세 테이블만 웜에서 읽는다.** `system_events`(공백 판정)·`fault_injections` 는
SQLite 에 30일 남아 있고 양도 적어 그대로 핫에서 읽는다. 웜에 없는 것을 굳이 웜에
넣으면 내보내기 대상만 늘고 얻는 것이 없다.

**핫과 웜을 섞지 않는다.** 한 구간을 두 곳에서 합쳐 읽으면 겹치는 시각의 행이 두 번
들어와 관측이 중복된다 — 그건 조용히 틀리는 종류의 버그다. 어느 쪽을 쓸지는 호출자가
날짜로 고른다(`for_day`).

**파라미터는 리터럴로 박는다.** DuckDB 는 값을 바인딩하면 `read_parquet` 의 필터
푸시다운을 못 해 Parquet 을 통째로 메모리에 올린다 — 실측 +416MB / 0.36초 대
+18MB / 0.06초. 숫자만 허용하는 `warm._inline_params` 를 그대로 쓴다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..logging_setup import get_logger
from .hot import Database
from .warm import RAW_KINDS, SOURCES, _inline_params, partition_path

log = get_logger(__name__)

#: SQLite 테이블 이름 → 웜의 종류. 이 셋만 웜에서 읽는다.
TABLE_TO_KIND: dict[str, str] = {SOURCES[k].table: k for k in RAW_KINDS}


class WarmReplayDatabase:
    """웜 파티션을 읽되 `Database.query()` 와 같은 시그니처를 갖는다.

    핫 DB 를 함께 받는 이유는 웜에 없는 테이블(`system_events` 등) 때문이다.
    """

    def __init__(self, hot: Database, days: list[str]) -> None:
        if not days:
            raise ValueError("리플레이할 날짜가 없다")
        self.hot = hot
        self.days = sorted(days)
        self._con: Any = None

    # -------------------------------------------------------------- 생성자

    @classmethod
    def for_day(cls, hot: Database, day: str) -> "WarmReplayDatabase":
        """하루치. 앞뒤 경계를 위해 **전날도 함께 연다.**

        룰의 지속 조건과 베이스라인 창(기본 30분)이 자정을 넘어 뒤를 본다. 그날
        파일만 열면 00:00 직후 구간은 베이스라인이 서지 않아 **하루의 앞부분이
        조용히 판정에서 빠진다** — 그건 "안 잡혔다"로 보이지 재현 실패로 보이지 않는다.
        """
        d = date.fromisoformat(day)
        prev = date.fromordinal(d.toordinal() - 1).isoformat()
        days = [x for x in (prev, day) if any(
            partition_path(x, kind).exists() for kind in RAW_KINDS
        )]
        if day not in days:
            raise FileNotFoundError(f"웜에 {day} 의 초 단위 원본이 없다")
        return cls(hot, days)

    @staticmethod
    def available_days() -> list[str]:
        """초 단위 원본이 **세 종류 다** 있는 날짜만. 하나라도 없으면 재현이 반쪽이다."""
        from .warm import partition_days

        sets = [set(partition_days(kind)) for kind in RAW_KINDS]
        return sorted(set.intersection(*sets)) if sets else []

    # ---------------------------------------------------------------- 조회

    def _connection(self) -> Any:
        if self._con is None:
            import duckdb

            self._con = duckdb.connect()
        return self._con

    def _files(self, kind: str) -> list[str]:
        return [
            partition_path(d, kind).as_posix()
            for d in self.days
            if partition_path(d, kind).exists()
        ]

    def query(self, sql: str, params: Any = None) -> list[dict]:
        """`Database.query` 와 같은 규약. 행은 `dict` 다 (`row["x"]`·`.keys()`·`dict(row)`)."""
        params = list(params) if params else []
        table = _table_of(sql)
        kind = TABLE_TO_KIND.get(table or "")
        if kind is None:
            return self.hot.query(sql, tuple(params))

        files = self._files(kind)
        if not files:
            # 그 날짜에 그 종류가 없다. 빈 결과가 맞다 — GPU 없는 PC 의 `raw_gpu` 등.
            return []

        # `SELECT *` 는 컬럼 순서를 파일에 맡기게 되므로 명시 목록으로 바꾼다.
        # 파일마다 컬럼이 갈리면 조회가 통째로 깨지는데, 그건 아주 늦게 드러난다.
        columns = ", ".join(f'"{c}"' for c in SOURCES[kind].columns)
        source = "read_parquet([{}])".format(", ".join(f"'{f}'" for f in files))
        rewritten = re.sub(
            rf"\bSELECT\s+\*\s+FROM\s+{re.escape(table)}\b",
            f"SELECT {columns} FROM {source}",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        if rewritten == sql:  # `SELECT a, b FROM t` 형태
            rewritten = re.sub(
                rf"\bFROM\s+{re.escape(table)}\b", f"FROM {source}", sql,
                count=1, flags=re.IGNORECASE,
            )

        statement = _inline_params(rewritten, params)
        cursor = self._connection().execute(statement)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "WarmReplayDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _table_of(sql: str) -> str | None:
    match = re.search(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", sql, flags=re.IGNORECASE)
    return match.group(1) if match else None


if __name__ == "__main__":  # 스모크: python -m argus.storage.replay_db
    from ..logging_setup import setup

    setup(level="WARNING")
    days = WarmReplayDatabase.available_days()
    print(f"  웜에 초 단위 원본이 있는 날짜: {len(days)}일 {days[-3:] if days else ''}")
    if not days:
        print("  (아직 내보낸 원본이 없다 — 상주가 하루 지난 날짜부터 내보낸다)")
        print("[OK] storage.replay_db (내보낸 원본 없음)")
        raise SystemExit(0)

    with Database() as hot, WarmReplayDatabase.for_day(hot, days[-1]) as warm:
        rows = warm.query(
            "SELECT * FROM metrics_raw WHERE ts BETWEEN ? AND ? ORDER BY ts LIMIT 3",
            (0, 9_999_999_999),
        )
        print(f"  metrics_raw 표본 {len(rows)}행, 컬럼 {len(rows[0]) if rows else 0}개")
        procs = warm.query(
            "SELECT ts, pid, name FROM process_metrics WHERE ts BETWEEN ? AND ? LIMIT 3",
            (0, 9_999_999_999),
        )
        print(f"  process_metrics 표본 {len(procs)}행")
        # 웜에 없는 테이블은 핫으로 넘어가야 한다
        events = warm.query("SELECT ts FROM system_events WHERE ts BETWEEN ? AND ?", (0, 9e9))
        print(f"  system_events(핫 위임) {len(events)}행")

    problems = []
    if not rows:
        problems.append("metrics_raw 를 하나도 읽지 못했다")
    elif "cpu_total" not in rows[0]:
        problems.append(f"컬럼이 빠졌다: {sorted(rows[0])[:6]}")
    if not procs:
        problems.append("process_metrics 를 하나도 읽지 못했다")
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] storage.replay_db")

"""억제 — 같은 일을 두 번 말하지 않는다.

디스크 병목을 알리는 중에 "Chrome CPU 높음"을 따로 알리면, 사용자는 두 개의 문제가
있다고 읽는다. 실제로는 하나이고 두 번째는 첫 번째의 증상이다.

**억제한 사건을 지우지 않는다.** `suppressed_by` 로 표시만 한다. 왜 안 알렸는지
설명할 수 있어야 하고, 나중에 억제 규칙이 틀렸는지 검증하려면 기록이 남아야 한다.
조용히 사라지는 것은 디버깅할 수 없다.
"""

from __future__ import annotations

from ..logging_setup import get_logger
from ..storage.hot import Database

log = get_logger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def apply_suppression(db: Database, incident_id: int, *, overlap_s: float = 0.0) -> int | None:
    """이 사건을 묻어야 할 상위 사건이 있으면 그 id 를 돌려주고 표시한다.

    상위의 조건은 둘이다.
    - 시간이 겹친다 (같은 시각에 일어난 일이어야 같은 일일 수 있다)
    - 심각도가 더 높다. 같으면 **먼저 시작한 쪽**이 상위다 — 나중 것이 결과일
      가능성이 크기 때문이다.

    병목 종류가 같은지는 보지 않는다. CPU 병목이 디스크 병목을 유발하는 경우가 있고,
    그때 둘 다 알리는 것이 정확히 피하려는 상황이다.
    """
    rows = db.query("SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not rows:
        return None
    target = dict(rows[0])
    if target["ts_end"] is None:
        return None  # 진행 중인 것은 판단하지 않는다

    rank = SEVERITY_RANK.get(target["severity"], 0)
    candidates = db.query(
        "SELECT id, severity, ts_start, ts_end FROM incidents "
        "WHERE id != ? AND suppressed_by IS NULL "
        "AND ts_start <= ? AND COALESCE(ts_end, ?) >= ? "
        "ORDER BY ts_start",
        (
            incident_id,
            target["ts_end"] - overlap_s,
            target["ts_end"],
            target["ts_start"] + overlap_s,
        ),
    )

    for row in candidates:
        other_rank = SEVERITY_RANK.get(row["severity"], 0)
        if other_rank > rank or (other_rank == rank and row["ts_start"] < target["ts_start"]):
            with db._lock:  # noqa: SLF001
                db.conn.execute(
                    "UPDATE incidents SET suppressed_by = ? WHERE id = ?", (row["id"], incident_id)
                )
                db.conn.commit()
            log.info("사건 억제", extra={"incident": incident_id, "by": row["id"]})
            return int(row["id"])
    return None

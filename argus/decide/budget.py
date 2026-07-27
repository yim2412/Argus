"""알림 예산 — 하루에 몇 번까지 말을 걸 것인가.

**오탐 3번이면 사용자는 알림을 끄고, 그 순간 제품은 죽는다.** 정탐이어도 마찬가지다.
하루 30번 맞는 말을 하는 도구와 하루 3번 맞는 말을 하는 도구 중 후자가 살아남는다.

예산을 다 쓰면 조용해지는 것이 아니라 **기준을 올린다.** 남은 것은 대시보드에만
남기고, 그보다 심각한 것은 여전히 알린다. 진짜 중요한 일이 예산 때문에 묻히면
예산 자체가 위험해진다.

여기서 발송하지 않는다 — 보낼지 말지만 정한다. 발송 경로는 오탐률이 검증된 뒤에
붙인다(CLAUDE.md: 알림은 되돌릴 수 없다).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..logging_setup import get_logger
from ..storage.hot import Database

log = get_logger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class Decision:
    """알릴 것인가, 아니라면 왜 아닌가."""

    notify: bool
    reason: str = ""


@dataclass
class NotificationBudget:
    """하루 알림 총량과 심각도 컷."""

    per_day: int = 8
    # 대시보드에만 남기는 하한. info 는 애초에 알리지 않는다.
    min_severity: str = "warning"

    def used_today(self, db: Database, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        day_start = now - (now % 86400)
        rows = db.query(
            "SELECT COUNT(*) AS c FROM incidents WHERE notified = 1 AND ts_start >= ?",
            (day_start,),
        )
        return int(rows[0]["c"]) if rows else 0

    def effective_severity(self, db: Database, now: float | None = None) -> str:
        """예산을 얼마나 썼는지에 따라 기준을 올린다."""
        used = self.used_today(db, now)
        if used < self.per_day:
            return self.min_severity
        # 예산 소진 — critical 만 통과시킨다.
        return "critical"

    def decide(self, db: Database, incident: dict, now: float | None = None) -> Decision:
        if incident.get("suppressed_by"):
            return Decision(False, "상위 사건에 묻힘")

        severity = incident.get("severity") or "info"
        threshold = self.effective_severity(db, now)
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(threshold, 1):
            used = self.used_today(db, now)
            if used >= self.per_day:
                return Decision(False, f"하루 예산 {self.per_day}건 소진 — 기준을 critical 로 올림")
            return Decision(False, f"심각도 {severity} < {threshold}")

        return Decision(True)

    def record(self, db: Database, incident_id: int, decision: Decision) -> None:
        """판단 결과를 남긴다. **안 보낸 이유도 남긴다** — 조용히 사라지면 왜 알림이
        오지 않았는지 아무도 설명할 수 없다."""
        with db._lock:  # noqa: SLF001
            db.conn.execute(
                "UPDATE incidents SET notified = ?, notify_skipped = ? WHERE id = ?",
                (1 if decision.notify else 0, decision.reason or None, incident_id),
            )
            db.conn.commit()

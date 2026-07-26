"""배치 writer — 큐를 비워 DB 에 넣는다.

쓰기를 이 스레드 하나가 전담한다. 수집기들은 큐에 넣기만 하므로 서로도, DB 와도
경합하지 않는다. SQLite 는 쓰기가 직렬화되므로 writer 를 여럿 두면 손해만 본다.

행을 모아서 넣는 이유: 커밋 한 번의 고정 비용이 행 하나 넣는 비용보다 훨씬 크다.
1Hz 수집기 5개가 각자 커밋하면 초당 5회 커밋이지만, 200ms 배치면 초당 5회로 같되
그 안의 행이 뭉쳐 있어 훨씬 싸다.
"""

from __future__ import annotations

import time
from collections import defaultdict

from ..logging_setup import get_logger
from ..runtime.stats import STATS
from ..runtime.supervisor import Component
from .hot import Database
from .queue import Sample, SampleQueue

log = get_logger(__name__)


class BatchWriter(Component):
    """주기적으로 큐를 비워 테이블별로 묶어 삽입한다."""

    name = "writer"
    # 스로틀이 걸렸다는 것은 부하가 크다는 뜻인데, 그때 쓰기를 늦추면 큐가 넘쳐
    # 데이터를 잃는다. 쓰기는 오히려 제때 비워야 한다.
    throttleable = False

    def __init__(
        self,
        db: Database,
        queue: SampleQueue,
        *,
        flush_interval_ms: int = 200,
        flush_max_rows: int = 500,
    ) -> None:
        self.db = db
        self.queue = queue
        self.interval_s = flush_interval_ms / 1000.0
        self.max_rows = flush_max_rows
        self._written = 0

    def flush_once(self) -> int:
        """한 번 비운다. 삽입한 행 수를 돌려준다."""
        batch = self.queue.drain(self.max_rows)
        if not batch:
            return 0

        # (테이블, 컬럼 조합) 별로 묶는다. 같은 테이블이라도 컬럼 구성이 다르면
        # 별도 executemany 가 되어야 한다.
        groups: dict[tuple[str, tuple[str, ...]], list[tuple]] = defaultdict(list)
        for sample in batch:
            groups[(sample.table, sample.columns)].append(sample.values)

        started = time.perf_counter()
        total = 0
        for (table, columns), rows in groups.items():
            try:
                total += self.db.insert_many(table, columns, rows)
            except Exception:
                # 한 테이블의 삽입 실패가 다른 테이블까지 막지 않게 한다.
                # 실패한 행은 버린다 — 되돌려 넣으면 같은 오류로 큐가 영영 막힌다.
                log.exception("배치 삽입 실패", extra={"table": table, "rows": len(rows)})
        elapsed_ms = (time.perf_counter() - started) * 1000

        STATS.record_write_latency(elapsed_ms)
        self._written += total
        if elapsed_ms > 500:
            log.warning(
                "DB 쓰기가 느리다", extra={"elapsed_ms": round(elapsed_ms, 1), "rows": total}
            )
        return total

    def tick(self) -> None:
        self.flush_once()

    def teardown(self) -> None:
        """종료 시 남은 것을 모두 비운다. 데이터를 들고 죽지 않기 위함."""
        drained = 0
        while True:
            n = self.flush_once()
            drained += n
            if n == 0:
                break
        if drained:
            log.info("종료 전 잔여 데이터 기록", extra={"rows": drained})

    @property
    def written(self) -> int:
        return self._written


if __name__ == "__main__":  # 스모크: python -m argus.storage.writer
    from ..logging_setup import setup

    setup(level="WARNING")
    with Database() as db:
        before = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]
        q = SampleQueue(maxsize=1000)
        now = time.time()
        for i in range(250):
            q.put(Sample("self_telemetry", ("ts", "cpu_percent"), (now + i, float(i))))

        writer = BatchWriter(db, q, flush_max_rows=100)
        rounds = 0
        while q.depth:
            writer.flush_once()
            rounds += 1
        after = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]

        print(f"  250행 투입 → {rounds}회 배치로 기록 (누적 {before} -> {after})")
        print(f"  마지막 쓰기 지연: {STATS.snapshot().write_latency_ms:.2f}ms")
        # 방금 넣은 테스트 행은 지운다
        db.conn.execute("DELETE FROM self_telemetry WHERE ts >= ?", (now,))
        db.conn.commit()
        if after - before != 250:
            print("[FAIL] 삽입 행 수가 맞지 않는다")
            raise SystemExit(1)
    print("[OK] storage.writer")

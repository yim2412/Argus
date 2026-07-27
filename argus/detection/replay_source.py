"""저장된 관측을 뒤에서 따라 읽는다 — 실시간 탐지의 입력.

`Replayer` 는 "구간을 재생"하는 도구라 끝이 정해져 있다. 실시간은 끝이 없고, 같은
행을 두 번 읽으면 안 되며, 아직 안 쌓인 구간을 건드려서도 안 된다. 그 커서 관리만
여기로 분리했다 — 관측을 만드는 일 자체는 `Replayer` 가 그대로 한다.

**뒤늦게 도착하는 행이 핵심 문제다.** 수집기는 큐에 넣고 writer 가 배치로 쓰므로,
`now` 까지 읽으면 방금 수집됐지만 아직 안 쓰인 행을 영영 건너뛴다. 그래서 `lag_s`
만큼 물러선 지점까지만 읽는다.
"""

from __future__ import annotations

import time

from ..eval.replay import Replayer, Window
from ..logging_setup import get_logger
from ..storage.hot import Database

log = get_logger(__name__)

# 한 번에 처리할 최대 관측 수. 오래 멈췄다 재개할 때 한 번에 몰아 읽으면
# 이 틱이 길어져 예산을 넘긴다. 나눠서 따라잡는다.
MAX_BATCH = 600


class ObservationTail:
    """마지막으로 읽은 지점 뒤부터 새 관측만 돌려준다."""

    def __init__(self, db: Database, *, lag_s: float = 5.0, start_ts: float | None = None) -> None:
        self.db = db
        self.lag_s = lag_s
        self.replayer = Replayer(db)
        # 시작 시점보다 과거는 보지 않는다. 워밍이 이미 그 구간을 학습에 썼고,
        # 다시 탐지에 넣으면 기동할 때마다 과거 이상이 새 알림으로 되살아난다.
        self._cursor = start_ts if start_ts is not None else time.time() - lag_s

    @property
    def cursor(self) -> float:
        return self._cursor

    def skip_to_now(self) -> None:
        """커서를 현재로 당긴다. 절전 복귀 후 밀린 구간을 소급 판정하지 않는다."""
        self._cursor = time.time() - self.lag_s

    def next_batch(self) -> list:
        horizon = time.time() - self.lag_s
        if horizon <= self._cursor:
            return []

        window = Window(self._cursor, horizon)
        try:
            observations = list(self.replayer.stream(window))
        except Exception as exc:
            # 읽기 실패가 상주 프로세스를 죽이면 안 된다. 다음 틱에 다시 시도한다.
            log.warning("관측 읽기 실패", extra={"error": str(exc)})
            return []

        # `stream` 은 경계를 포함하므로 직전에 처리한 행이 다시 나올 수 있다.
        observations = [o for o in observations if o.ts > self._cursor]
        if not observations:
            self._cursor = horizon
            return []

        if len(observations) > MAX_BATCH:
            observations = observations[:MAX_BATCH]
            log.debug("따라잡는 중", extra={"batch": len(observations)})

        self._cursor = observations[-1].ts
        return observations


if __name__ == "__main__":  # 스모크: python -m argus.detection.replay_source
    with Database() as db:
        # 과거 60초를 커서 시작점으로 잡아 실제로 뭔가 읽히는지 본다
        tail = ObservationTail(db, lag_s=2.0, start_ts=time.time() - 60)
        first = tail.next_batch()
        print(f"  1회차: 관측 {len(first)}개, 커서 {tail.cursor:.1f}")

        second = tail.next_batch()
        print(f"  2회차: 관측 {len(second)}개 (같은 행을 다시 주면 안 된다)")

        overlap = {o.ts for o in first} & {o.ts for o in second}
        print(f"  중복: {len(overlap)}건")

        tail.skip_to_now()
        after_skip = tail.next_batch()
        print(f"  skip_to_now 후: {len(after_skip)}개")

    problems = []
    if not first:
        problems.append("최근 60초에서 관측을 하나도 읽지 못했다 — 수집이 도는지 확인할 것")
    if overlap:
        problems.append(f"같은 관측을 두 번 돌려줬다: {len(overlap)}건")
    if len(after_skip) > 5:
        problems.append(f"skip_to_now 가 과거를 건너뛰지 않았다: {len(after_skip)}개")
    for p in problems:
        print(f"[FAIL] {p}")
    if problems:
        raise SystemExit(1)
    print("[OK] detection.replay_source")

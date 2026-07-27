"""리플레이 하네스 — 저장된 데이터를 관측 스트림으로 되돌린다.

**왜 필요한가**: 탐지기를 고칠 때마다 며칠을 기다릴 수는 없다. 메모리 누수를 30분에
걸쳐 주입하는 시나리오를 실시간으로만 검증하면 한 번 돌리는 데 30분이다. 저장된
데이터를 다시 흘리면 그 30분을 몇 초에 재현할 수 있고, **같은 입력에 대해 항상 같은
결과**가 나오므로 탐지기 A 와 B 를 공정하게 비교할 수 있다.

**설계상 중요한 것 두 가지**

1. **시각은 데이터에서 온다.** 재생 중에 `time.time()` 을 부르면 오늘 시각이 나오고,
   탐지기의 시간 기반 판단이 전부 틀어진다. `Observation.ts` 는 항상 저장된 시각이다.
2. **공백 구간은 표시해서 넘긴다.** 절전으로 몇 시간 끊긴 구간을 그냥 이어 붙이면
   "메모리가 3시간 만에 급증했다"처럼 보인다. `system_events` 의 `time_gap` 을 읽어
   해당 관측에 `suspect` 를 세운다.

프로세스 메트릭은 `tier 1`(활성 집합, 1초)과 `tier 2`(전체, 30초)가 섞여 있다. 같은
타임스탬프의 프로세스 행들을 그 시각의 관측에 묶는다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator

from ..detection.base import Observation, ProcessView
from ..logging_setup import get_logger
from ..storage.hot import Database

log = get_logger(__name__)

# 이 시간 이상 떨어진 두 관측 사이는 연속으로 보지 않는다.
DEFAULT_GAP_TOLERANCE_S = 10.0


@dataclass(frozen=True)
class Window:
    """재생할 시간 구간."""

    start: float
    end: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)

    def __str__(self) -> str:
        fmt = "%Y-%m-%d %H:%M:%S"
        return (
            f"{time.strftime(fmt, time.localtime(self.start))} ~ "
            f"{time.strftime(fmt, time.localtime(self.end))} "
            f"({self.duration_s / 60:.1f}분)"
        )


class Replayer:
    """DB 구간을 `Observation` 스트림으로 바꾼다."""

    def __init__(self, db: Database, *, gap_tolerance_s: float = DEFAULT_GAP_TOLERANCE_S) -> None:
        self.db = db
        self.gap_tolerance_s = gap_tolerance_s
        self._stats: dict[str, int] = {}

    # ------------------------------------------------------------------ 구간

    def available_window(self) -> Window | None:
        """저장된 데이터의 전체 범위."""
        row = self.db.query("SELECT MIN(ts) AS a, MAX(ts) AS b FROM metrics_raw")[0]
        if row["a"] is None:
            return None
        return Window(row["a"], row["b"])

    def window_around_injections(self, *, padding_s: float = 120.0) -> Window | None:
        """주입 구간 전체를 감싸는 창.

        앞뒤로 여유를 둔다. 정상 구간이 있어야 오탐률을 잴 수 있기 때문이다 —
        결함 구간만 재생하면 "전부 이상"이라고 답하는 탐지기가 만점을 받는다.
        """
        rows = self.db.query(
            "SELECT MIN(ts_start) AS a, MAX(ts_end) AS b FROM fault_injections WHERE completed=1"
        )
        if not rows or rows[0]["a"] is None:
            return None
        available = self.available_window()
        start = rows[0]["a"] - padding_s
        end = (rows[0]["b"] or rows[0]["a"]) + padding_s
        if available is not None:
            start = max(start, available.start)
            end = min(end, available.end)
        return Window(start, end)

    # ------------------------------------------------------------------ 재생

    def _gap_intervals(self, window: Window) -> list[tuple[float, float]]:
        """신뢰할 수 없는 구간. 공백 직후 일정 시간도 함께 배제한다."""
        intervals: list[tuple[float, float]] = []
        try:
            rows = self.db.query(
                "SELECT ts, gap_seconds FROM system_events "
                "WHERE event IN ('time_gap','startup') AND ts BETWEEN ? AND ?",
                (window.start - 3600, window.end),
            )
        except Exception:
            return intervals

        for row in rows:
            # 공백 직후에는 속도 카운터가 다시 쌓이는 중이라 값이 의미 없다.
            intervals.append((row["ts"] - 1.0, row["ts"] + 30.0))
        return intervals

    def _load_processes(self, window: Window) -> dict[float, list[ProcessView]]:
        rows = self.db.query(
            "SELECT ts, pid, name, cpu_percent, rss_mb, io_read_bps, io_write_bps, "
            "handles, threads, foreground FROM process_metrics "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (window.start, window.end),
        )
        grouped: dict[float, list[ProcessView]] = {}
        for row in rows:
            grouped.setdefault(row["ts"], []).append(
                ProcessView(
                    pid=row["pid"],
                    name=row["name"] or "",
                    cpu_percent=row["cpu_percent"],
                    rss_mb=row["rss_mb"],
                    io_read_bps=row["io_read_bps"],
                    io_write_bps=row["io_write_bps"],
                    handles=row["handles"],
                    threads=row["threads"],
                    foreground=bool(row["foreground"]),
                )
            )
        return grouped

    def _load_gpus(self, window: Window) -> dict[float, list[dict]]:
        rows = self.db.query(
            "SELECT * FROM gpu_metrics WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (window.start, window.end),
        )
        grouped: dict[float, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["ts"], []).append(dict(row))
        return grouped

    def stream(self, window: Window) -> Iterator[Observation]:
        """구간을 관측 스트림으로. 시각순, 결정론적."""
        metric_rows = self.db.query(
            "SELECT * FROM metrics_raw WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (window.start, window.end),
        )
        processes = self._load_processes(window)
        gpus = self._load_gpus(window)
        gaps = self._gap_intervals(window)

        # 프로세스 행의 타임스탬프는 시스템 메트릭과 정확히 일치하지 않는다(수집기가
        # 다른 스레드다). 가장 가까운 시스템 관측에 붙인다.
        process_times = sorted(processes)
        gpu_times = sorted(gpus)

        self._stats = {
            "metrics": len(metric_rows),
            "process_ticks": len(process_times),
            "gpu_ticks": len(gpu_times),
            "gap_intervals": len(gaps),
            "suspect": 0,
        }

        previous_ts: float | None = None
        for row in metric_rows:
            ts = row["ts"]

            suspect = any(a <= ts <= b for a, b in gaps)
            if previous_ts is not None and ts - previous_ts > self.gap_tolerance_s:
                # 기록되지 않은 공백(프로그램이 죽어 있었던 구간 등)도 배제한다.
                suspect = True
            previous_ts = ts
            if suspect:
                self._stats["suspect"] += 1

            metrics = {k: row[k] for k in row.keys() if k != "cpu_per_core"}
            if row["cpu_per_core"]:
                try:
                    metrics["cpu_per_core"] = json.loads(row["cpu_per_core"])
                except (json.JSONDecodeError, TypeError):
                    metrics["cpu_per_core"] = None

            yield Observation(
                ts=ts,
                metrics=metrics,
                processes=processes.get(_nearest(process_times, ts), []),
                gpus=gpus.get(_nearest(gpu_times, ts), []),
                suspect=suspect,
            )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)


def _nearest(sorted_times: list[float], target: float, tolerance: float = 1.5) -> float:
    """가장 가까운 타임스탬프. 허용 범위를 벗어나면 매칭하지 않는다."""
    if not sorted_times:
        return -1.0
    import bisect

    index = bisect.bisect_left(sorted_times, target)
    candidates = []
    if index < len(sorted_times):
        candidates.append(sorted_times[index])
    if index > 0:
        candidates.append(sorted_times[index - 1])
    if not candidates:
        return -1.0
    best = min(candidates, key=lambda t: abs(t - target))
    return best if abs(best - target) <= tolerance else -1.0


if __name__ == "__main__":  # 스모크: python -m argus.eval.replay
    from ..logging_setup import setup

    setup(level="WARNING")
    with Database() as db:
        replayer = Replayer(db)
        available = replayer.available_window()
        if available is None:
            print("[FAIL] 재생할 데이터가 없다 — 먼저 `python -m argus` 로 수집할 것")
            raise SystemExit(1)
        print(f"  저장된 범위: {available}")

        window = replayer.window_around_injections() or available
        print(f"  재생 구간  : {window}")

        started = time.perf_counter()
        observations = list(replayer.stream(window))
        elapsed = time.perf_counter() - started

        print(f"  관측 {len(observations)}개  재생 {elapsed*1000:.0f}ms")
        print(f"  통계: {replayer.stats}")
        if observations:
            speedup = window.duration_s / elapsed if elapsed > 0 else 0
            print(f"  배속: 약 {speedup:,.0f}x (실시간 {window.duration_s/60:.1f}분 → {elapsed:.2f}초)")
            sample = observations[len(observations) // 2]
            print(
                f"  표본: cpu={sample.metric('cpu_total')}%  "
                f"mem={sample.metric('mem_percent')}%  "
                f"프로세스 {len(sample.processes)}개  suspect={sample.suspect}"
            )

        # 결정론 확인 — 두 번 재생해 완전히 같아야 비교가 성립한다
        again = list(replayer.stream(window))
        same = len(again) == len(observations) and all(
            a.ts == b.ts and a.metrics == b.metrics for a, b in zip(observations, again)
        )
        print(f"  재현성(두 번 재생 동일): {same}")

        problems = []
        if not observations:
            problems.append("관측이 하나도 생성되지 않았다")
        if not same:
            problems.append("두 번 재생 결과가 다르다 — 탐지기 비교가 무의미해진다")
        if observations and not any(o.processes for o in observations):
            problems.append("프로세스가 하나도 붙지 않았다 — 타임스탬프 매칭 실패")
        for p in problems:
            print(f"[FAIL] {p}")
        if problems:
            raise SystemExit(1)
    print("[OK] eval.replay")

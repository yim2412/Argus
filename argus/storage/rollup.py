"""1분 롤업 — 초 단위 원본을 접어 장기 보존 가능한 형태로 만든다.

**이것이 없으면 장기 데이터가 존재할 수 없다.** 원본은 24시간 뒤 삭제되는데,
Phase 4 는 레짐 학습에 며칠치를, Phase 5 는 최근 14일을, Phase 7 은 2주 축적을
전제한다. 원본을 그대로 오래 두는 것은 답이 아니라(1Hz × 다차원 = 하루 수백 MB),
1분으로 접는다 — 하루 1,440행이면 몇 년을 둬도 무해하다.

**평균만 남기면 레짐을 구분할 수 없다.** 게임과 빌드는 평균 CPU 가 같아도 분포가
다르다. 게임은 고르게 눌려 있고 빌드는 코어별로 튄다. 그래서 표준편차·최대·p95 를
함께 접는다. 접고 나면 원본이 없으므로, **여기서 안 남긴 것은 영원히 못 본다.**

집계를 SQL 이 아니라 파이썬에서 하는 이유: SQLite 에 백분위 함수가 없다.
p95 를 근사하려고 SQL 을 비틀기보다 1분 60행을 읽어 정확히 계산하는 편이 싸고 명확하다.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

from ..config.loader import RollupSettings
from ..logging_setup import get_logger
from ..runtime.supervisor import Component
from .hot import Database

log = get_logger(__name__)

BUCKET_S = 60

# metrics_1m 컬럼 순서. INSERT 와 집계 결과 조립이 같은 순서를 봐야 한다.
COLUMNS: tuple[str, ...] = (
    "ts_min",
    "sample_count",
    "has_gap",
    "cpu_mean",
    "cpu_max",
    "cpu_p95",
    "cpu_std",
    "cpu_core_max_mean",
    "cpu_imbalance_mean",
    "cpu_freq_mean",
    "cpu_perf_mean",
    "mem_percent_mean",
    "mem_percent_max",
    "mem_used_mb_mean",
    "swap_used_mb_max",
    "disk_read_bps_mean",
    "disk_read_bps_max",
    "disk_write_bps_mean",
    "disk_write_bps_max",
    "disk_read_iops_mean",
    "disk_write_iops_mean",
    "disk_queue_mean",
    "disk_queue_max",
    "disk_resp_ms_mean",
    "disk_resp_ms_p95",
    "net_rx_bps_mean",
    "net_rx_bps_max",
    "net_tx_bps_mean",
    "net_tx_bps_max",
    "ctx_switches_mean",
    "ctx_switches_max",
    "proc_count_mean",
    "thread_count_mean",
    "gpu_util_mean",
    "gpu_util_max",
    "gpu_vram_mb_mean",
    "gpu_vram_mb_max",
    "gpu_temp_max",
    "gpu_power_mean",
    "foreground_proc",
    "foreground_ratio",
    "top_cpu_proc",
    "top_cpu_share",
)


# ---------------------------------------------------------------- 집계 보조

def _clean(values: Iterable[Any]) -> list[float]:
    """NULL 을 걸러낸 실수 목록.

    수집기가 개별 지표를 못 얻는 것은 정상 상황이다(PDH 미가용, GPU 없음).
    한 지표가 비었다고 그 1분 전체를 버리면 안 되므로 지표별로 따로 거른다.
    """
    return [float(v) for v in values if v is not None]


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _max(values: Sequence[float]) -> float | None:
    return round(max(values), 3) if values else None


def _pct(values: Sequence[float], q: float) -> float | None:
    """표본 백분위(최근접 순위).

    보간하지 않는 이유는 관측된 값만 남기기 위해서다 — "응답시간 p95 = 95.05ms"처럼
    실제로 없던 값을 만들지 않는다.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _p95(values: Sequence[float]) -> float | None:
    return _pct(values, 0.95)


def _std(values: Sequence[float]) -> float | None:
    """모표준편차. 1분 안의 흔들림 크기를 재는 것이지 모집단 추정이 아니다."""
    if len(values) < 2:
        return 0.0 if values else None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return round(variance**0.5, 3)


def bucket_of(ts: float, size: int = BUCKET_S) -> int:
    return int(ts // size) * size


# ---------------------------------------------------------------- 공통 골격


class _RollupBase(Component):
    """워터마크 관리와 밀린 구간 계산. 1분·5분 롤업이 공유한다.

    워터마크를 롤업마다 따로 두는 이유: 보존 정리가 **자기를 접은 롤업의** 워터마크를
    봐야 하기 때문이다. 하나로 묶으면 프로세스 데이터가 "1분 롤업이 지나갔다"는 이유로
    접히지도 않은 채 지워진다 — 실제로 그렇게 되어 있었다.
    """

    state_name: str = ""
    bucket_s: int = BUCKET_S
    source_table: str = "metrics_raw"
    source_ts_column: str = "ts"

    db: Database
    settings: RollupSettings

    def watermark(self) -> float | None:
        rows = self.db.query(
            "SELECT watermark_ts FROM rollup_state WHERE name=?", (self.state_name,)
        )
        return float(rows[0]["watermark_ts"]) if rows else None

    def _set_watermark(self, ts: float) -> None:
        with self.db._lock:  # noqa: SLF001 - 같은 커넥션을 쓰는 내부 협력
            self.db.conn.execute(
                "INSERT INTO rollup_state (name, watermark_ts, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET watermark_ts=excluded.watermark_ts, "
                "updated_at=excluded.updated_at",
                (self.state_name, ts, time.time()),
            )
            self.db.conn.commit()

    def _pending_range(self, now: float) -> tuple[int, int] | None:
        """접어야 할 [start, end) 버킷 구간. 없으면 None.

        진행 중인 버킷은 접지 않는다 — 아직 데이터가 들어오는 중이라 반쪽짜리가 된다.
        `lag_s` 는 배치 writer 가 큐를 비우는 시간까지 감안한 여유다.
        """
        start_ts = self.watermark()
        if start_ts is None:
            rows = self.db.query(
                f"SELECT MIN({self.source_ts_column}) AS lo FROM {self.source_table}"
            )
            if not rows or rows[0]["lo"] is None:
                return None
            start_ts = float(rows[0]["lo"])

        start = bucket_of(start_ts, self.bucket_s)
        end = bucket_of(now - self.settings.lag_s, self.bucket_s)
        if end <= start:
            return None

        # 한 번에 처리할 버킷 수를 제한한다. 첫 실행이나 오래 꺼져 있던 뒤에는
        # 밀린 구간이 수천 개일 수 있는데, 그걸 한 틱에 처리하면 우리가 만든
        # 디스크 IO 가 관측 대상을 오염시킨다.
        max_end = start + self.settings.max_buckets_per_run * self.bucket_s
        return start, min(end, max_end)

    def tick(self) -> None:
        self.run_once()

    def run_once(self, now: float | None = None) -> int:  # pragma: no cover - 서브클래스가 구현
        raise NotImplementedError


class Rollup(_RollupBase):
    """완결된 1분 버킷을 `metrics_1m` 으로 접는다."""

    name = "rollup"
    state_name = "metrics_1m"
    bucket_s = BUCKET_S

    def __init__(self, db: Database, settings: RollupSettings) -> None:
        self.db = db
        self.settings = settings
        self.interval_s = settings.interval_s

    def _gap_buckets(self, start: int, end: int) -> set[int]:
        """절전·시각변경 공백이 걸친 버킷.

        이 구간의 통계는 신뢰할 수 없다. 지우지 않고 표시만 하는 이유는, 나중에
        레짐·베이스라인이 "제외"를 스스로 판단할 수 있어야 하기 때문이다.
        """
        rows = self.db.query(
            "SELECT ts, gap_seconds FROM system_events "
            "WHERE ts >= ? AND ts < ? AND gap_seconds IS NOT NULL",
            (start - 86400, end),
        )
        marked: set[int] = set()
        for row in rows:
            gap = float(row["gap_seconds"] or 0)
            gap_start = float(row["ts"]) - gap
            b = bucket_of(gap_start)
            while b <= bucket_of(float(row["ts"])):
                if start <= b < end:
                    marked.add(b)
                b += BUCKET_S
        return marked

    def _system_buckets(self, start: int, end: int) -> dict[int, dict[str, list[float]]]:
        rows = self.db.query(
            "SELECT ts, cpu_total, cpu_max_core, cpu_freq_mhz, cpu_perf_percent, "
            "mem_percent, mem_used_mb, swap_used_mb, "
            "disk_read_bps, disk_write_bps, disk_read_iops, disk_write_iops, "
            "disk_queue, disk_resp_ms, net_rx_bps, net_tx_bps, "
            "ctx_switches_ps, proc_count, thread_count "
            "FROM metrics_raw WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        )
        buckets: dict[int, dict[str, list[float]]] = {}
        for row in rows:
            b = buckets.setdefault(bucket_of(row["ts"]), {})
            for key in row.keys():
                if key == "ts":
                    continue
                if row[key] is not None:
                    b.setdefault(key, []).append(float(row[key]))
            # 코어 불균형 = 가장 바쁜 코어와 전체 평균의 차이.
            # 단일 스레드 부하(게임 루프)와 전 코어 부하(빌드)를 가르는 지문이다.
            if row["cpu_max_core"] is not None and row["cpu_total"] is not None:
                b.setdefault("cpu_imbalance", []).append(
                    float(row["cpu_max_core"]) - float(row["cpu_total"])
                )
        return buckets

    def _gpu_buckets(self, start: int, end: int) -> dict[int, dict[str, list[float]]]:
        """GPU 는 여러 장일 수 있다. 지금은 0번만 접는다 — 다중 GPU 는 레짐에
        필요하지 않고, 필요해지면 그때 컬럼이 아니라 별도 테이블로 가야 한다."""
        rows = self.db.query(
            "SELECT ts, util_percent, vram_used_mb, temp_c, power_w FROM gpu_metrics "
            "WHERE ts >= ? AND ts < ? AND gpu_index = 0 ORDER BY ts",
            (start, end),
        )
        buckets: dict[int, dict[str, list[float]]] = {}
        for row in rows:
            b = buckets.setdefault(bucket_of(row["ts"]), {})
            for key in ("util_percent", "vram_used_mb", "temp_c", "power_w"):
                if row[key] is not None:
                    b.setdefault(key, []).append(float(row[key]))
        return buckets

    def _process_buckets(self, start: int, end: int) -> dict[int, tuple[Any, ...]]:
        """버킷별 (최빈 포어그라운드, 비율, CPU 1위 프로세스, 그 CPU).

        레짐 추론이 요구하는 "무엇을 하는 중인가"는 리소스 수치만으로 알 수 없다.
        포어그라운드 프로세스가 그 질문에 답하는 유일한 신호다.
        """
        rows = self.db.query(
            "SELECT ts, name, cpu_percent, foreground FROM process_metrics "
            "WHERE ts >= ? AND ts < ? AND (foreground = 1 OR cpu_percent > 0)",
            (start, end),
        )
        fg_counts: dict[int, dict[str, int]] = {}
        cpu_sums: dict[int, dict[str, float]] = {}
        # 버킷별 고유 타임스탬프 수. 프로세스 CPU 합을 이걸로 나눠야 버킷 간 비교가 된다
        # (같은 1분이라도 스로틀·드롭으로 표본 수가 다를 수 있다).
        tick_sets: dict[int, set[float]] = {}
        for row in rows:
            b = bucket_of(row["ts"])
            tick_sets.setdefault(b, set()).add(row["ts"])
            if row["foreground"]:
                fg_counts.setdefault(b, {})
                fg_counts[b][row["name"]] = fg_counts[b].get(row["name"], 0) + 1
            if row["cpu_percent"]:
                cpu_sums.setdefault(b, {})
                name = row["name"]
                cpu_sums[b][name] = cpu_sums[b].get(name, 0.0) + float(row["cpu_percent"])

        out: dict[int, tuple[Any, ...]] = {}
        for b in set(fg_counts) | set(cpu_sums):
            counts = fg_counts.get(b, {})
            total = sum(counts.values())
            if counts:
                proc, hits = max(counts.items(), key=lambda kv: kv[1])
                fg_proc, fg_ratio = proc, round(hits / total, 3)
            else:
                fg_proc, fg_ratio = None, None

            sums = cpu_sums.get(b, {})
            if sums:
                samples = max(1, len(tick_sets.get(b, ())))
                top_name, top_sum = max(sums.items(), key=lambda kv: kv[1])
                top_proc, top_share = top_name, round(top_sum / samples, 2)
            else:
                top_proc, top_share = None, None
            out[b] = (fg_proc, fg_ratio, top_proc, top_share)
        return out

    def run_once(self, now: float | None = None) -> int:
        """밀린 버킷을 접는다. 접은 버킷 수를 돌려준다."""
        now = now if now is not None else time.time()
        pending = self._pending_range(now)
        if pending is None:
            return 0
        start, end = pending

        system = self._system_buckets(start, end)
        gpu = self._gpu_buckets(start, end)
        process = self._process_buckets(start, end)
        gaps = self._gap_buckets(start, end)

        rows: list[tuple[Any, ...]] = []
        for bucket in sorted(system):
            s = system[bucket]
            g = gpu.get(bucket, {})
            fg_proc, fg_ratio, top_proc, top_share = process.get(bucket, (None, None, None, None))

            cpu = _clean(s.get("cpu_total", []))
            rows.append(
                (
                    bucket,
                    len(cpu),
                    1 if bucket in gaps else 0,
                    _mean(cpu),
                    _max(cpu),
                    _p95(cpu),
                    _std(cpu),
                    _mean(s.get("cpu_max_core", [])),
                    _mean(s.get("cpu_imbalance", [])),
                    _mean(s.get("cpu_freq_mhz", [])),
                    _mean(s.get("cpu_perf_percent", [])),
                    _mean(s.get("mem_percent", [])),
                    _max(s.get("mem_percent", [])),
                    _mean(s.get("mem_used_mb", [])),
                    _max(s.get("swap_used_mb", [])),
                    _mean(s.get("disk_read_bps", [])),
                    _max(s.get("disk_read_bps", [])),
                    _mean(s.get("disk_write_bps", [])),
                    _max(s.get("disk_write_bps", [])),
                    _mean(s.get("disk_read_iops", [])),
                    _mean(s.get("disk_write_iops", [])),
                    _mean(s.get("disk_queue", [])),
                    _max(s.get("disk_queue", [])),
                    _mean(s.get("disk_resp_ms", [])),
                    _p95(s.get("disk_resp_ms", [])),
                    _mean(s.get("net_rx_bps", [])),
                    _max(s.get("net_rx_bps", [])),
                    _mean(s.get("net_tx_bps", [])),
                    _max(s.get("net_tx_bps", [])),
                    _mean(s.get("ctx_switches_ps", [])),
                    _max(s.get("ctx_switches_ps", [])),
                    _mean(s.get("proc_count", [])),
                    _mean(s.get("thread_count", [])),
                    _mean(g.get("util_percent", [])),
                    _max(g.get("util_percent", [])),
                    _mean(g.get("vram_used_mb", [])),
                    _max(g.get("vram_used_mb", [])),
                    _max(g.get("temp_c", [])),
                    _mean(g.get("power_w", [])),
                    fg_proc,
                    fg_ratio,
                    top_proc,
                    top_share,
                )
            )

        if rows:
            placeholders = ", ".join("?" * len(COLUMNS))
            with self.db._lock:  # noqa: SLF001
                # REPLACE 인 이유: 같은 버킷을 다시 접어도 결과가 같아야 한다(멱등).
                # 크래시로 워터마크가 뒤로 갔을 때 중복 키로 죽으면 안 된다.
                self.db.conn.executemany(
                    f"INSERT OR REPLACE INTO metrics_1m ({', '.join(COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
                self.db.conn.commit()

        # 원본이 없는 구간(수집 중단 등)도 지나가야 워터마크가 멈추지 않는다.
        self._set_watermark(end)
        if rows:
            log.debug("롤업", extra={"buckets": len(rows), "watermark": end})
        return len(rows)


# ---------------------------------------------------------------- 프로세스


PROCESS_BUCKET_S = 300

PROCESS_COLUMNS: tuple[str, ...] = (
    "ts_5m",
    "name",
    "sample_count",
    "pid_count",
    "cpu_mean",
    "cpu_p50",
    "cpu_p95",
    "cpu_p99",
    "cpu_max",
    "rss_mean",
    "rss_p50",
    "rss_p95",
    "rss_max",
    "io_read_bps_mean",
    "io_write_bps_mean",
    "io_write_bps_max",
    "handles_mean",
    "handles_max",
    "threads_max",
    "foreground_ratio",
)


class ProcessRollup(_RollupBase):
    """프로세스를 프로그램 이름 단위로 5분마다 접는다.

    **PID 가 아니라 이름이 단위다.** PID 는 재시작마다 바뀌므로 지문의 단위가 될 수
    없다. "크롬은 평소 CPU 6%"가 지문이지 "PID 8812 는"이 아니다.

    **버킷마다 상위 N 개만 남긴다.** `cpu > 0` 같은 조건으로 거르면 프로세스가 500개인
    PC 에서 저장량이 예측 불가로 늘어난다. 배포 대상의 하드웨어를 가정하지 않으려면
    행 수에 상한이 있어야 한다. CPU 상위와 메모리 상위를 따로 뽑아 합치는 이유는,
    메모리만 먹고 CPU 는 안 쓰는 프로세스(누수 후보)를 놓치지 않기 위해서다.
    """

    name = "process_rollup"
    state_name = "process_5m"
    bucket_s = PROCESS_BUCKET_S
    source_table = "process_metrics"

    def __init__(self, db: Database, settings: RollupSettings, top_n: int = 40) -> None:
        self.db = db
        self.settings = settings
        self.top_n = top_n
        self.interval_s = settings.interval_s * 5

    def run_once(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        pending = self._pending_range(now)
        if pending is None:
            return 0
        start, end = pending

        # **시각별로 먼저 합친다.** 같은 이름의 프로세스가 여럿이면 그 합이 그 프로그램의
        # 사용량이다(크롬 탭 30개는 사용자에게 하나의 크롬이다). 표본을 그냥 모아
        # 분위수를 내면 "개별 프로세스의 p95"가 되는데, 지문에 필요한 것은
        # "크롬 전체가 평소 얼마나 쓰나"다. 둘은 30배쯤 다르다.
        rows = self.db.query(
            "SELECT ts, name, SUM(cpu_percent) AS cpu, SUM(rss_mb) AS rss, "
            "SUM(io_read_bps) AS io_read, SUM(io_write_bps) AS io_write, "
            "SUM(handles) AS handles, SUM(threads) AS threads, "
            "COUNT(DISTINCT pid) AS pids, MAX(foreground) AS fg "
            "FROM process_metrics WHERE ts >= ? AND ts < ? AND name IS NOT NULL "
            "GROUP BY ts, name",
            (start, end),
        )

        # 버킷 → 이름 → 시계열
        buckets: dict[int, dict[str, dict[str, list[float]]]] = {}
        for row in rows:
            bucket = bucket_of(row["ts"], self.bucket_s)
            series = buckets.setdefault(bucket, {}).setdefault(
                row["name"], {k: [] for k in ("cpu", "rss", "io_read", "io_write", "handles", "threads", "pids", "fg")}
            )
            for key in ("cpu", "rss", "io_read", "io_write", "handles", "threads", "pids"):
                if row[key] is not None:
                    series[key].append(float(row[key]))
            series["fg"].append(1.0 if row["fg"] else 0.0)

        out: list[tuple[Any, ...]] = []
        for bucket, by_name in buckets.items():
            # 상한을 두되 CPU 상위와 메모리 상위를 따로 뽑는다 — 메모리만 먹고 CPU 는
            # 안 쓰는 프로세스(누수 후보)를 CPU 순위로만 자르면 놓친다.
            by_cpu = sorted(by_name.items(), key=lambda kv: _mean(kv[1]["cpu"]) or 0.0, reverse=True)
            by_rss = sorted(by_name.items(), key=lambda kv: _max(kv[1]["rss"]) or 0.0, reverse=True)
            keep = {n for n, _ in by_cpu[: self.top_n]} | {n for n, _ in by_rss[: self.top_n]}

            for name in keep:
                series = by_name[name]
                cpu, rss, fg = series["cpu"], series["rss"], series["fg"]
                samples = len(fg)
                out.append(
                    (
                        bucket,
                        name,
                        samples,
                        int(_max(series["pids"]) or 0) or None,
                        _mean(cpu),
                        _pct(cpu, 0.50),
                        _pct(cpu, 0.95),
                        _pct(cpu, 0.99),
                        _max(cpu),
                        _mean(rss),
                        _pct(rss, 0.50),
                        _pct(rss, 0.95),
                        _max(rss),
                        _mean(series["io_read"]),
                        _mean(series["io_write"]),
                        _max(series["io_write"]),
                        _mean(series["handles"]),
                        int(_max(series["handles"]) or 0) or None,
                        int(_max(series["threads"]) or 0) or None,
                        round(sum(fg) / samples, 3) if samples else None,
                    )
                )

        if out:
            placeholders = ", ".join("?" * len(PROCESS_COLUMNS))
            with self.db._lock:  # noqa: SLF001
                self.db.conn.executemany(
                    f"INSERT OR REPLACE INTO process_5m ({', '.join(PROCESS_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    out,
                )
                self.db.conn.commit()

        self._set_watermark(end)
        if out:
            log.debug("프로세스 롤업", extra={"rows": len(out), "watermark": end})
        return len(out)


# ---------------------------------------------------------------- 네트워크


NET_COLUMNS: tuple[str, ...] = (
    "ts_5m",
    "name",
    "sample_count",
    "conn_count",
    "distinct_remotes",
    "distinct_ports",
    "listen_count",
)


class NetworkRollup(_RollupBase):
    """네트워크 활동을 프로그램 단위로 5분마다 접는다.

    **개별 원격 주소를 저장하지 않는다.** 신호가 되지 않기 때문이고(실측: 신규 주소가
    시간당 87건 — 알림이 아니라 소음이다), 동시에 개인정보이기 때문이다. 원본이
    72시간 뒤 사라지는데 집계가 주소를 영구 보관하면 보존 정책을 우회하는 셈이 된다.

    계획서가 원하는 "단시간 다수 신규 원격 IP"는 **개수만으로** 잡힌다.
    """

    name = "net_rollup"
    state_name = "net_activity_5m"
    bucket_s = PROCESS_BUCKET_S
    source_table = "net_connections"

    def __init__(self, db: Database, settings: RollupSettings) -> None:
        self.db = db
        self.settings = settings
        self.interval_s = settings.interval_s * 5

    def run_once(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        pending = self._pending_range(now)
        if pending is None:
            return 0
        start, end = pending

        rows = self.db.query(
            "SELECT ts, name, raddr, rport, status FROM net_connections "
            "WHERE ts >= ? AND ts < ? AND name IS NOT NULL",
            (start, end),
        )

        buckets: dict[tuple[int, str], dict] = {}
        for row in rows:
            key = (bucket_of(row["ts"], self.bucket_s), row["name"])
            entry = buckets.setdefault(
                key,
                {"ticks": set(), "remotes": set(), "ports": set(), "conns": 0, "listen": 0},
            )
            entry["ticks"].add(row["ts"])
            entry["conns"] += 1
            if row["raddr"]:
                entry["remotes"].add(row["raddr"])
            if row["rport"]:
                entry["ports"].add(row["rport"])
            if (row["status"] or "").upper() == "LISTEN":
                entry["listen"] += 1

        out = []
        for (bucket, program), entry in buckets.items():
            samples = max(1, len(entry["ticks"]))
            out.append(
                (
                    bucket,
                    program,
                    samples,
                    round(entry["conns"] / samples, 2),
                    len(entry["remotes"]),
                    len(entry["ports"]),
                    entry["listen"],
                )
            )

        if out:
            placeholders = ", ".join("?" * len(NET_COLUMNS))
            with self.db._lock:  # noqa: SLF001
                self.db.conn.executemany(
                    f"INSERT OR REPLACE INTO net_activity_5m ({', '.join(NET_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    out,
                )
                self.db.conn.commit()

        self._set_watermark(end)
        if out:
            log.debug("네트워크 롤업", extra={"rows": len(out), "watermark": end})
        return len(out)


if __name__ == "__main__":  # 스모크: python -m argus.storage.rollup
    from ..config.loader import load_settings
    from ..logging_setup import setup

    setup(level="INFO")
    settings = load_settings()

    with Database() as db:
        rollup = Rollup(db, settings.rollup)

        # 원본이 없는 DB(새 설치·테스트 환경)에서도 스모크가 성립해야 한다.
        # 접을 것이 없다는 이유로 [FAIL] 이 나면 진짜 고장과 구분되지 않는다.
        pending = rollup._pending_range(time.time())
        synthetic = pending is None
        if synthetic:
            base = bucket_of(time.time() - 600)
            db.insert_many(
                "metrics_raw",
                ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms"),
                [(base + i, 10.0 + i % 7, 40.0 + i % 11, 30.0, 0.1) for i in range(60)],
            )
            # 워터마크가 이미 앞서 있으면 합성 구간을 건너뛴다. 스모크가 검증하려는
            # 것은 집계 로직이므로 시작점을 명시적으로 맞춘다.
            rollup._set_watermark(base)
            print("  (원본이 없어 합성 1분을 넣고 검증한다)")

        before = db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"]
        started = time.perf_counter()
        made = rollup.run_once()
        elapsed = (time.perf_counter() - started) * 1000
        after = db.query("SELECT COUNT(*) AS c FROM metrics_1m")[0]["c"]

        print(f"  접은 버킷 : {made} ({elapsed:.0f}ms)")
        print(f"  metrics_1m: {before} -> {after}행")
        print(f"  워터마크  : {rollup.watermark()}")

        sample = db.query(
            "SELECT ts_min, sample_count, cpu_mean, cpu_max, cpu_std, cpu_imbalance_mean, "
            "gpu_util_mean, foreground_proc, foreground_ratio, top_cpu_proc "
            "FROM metrics_1m ORDER BY ts_min DESC LIMIT 5"
        )
        for row in sample:
            stamp = time.strftime("%H:%M", time.localtime(row["ts_min"]))
            print(
                f"    {stamp}  n={row['sample_count']:<3} cpu={row['cpu_mean']}"
                f"/{row['cpu_max']} σ={row['cpu_std']} imb={row['cpu_imbalance_mean']} "
                f"gpu={row['gpu_util_mean']}  fg={row['foreground_proc']}"
                f"({row['foreground_ratio']})  top={row['top_cpu_proc']}"
            )

        if after == 0:
            print("[FAIL] 접힌 버킷이 없다 — 원본이 있는데 롤업이 비었다면 버그다")
            raise SystemExit(1)

        # 네트워크 롤업 (5분) — 개별 주소는 저장하지 않는다
        net = NetworkRollup(db, settings.rollup)
        net_rows = net.run_once()
        net_total = db.query("SELECT COUNT(*) AS c FROM net_activity_5m")[0]["c"]
        print(f"\n  네트워크 롤업: {net_rows}행, 누적 {net_total}행")
        for row in db.query(
            "SELECT * FROM net_activity_5m ORDER BY ts_5m DESC, distinct_remotes DESC LIMIT 4"
        ):
            stamp = time.strftime("%H:%M", time.localtime(row["ts_5m"]))
            print(
                f"    {stamp}  {row['name']:<22} 대상 {row['distinct_remotes']:>4}개 "
                f"포트 {row['distinct_ports']:>3}개  연결 {row['conn_count']}"
            )

        # 프로세스 롤업 (5분)
        proc = ProcessRollup(db, settings.rollup, top_n=settings.rollup.process_top_n)
        started = time.perf_counter()
        proc_rows = proc.run_once()
        proc_ms = (time.perf_counter() - started) * 1000
        total = db.query("SELECT COUNT(*) AS c FROM process_5m")[0]["c"]
        print(f"\n  프로세스 롤업: {proc_rows}행 ({proc_ms:.0f}ms), 누적 {total}행")
        print(f"  워터마크     : {proc.watermark()}")
        for row in db.query(
            "SELECT * FROM process_5m ORDER BY ts_5m DESC, cpu_mean DESC LIMIT 5"
        ):
            stamp = time.strftime("%H:%M", time.localtime(row["ts_5m"]))
            print(
                f"    {stamp}  {row['name']:<22} n={row['sample_count']:<4} "
                f"pids={row['pid_count']:<3} cpu p50={row['cpu_p50']} p95={row['cpu_p95']} "
                f"p99={row['cpu_p99']}  rss p95={row['rss_p95']}MB"
            )
    print("[OK] storage.rollup")

-- Phase 4-A: 1분 롤업. 장기 데이터가 존재할 수 있게 만드는 단계.
--
-- 지금까지 원본은 24시간 뒤 삭제됐다. 그런데 Phase 4 는 레짐 학습에 며칠치를,
-- Phase 5 는 "최근 14일"을, Phase 7 은 "2주 축적"을 전제한다.
-- **그 데이터는 지금 구조에서 존재할 수 없다** — 매일 사라지기 때문이다.
--
-- 원본을 그대로 오래 두는 것은 답이 아니다(1Hz × 다차원 = 하루 300MB 규모).
-- 1분으로 접으면 하루 1,440행이라 몇 년을 둬도 무해하다.
--
-- **평균만 남기면 안 된다.** 레짐(게임/빌드/유휴)은 평균이 아니라 *분포*로 갈린다 —
-- 같은 CPU 30% 라도 게임은 고르게 눌려 있고 빌드는 코어별로 튄다. 그래서 분산·최대·
-- p95 를 함께 접는다. 계획서 3.3 의 "코어 수와 무관한 집계값(평균·최대·분산·불균형도)"
-- 이 요구하는 것도 이것이다.

CREATE TABLE IF NOT EXISTS metrics_1m (
    ts_min          INTEGER PRIMARY KEY,  -- 1분 버킷 시작 (unix epoch, 60 의 배수)
    sample_count    INTEGER NOT NULL,     -- 실제 표본 수. 60 보다 크게 모자라면 신뢰도가 낮다
    has_gap         INTEGER DEFAULT 0,    -- 절전·시각변경 공백을 포함한 구간인가

    -- CPU. 레짐 구분의 주력이라 분포를 가장 자세히 남긴다.
    cpu_mean        REAL,
    cpu_max         REAL,
    cpu_p95         REAL,
    cpu_std         REAL,                 -- 부하가 고른가 튀는가. 게임과 빌드를 가른다
    cpu_core_max_mean REAL,               -- per-core 최대값의 평균
    cpu_imbalance_mean REAL,              -- (코어최대 - 전체평균). 단일 스레드 부하의 지문
    cpu_freq_mean   REAL,
    cpu_perf_mean   REAL,                 -- 실클럭 비율. 스로틀링이 여기서 보인다

    -- 메모리
    mem_percent_mean REAL,
    mem_percent_max  REAL,
    mem_used_mb_mean REAL,
    swap_used_mb_max REAL,

    -- 디스크. 처리량(원인)과 응답시간(증상)을 함께 남긴다.
    disk_read_bps_mean  REAL,
    disk_read_bps_max   REAL,
    disk_write_bps_mean REAL,
    disk_write_bps_max  REAL,
    disk_read_iops_mean  REAL,
    disk_write_iops_mean REAL,
    disk_queue_mean REAL,
    disk_queue_max  REAL,
    disk_resp_ms_mean REAL,
    disk_resp_ms_p95  REAL,               -- 응답시간은 평균보다 꼬리가 아프다

    -- 네트워크
    net_rx_bps_mean REAL,
    net_rx_bps_max  REAL,
    net_tx_bps_mean REAL,
    net_tx_bps_max  REAL,

    -- 시스템 전반
    ctx_switches_mean REAL,
    ctx_switches_max  REAL,
    proc_count_mean   REAL,
    thread_count_mean REAL,

    -- GPU. 없는 머신에서는 전부 NULL 이며, 그 자체가 정보다.
    gpu_util_mean    REAL,
    gpu_util_max     REAL,
    gpu_vram_mb_mean REAL,
    gpu_vram_mb_max  REAL,
    gpu_temp_max     REAL,
    gpu_power_mean   REAL,

    -- 레짐 라벨링의 입력. "무엇을 하는 중인가"는 리소스만으로 알 수 없다.
    foreground_proc  TEXT,                -- 그 1분의 최빈 포어그라운드 프로세스
    foreground_ratio REAL,                -- 최빈값이 차지한 비율. 낮으면 전환이 잦았다는 뜻
    top_cpu_proc     TEXT,                -- CPU 를 가장 많이 쓴 프로세스
    top_cpu_share    REAL                 -- 그 프로세스가 차지한 CPU (%)
);

CREATE INDEX IF NOT EXISTS idx_metrics_1m_fg ON metrics_1m (foreground_proc);

-- 어디까지 접었는지. **보존 정리가 이 값을 넘어 지우면 데이터가 영구히 사라진다.**
-- 롤업과 삭제 사이의 유일한 안전장치라 별도 테이블로 명시한다.
CREATE TABLE IF NOT EXISTS rollup_state (
    name          TEXT PRIMARY KEY,   -- 'metrics_1m'
    watermark_ts  REAL NOT NULL,      -- 이 시각 이전은 집계 완료
    updated_at    REAL NOT NULL
);

-- 웜 스토어(Parquet)로 내보낸 날짜. 내보낸 날짜는 metrics_1m 에서 지워도 된다.
-- Parquet 은 append 가 안 되므로 **완전히 끝난 날짜만** 한 번에 쓰고 이후 불변으로 둔다.
CREATE TABLE IF NOT EXISTS warm_exports (
    date_key   TEXT PRIMARY KEY,      -- 'YYYY-MM-DD' (로컬 시각 기준)
    path       TEXT NOT NULL,
    row_count  INTEGER NOT NULL,
    bytes      INTEGER,
    exported_at REAL NOT NULL
);

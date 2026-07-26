-- Phase 1: 실제 메트릭 수집 테이블.
--
-- 설계 메모
--  * 절대 임계값을 스키마에 반영하지 않는다. 판단은 machine_profile 기준의 상대값으로 한다.
--  * GPU 는 여러 장일 수 있어 별도 테이블로 분리했다(metrics_raw 에 넣으면 다중 GPU 를 못 담는다).
--  * cpu_per_core 는 코어 수가 머신마다 달라 컬럼으로 못 박는다. JSON 배열 문자열로 둔다.
--    BLOB 대신 TEXT 인 이유는 디버깅 시 눈으로 읽을 수 있어야 하기 때문이다.
--  * 속도(bps/iops)는 수집기가 이전 스냅샷과의 차이로 계산해 넣는다. 누적값을 그대로
--    저장하면 나중에 모든 질의에서 차분을 다시 계산해야 한다.

-- 1Hz 시스템 스냅샷
CREATE TABLE IF NOT EXISTS metrics_raw (
    ts              REAL NOT NULL,
    cpu_total       REAL,     -- 전체 CPU 사용률 %
    cpu_per_core    TEXT,     -- JSON 배열, 예: "[12.5, 3.1, ...]"
    cpu_max_core    REAL,     -- 단일 코어 최대치 (단일 스레드 병목 신호)
    cpu_freq_mhz    REAL,     -- 현재 클럭
    -- PDH: 공칭 클럭 대비 실제 성능 %. 100 초과는 부스트, 100 미만이면 스로틀·절전.
    -- 온도 병목을 클럭 하락으로 잡아내는 핵심 지표.
    cpu_perf_percent REAL,
    mem_used_mb     REAL,
    mem_avail_mb    REAL,
    mem_percent     REAL,
    swap_used_mb    REAL,
    disk_read_bps   REAL,
    disk_write_bps  REAL,
    disk_read_iops  REAL,
    disk_write_iops REAL,
    disk_queue      REAL,     -- PDH: Current Disk Queue Length
    disk_resp_ms    REAL,     -- PDH: Avg. Disk sec/Transfer (증상 지표 — 사용률보다 중요)
    net_rx_bps      REAL,
    net_tx_bps      REAL,
    ctx_switches_ps REAL,     -- PDH: Context Switches/sec (경합 신호)
    proc_count      INTEGER,
    thread_count    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_metrics_raw_ts ON metrics_raw (ts);

-- GPU (장치별 한 행)
CREATE TABLE IF NOT EXISTS gpu_metrics (
    ts                REAL NOT NULL,
    gpu_index         INTEGER NOT NULL,
    util_percent      REAL,
    mem_util_percent  REAL,   -- 메모리 대역폭 사용률 (VRAM 점유율과 다르다)
    vram_used_mb      REAL,
    vram_total_mb     REAL,
    temp_c            REAL,
    power_w           REAL,
    power_limit_w     REAL,
    pstate            INTEGER,
    clock_sm_mhz      INTEGER,
    clock_mem_mhz     INTEGER,
    fan_percent       REAL,
    throttle_reasons  TEXT     -- 쉼표 구분. THERMAL 이 뜨면 온도 병목이 확정된다.
);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_ts ON gpu_metrics (ts, gpu_index);

-- 프로세스별 메트릭.
-- tier 1 = 활성 집합(1초 주기), tier 2 = 전체 스캔에서만 관측.
-- 전체 프로세스를 1초마다 순회하면 그 자체로 CPU 를 수 % 먹는다.
CREATE TABLE IF NOT EXISTS process_metrics (
    ts            REAL NOT NULL,
    pid           INTEGER NOT NULL,
    name          TEXT,
    cpu_percent   REAL,       -- 논리 코어 수로 정규화 (머신 전체 대비 %)
    rss_mb        REAL,
    io_read_bps   REAL,
    io_write_bps  REAL,
    handles       INTEGER,
    threads       INTEGER,
    tier          INTEGER,
    foreground    INTEGER      -- 1 = 이 시점의 포어그라운드 창 소유 프로세스
);
CREATE INDEX IF NOT EXISTS idx_process_metrics_ts ON process_metrics (ts);
-- (name, ts) 인덱스는 일부러 만들지 않는다. 프로세스 지문(Phase 6)에서 필요해지지만
-- 지금은 아무 질의도 쓰지 않으면서 DB 의 21% 를 차지했다(실측: 데이터 172KB 에 인덱스
-- 98KB). 이 테이블은 보존 기한이 24시간이라 나중에 인덱스를 추가해도 재구축이 빠르다.
-- 쓰지 않는 인덱스는 매 삽입마다 비용만 낸다.

-- 프로세스 생성/종료 이벤트.
-- 폴링으로 잡는 근사치라 1초 미만 단명 프로세스는 놓친다. 정확한 포착은 ETW(Phase 12).
CREATE TABLE IF NOT EXISTS process_events (
    ts       REAL NOT NULL,
    event    TEXT NOT NULL,   -- 'start' | 'exit'
    pid      INTEGER NOT NULL,
    ppid     INTEGER,
    name     TEXT,
    exe      TEXT,
    username TEXT
);
CREATE INDEX IF NOT EXISTS idx_process_events_ts ON process_events (ts);

-- 네트워크 연결 스냅샷 (저빈도).
-- 개인정보가 가장 짙은 테이블이다. 절대 커밋·전송하지 않는다.
CREATE TABLE IF NOT EXISTS net_connections (
    ts     REAL NOT NULL,
    pid    INTEGER,
    name   TEXT,
    laddr  TEXT,
    lport  INTEGER,
    raddr  TEXT,
    rport  INTEGER,
    status TEXT,
    family TEXT
);
CREATE INDEX IF NOT EXISTS idx_net_connections_ts ON net_connections (ts);

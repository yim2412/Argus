-- Phase 2: 평가 인프라.
--
-- 이상 탐지에는 정답이 없다. "이 구간이 진짜 이상이었다"고 말해 줄 사람이 없으니
-- 탐지기를 만들어도 잘 맞는지 잴 방법이 없고, 결국 임계값을 감으로 만지게 된다.
--
-- 그래서 정답을 **만든다.** 결함을 인위로 주입하면 언제부터 언제까지가 이상인지
-- 아는 구간이 생기고, 그것이 유일한 채점 기준이 된다.

-- 결함 주입 구간 = 정답 라벨(ground truth).
-- 주입기가 시작할 때 행을 만들고 끝날 때 ts_end 를 채운다. ts_end 가 NULL 인 행은
-- 주입기가 비정상 종료된 것이므로 채점에서 제외해야 한다.
CREATE TABLE IF NOT EXISTS fault_injections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario   TEXT NOT NULL,     -- 'memory_leak' | 'cpu_spin' | 'disk_thrash' | ...
    ts_start   REAL NOT NULL,
    ts_end     REAL,
    pid        INTEGER,           -- 주입기 프로세스. 귀인이 이걸 지목해야 정답
    params     TEXT,              -- JSON
    ramp       INTEGER DEFAULT 0, -- 1 = 서서히 증가 (점진적 열화 시나리오)
    completed  INTEGER DEFAULT 0, -- 1 = 정상 종료. 0 이면 채점에서 뺀다
    notes      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fault_injections_ts ON fault_injections (ts_start);

-- 탐지기가 낸 판정. 리플레이 채점의 입력이자, 나중에 실시간 탐지의 원시 신호이기도 하다.
CREATE TABLE IF NOT EXISTS anomaly_signals (
    ts         REAL NOT NULL,
    detector   TEXT NOT NULL,
    score      REAL,              -- 0~1 정규화. 탐지기 간 비교를 위해 필수
    severity   TEXT,              -- 'info' | 'warning' | 'critical'
    features   TEXT,              -- JSON. 어떤 지표가 기여했는가
    run_id     INTEGER            -- 평가 실행 식별. NULL 이면 실시간 탐지
);
CREATE INDEX IF NOT EXISTS idx_anomaly_signals_ts ON anomaly_signals (ts, detector);

-- 스코어보드 이력.
-- 새 탐지기는 여기서 기존 대비 개선을 입증한 뒤에만 채택한다(CLAUDE.md).
-- 이력을 남기는 이유는 회귀 감시다 — 어제보다 나빠졌는지 알아야 한다.
CREATE TABLE IF NOT EXISTS eval_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    detector       TEXT NOT NULL,
    window_start   REAL,
    window_end     REAL,
    scenarios      TEXT,          -- 채점에 포함된 시나리오 목록 (JSON)
    true_positive  INTEGER,
    false_positive INTEGER,
    false_negative INTEGER,
    precision_pct  REAL,
    recall_pct     REAL,
    f1             REAL,
    detect_latency_s REAL,        -- 주입 시작 → 첫 탐지. 이게 실사용 체감을 좌우한다
    fp_per_hour    REAL,          -- 정상 구간 오탐률. 알림 피로의 직접 지표
    params         TEXT,          -- 탐지기 설정 (JSON)
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_ts ON eval_runs (ts, detector);

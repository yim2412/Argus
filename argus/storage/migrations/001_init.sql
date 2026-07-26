-- Phase 0: 최소 스키마.
-- 메트릭 테이블(metrics_raw, process_metrics 등)은 Phase 1 에서 002_ 로 추가한다.

-- 앱 메타데이터. 스키마 버전은 PRAGMA user_version 이 정본이고,
-- 여기에는 진단에 필요한 부가 정보를 둔다.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Argus 자신의 리소스 사용량.
-- 모니터가 병목이 되는 것이 이런 도구의 1순위 실패 모드라, 수집기보다 먼저 만든다.
CREATE TABLE IF NOT EXISTS self_telemetry (
    ts               REAL    NOT NULL,  -- unix epoch (초, 소수점 포함)
    cpu_percent      REAL,              -- Argus 프로세스의 CPU 점유율
    rss_mb           REAL,              -- 상주 메모리 (MB)
    threads          INTEGER,
    handles          INTEGER,           -- Windows 핸들 수 (누수 조기 신호)
    queue_depth      INTEGER,           -- 수집→저장 큐에 밀린 행 수
    drop_count       INTEGER,           -- 큐 가득참으로 버린 누적 행 수
    write_latency_ms REAL,              -- 최근 DB 배치 쓰기 소요
    throttle_level   INTEGER            -- 0=정상, 클수록 강하게 스로틀
);

CREATE INDEX IF NOT EXISTS idx_self_telemetry_ts ON self_telemetry (ts);

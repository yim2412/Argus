-- Phase 1 보강: 시스템 수준 사건 기록.
--
-- 상주 프로그램은 PC 가 절전에 들어갔다 깨어나는 것을 반드시 겪는다. 그때 데이터에
-- 몇 시간짜리 공백이 생기는데, 이를 기록해 두지 않으면 이후 단계가 그 공백을
-- "이상"으로 오인한다. 예를 들어 Phase 3 베이스라인은 이 구간을 학습에서 빼야 하고,
-- Phase 6 변화점 탐지는 복귀 시점의 급변을 이상으로 보면 안 된다.
--
-- 무엇이 언제 끊겼는지는 사후에 복원할 수 없다. 반드시 그 시점에 남겨야 한다.

CREATE TABLE IF NOT EXISTS system_events (
    ts          REAL NOT NULL,
    event       TEXT NOT NULL,   -- 'startup' | 'shutdown' | 'time_gap'
    gap_seconds REAL,            -- time_gap 일 때 공백 길이
    detail      TEXT             -- JSON. 원인 추정·직전 상태 등
);

CREATE INDEX IF NOT EXISTS idx_system_events_ts ON system_events (ts);

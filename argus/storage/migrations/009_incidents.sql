-- Phase 9: 사건(incident) — 사용자에게 보이는 단위.
--
-- Phase 8 이 만든 리포트가 **메모리에만 있었다.** 계산하고 버리고 있었으므로
-- 대시보드도, 피드백도, 알림도 붙일 곳이 없었다. 여기가 그 저장소다.
--
-- **신호(signal)와 사건(incident)은 다르다.** 신호는 탐지기가 낸 원시 판정이라
-- 1초 간격으로 수십 개가 나올 수 있다. 그걸 그대로 보여 주면 사용자는 "같은 일"을
-- 수십 번 읽게 된다. 사건은 그것을 하나로 접은 것이고, 시작·끝·원인·심각도를 갖는다.

CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start        REAL NOT NULL,
    ts_end          REAL,               -- NULL = 진행 중
    severity        TEXT NOT NULL,      -- 'info' | 'warning' | 'critical'
    bottleneck      TEXT,               -- CPU / IO / MEMORY / GPU / THERMAL / CONTENTION
    title           TEXT NOT NULL,
    explanation_md  TEXT,               -- 사람이 읽는 리포트 전문
    contributors    TEXT,               -- JSON: [{name, share, delta, pids, lead_s}, ...]
    evidence        TEXT,               -- JSON: 병목 판정 근거 문자열 목록

    -- 융합 근거. 탐지기 여럿이 같은 시간대에 발화하면 합의로 보고 심각도를 올린다.
    detectors       TEXT,               -- JSON: ["rules", ...]
    signal_count    INTEGER DEFAULT 0,
    peak_score      REAL,

    -- 억제. 상위 사건이 있으면 하위는 묻되 **지우지 않는다** — 왜 안 알렸는지
    -- 설명할 수 있어야 하고, 나중에 억제 규칙이 틀렸는지 검증해야 한다.
    suppressed_by   INTEGER REFERENCES incidents(id),

    -- 알림. 발송은 Phase 9 후반이며 오탐률 검증 전까지 켜지 않는다.
    notified        INTEGER DEFAULT 0,
    notify_skipped  TEXT,               -- 안 보낸 이유 (예산 소진·억제·설정)

    -- 피드백 (Phase 11 의 입력)
    user_label      TEXT,               -- 'normal' | 'real' | NULL
    labeled_at      REAL
);

CREATE INDEX IF NOT EXISTS idx_incidents_ts ON incidents (ts_start DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents (ts_end) WHERE ts_end IS NULL;

-- 어떤 신호가 어느 사건에 들어갔는지. 융합 결과를 나중에 검증하려면 필요하다 —
-- "이 사건은 무엇을 근거로 만들어졌나"에 답하지 못하면 억제·융합 규칙을 고칠 수 없다.
CREATE TABLE IF NOT EXISTS incident_signals (
    incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    detector    TEXT NOT NULL,
    score       REAL,
    PRIMARY KEY (incident_id, ts, detector)
);

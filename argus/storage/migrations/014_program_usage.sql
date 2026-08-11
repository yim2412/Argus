-- 프로그램별 누적 실행 시간.
--
-- **기존 테이블로는 답이 안 나온다.** 실측(관측 383시간)에서 순진한 두 경로가 둘 다 틀렸다:
--
--   * `process_5m` 버킷 합 → chrome 237.8h / discord 238.8h / steam 238.6h.
--     5분 버킷에 한 번이라도 보이면 5분을 다 세므로, 오래 켜 두는 것은 전부 관측
--     전체(238.8h)로 붙는다. 이건 실행 시간이 아니라 *존재 여부*다.
--   * `process_events` 수명 합 → chrome 1,073h. 프로세스 4,443개의 수명을 더한
--     "프로세스-시간"이지 "chrome 이 켜져 있던 시간"이 아니다. 관측 시간을 넘는다.
--
-- 맞는 계산은 **이름 단위 구간 합집합**(겹치면 한 번만) + **관측 세션 클램프**다.
-- 그래서 원본을 접는 새 테이블이 필요하다.
--
-- **단위는 실행 경로가 아니라 이름이다.** 경로로 나누면 자동 업데이트마다 쪼개진다 —
-- 이 PC 의 Discord 는 16일 만에 app-1.0.9249/9250/9251 세 경로가 됐고, 1년이면
-- 수십 줄이 된다. "얼마나 썼나"를 보려는데 그게 조각나면 답이 아니다. 경로로 나눌
-- 실익은 `python` 구분 하나뿐인데 그건 개발 환경에서만 생긴다. 게다가 실행 경로는
-- 민감 정보(설계 규칙 5)라 영구 보관할 이유가 없다. 컬럼 추가는 나중에 쉽고 반대는 어렵다.

CREATE TABLE IF NOT EXISTS program_usage_daily (
    day        TEXT NOT NULL,      -- 'YYYY-MM-DD' 로컬 시간대 (history._day_bounds 와 같은 기준)
    name       TEXT NOT NULL,      -- 정규화된 프로그램 이름 (확장자 없는 소문자)
    seconds    REAL NOT NULL,      -- 그날 켜져 있던 시간. 같은 이름 프로세스가 여럿이어도 한 번만 센다
    launches   INTEGER NOT NULL,   -- 그날 새로 뜬 횟수. 세션 시작 시 이미 떠 있던 것은 세지 않는다
    -- 그날 Argus 가 관측한 시간. **분모를 함께 저장하지 않으면 나중에 복원할 수 없다** —
    -- 원본(`process_events`)은 보존 기한이 지나면 지워지는데, "6시간"은 그날 PC 를
    -- 8시간 켰는지 20시간 켰는지에 따라 뜻이 전혀 다르다.
    observed_s REAL NOT NULL,
    PRIMARY KEY (day, name)
);

CREATE INDEX IF NOT EXISTS idx_program_usage_day ON program_usage_daily (day);

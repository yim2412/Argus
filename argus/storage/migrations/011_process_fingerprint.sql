-- 프로세스 지문 (Phase 6-B). "이 프로그램의 평소는 어디까지인가"를 담는다.
--
-- **6-A(procleak)의 오탐을 줄이는 것이 목적이다.** 6-A 는 자기 과거만 보고 판정하므로
-- "평소에도 핸들을 4천 개씩 쓰는 프로그램"과 "평소 200개인데 4천 개가 된 프로그램"을
-- 구분하지 못한다. 실측에서 medal(게임 녹화)이 핸들 383 → 1,395 로 늘어 발화했는데,
-- medal 의 평소 handles_max p99 는 12,466 이었다 — 완전히 정상 범위였다.
--
-- **단위: 5분 버킷 통계량의 분포다.** 원본 process_metrics 는 24시간만 남으므로 3일치
-- 지문은 버킷 통계를 다시 집계할 수밖에 없는데, 버킷별 p99 를 모아 다시 p99 를 내면
-- 그건 분위수가 아니다 — 그런데 예외도 안 나고 조용히 그럴듯해 보인다. 그래서 무엇을
-- 집계한 것인지 `stat` 에 남긴다. "medal 의 5분 버킷 handles_max 가 평소 어느 범위인가"
-- 이지 "medal 의 순간 핸들 분포"가 아니다.
--
-- **이름 단위의 한계.** PID 는 재시작마다 바뀌어 지문의 단위가 될 수 없어 프로그램
-- 이름을 쓰는데, `python` 처럼 범용적인 이름은 서로 다른 프로그램이 지문을 공유한다
-- (실측: python 의 rss_p95 p99 가 9GB 인데 이는 개발 작업 프로세스들 탓이다).
-- 실행 경로를 아이덴티티에 넣으면 갈리지만 그건 Phase 13 과 함께 판단한다.
--
-- **레짐 축은 비워 둔다.** Phase 4-B(레짐 추론)가 데이터 대기 중이라 기다리면 6-B 도
-- 같이 막힌다. 지금은 'all' 고정이고, 4-B 가 오면 행이 늘어날 뿐 마이그레이션이 없다.

CREATE TABLE IF NOT EXISTS process_fingerprint (
    name        TEXT    NOT NULL,   -- 프로그램 이름 (소문자, 확장자 제외)
    regime      TEXT    NOT NULL,   -- 활동 레짐. Phase 4-B 전까지 'all'
    stat        TEXT    NOT NULL,   -- 집계한 버킷 통계량 (예: handles_max, rss_p95)

    p50         REAL,
    p95         REAL,
    p99         REAL,
    maximum     REAL,

    samples     INTEGER NOT NULL,   -- 집계에 쓴 버킷 수
    days        INTEGER NOT NULL,   -- 관측된 날짜 수 (6시간 이상 켜져 있던 날만)
    built_at    REAL    NOT NULL,

    PRIMARY KEY (name, regime, stat)
);

CREATE INDEX IF NOT EXISTS idx_process_fingerprint_stat
    ON process_fingerprint (stat, name);

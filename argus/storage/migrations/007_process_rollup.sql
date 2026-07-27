-- 프로세스 5분 롤업. Phase 4-A 에서 빠뜨린 절반이다.
--
-- 4-A 는 `metrics_raw` 의 증발만 막았고 `process_metrics` 는 24시간 보존 그대로였다.
-- `metrics_1m` 에 남는 프로세스 정보는 `foreground_proc`·`top_cpu_proc`·`top_cpu_share`
-- 셋뿐이라 분포를 복원할 수 없다. Phase 6(지문)은 "프로세스명 × 레짐 단위 p50/p95/p99"
-- 를 학습하는데, **학습할 데이터가 매일 사라진다.**
--
-- 더 나빴던 것은 보호 장치가 헛돌고 있었다는 점이다. `process_metrics` 는 보존 규칙에서
-- 롤업 워터마크에 묶여 있었지만(`needs_rollup=True`) 정작 그 롤업은 프로세스를 접지
-- 않았다. 접히지도 않은 채 "롤업이 지나갔으니 안전하다"는 이유로 지워지고 있었다.
--
-- **단위는 PID 가 아니라 프로그램 이름이다.** PID 는 재시작마다 바뀌므로 지문의
-- 단위가 될 수 없다. "크롬은 평소 CPU 6%"가 지문이지 "PID 8812 는"이 아니다.
--
-- **버킷마다 상위 N 개만 남긴다.** `cpu > 0` 같은 조건으로 거르면 프로세스가 500개인
-- PC 에서 저장량이 예측 불가로 늘어난다. 배포 대상의 하드웨어를 가정하지 않으려면
-- 행 수에 상한이 있어야 한다. 계획서 3.2 의 "상위 프로세스만"이 이 뜻이다.

CREATE TABLE IF NOT EXISTS process_5m (
    ts_5m         INTEGER NOT NULL,   -- 5분 버킷 시작 (unix epoch, 300 의 배수)
    name          TEXT    NOT NULL,   -- 프로그램 이름 (소문자, 확장자 제외)
    sample_count  INTEGER NOT NULL,   -- 표본 수. 활성 집합이 아니면 30초 해상도라 적다
    pid_count     INTEGER,            -- 그 5분에 관측된 고유 PID 수 (크롬 탭 수 같은 것)

    -- CPU. 지문의 주력이라 분위수를 남긴다 — p99 초과가 이탈 판정의 기준이 된다.
    cpu_mean      REAL,
    cpu_p50       REAL,
    cpu_p95       REAL,
    cpu_p99       REAL,
    cpu_max       REAL,

    -- 메모리. 누수는 여기서 보인다.
    rss_mean      REAL,
    rss_p50       REAL,
    rss_p95       REAL,
    rss_max       REAL,

    -- IO. 평균만으로는 폭주를 못 보므로 최대도 남긴다.
    io_read_bps_mean  REAL,
    io_write_bps_mean REAL,
    io_write_bps_max  REAL,

    -- 핸들·스레드. 핸들 누수는 메모리보다 먼저 드러난다.
    handles_mean  REAL,
    handles_max   INTEGER,
    threads_max   INTEGER,

    -- 이 프로그램이 그 5분에 포어그라운드였던 비율. 레짐 라벨링의 보조 신호.
    foreground_ratio REAL,

    PRIMARY KEY (ts_5m, name)
);

CREATE INDEX IF NOT EXISTS idx_process_5m_name ON process_5m (name, ts_5m);

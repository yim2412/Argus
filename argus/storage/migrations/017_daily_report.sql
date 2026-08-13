-- 하루 요약 (일일 생산성 리포트).
--
-- **원본을 그때그때 집계하지 않고 미리 접어 남긴다.** 이유는 하나뿐이다 — 원본
-- (`process_metrics`)은 이틀이면 지워지는데, "어제 무엇을 했나"는 한 달 뒤에도
-- 답할 수 있어야 한다. 요약은 영구 보존 가치가 있고 행 하나가 하루치다.
--
-- **포어그라운드 초는 `process_metrics` 를 직접 센다** (2026-08-13 측정으로 정함).
-- 설계 초안은 `process_5m` 의 5분 버킷을 쓰려 했는데, 그 테이블은
-- `ProcessRollup(top_n=40)` 이 CPU 상위 40 ∪ RSS 상위 40 만 남긴 것이라 **포어그라운드
-- 여부가 선정에 들어가지 않는다.** 실측(접힌 79버킷 안):
--
--   chrome · league of legends · discord · explorer   손실 0%
--   windowsterminal                                   손실 11.6%
--   rainbowsix_be                                     손실 90.1%
--
-- 즉 무거운 프로그램은 멀쩡하고 **가벼운 앱만 조용히 깎인다** — 터미널로 두 시간
-- 작업한 날이 리포트에서 사라지는 셈이라, 하필 이 리포트가 답하려는 질문에서 제일
-- 나쁜 방향이다. 원본은 1초 해상도이고 절단이 없다(79버킷 23,700초 중 21,218초를
-- 포어그라운드로 잡았다 — 89.5%).
--
-- 그 대가로 이 롤업은 **원본이 지워지기 전에 접어야** 한다. `retention` 이
-- `process_metrics` 를 이 롤업의 워터마크로도 붙잡는다(`ProgramUsageRollup` 이
-- `process_events` 를 붙잡는 것과 같은 구조).

CREATE TABLE IF NOT EXISTS daily_report (
    day        TEXT PRIMARY KEY,   -- 'YYYY-MM-DD' 로컬. program_usage_daily 와 같은 기준
    total_s    REAL NOT NULL,      -- 그날 포어그라운드 총 시간

    -- 분모. **함께 저장하지 않으면 나중에 복원할 수 없다**(014 교훈). "3시간 작업"은
    -- 그날 PC 를 4시간 켰는지 16시간 켰는지에 따라 뜻이 전혀 다른데, 원본이 지워지고
    -- 나면 그 4시간·16시간을 되돌릴 방법이 없다.
    observed_s REAL NOT NULL,

    by_category TEXT NOT NULL,     -- JSON {카테고리: 초}
    top_apps    TEXT NOT NULL,     -- JSON [{name, seconds, category}] 상위 5
    by_slot     TEXT NOT NULL,     -- JSON {새벽|오전|오후|저녁: 초}
    built_at    REAL NOT NULL
);

-- 전날 대비 증감은 **저장하지 않는다.** 이웃 두 행의 뺄셈이라 저장하면 같은 값이 두
-- 곳에 생기고, 어제 리포트가 나중에 다시 만들어지면 둘이 갈린다.

-- 프로그램이 무엇인지 — 이름 옆에 붙일 사람 말 설명.
--
-- **"svchost 238시간"은 정보가 아니다.** 사용시간·프로세스 표에서 상위를 차지하는
-- 것 대부분이 이름만으로는 무엇인지 알 수 없는 것들이다(svchost · dwm · ctfmon ·
-- ace-service64 · aces). 사용자가 자기 PC 에서 무엇이 도는지 판단하려면 그 이름이
-- 무엇인지부터 알아야 한다.
--
-- 출처는 **exe 의 버전 리소스**(Windows 가 파일에 심어 둔 것)다. 새 의존성이 없고
-- (`ctypes` 로 version.dll 을 부른다) 한글 Windows 에서는 한글 설명이 나온다:
--
--   svchost  -> Host Process for Windows Services  (Microsoft Corporation)
--   dwm      -> 데스크톱 창 관리자                   (Microsoft Corporation)
--   chrome   -> Google Chrome                      (Google LLC)
--
-- 실측(2026-08-12, 이름 405종): 3.6초 · 개당 8.8ms · 성공 289/405(71%). 못 읽는
-- 것은 파일이 이미 지워졌거나 버전 리소스가 없는 exe 다.
--
-- **실행 경로를 저장하지 않는다.** 경로는 민감 정보고(설계 규칙 5), 014 에서 같은
-- 이유로 사용시간 테이블에서도 뺐다. 경로가 다시 필요하면 `process_events.exe` 에
-- 보존 기한만큼 남아 있다.
--
-- **단위는 이름이다.** 사용시간 테이블과 같은 키여야 조인이 성립한다. 같은 이름의
-- 다른 실행 경로는 가장 최근 것 하나로 대표한다 — 자동 업데이트로 경로가 갈리는
-- 것(Discord 는 16일에 세 경로)을 여기서 다시 쪼갤 이유가 없다.

CREATE TABLE IF NOT EXISTS program_info (
    name        TEXT PRIMARY KEY,   -- 정규화된 프로그램 이름 (program_usage_daily 와 같은 기준)
    description TEXT,               -- FileDescription. 못 읽었으면 NULL
    company     TEXT,               -- CompanyName. 서명 검증이 아니라 표시용이다
    -- **실패도 기록한다.** 안 그러면 못 읽는 이름 116개를 매 회차 다시 연다.
    attempts    INTEGER NOT NULL DEFAULT 0,
    checked_at  REAL NOT NULL
);

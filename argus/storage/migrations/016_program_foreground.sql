-- 이 프로그램을 사람이 직접 쓰는가.
--
-- **사용시간 표의 상위가 전부 배경 서비스였다.** svchost 238h · conhost 209h ·
-- runtimebroker 200h — 정의대로 동작한 결과지만("켜져 있던 시간"), 사용자가 알고
-- 싶은 것은 "내가 무엇을 얼마나 했나"다. 둘을 가르는 신호가 필요하다.
--
-- **포어그라운드 이력으로 가른다.** 창을 띄워 앞에 놓인 적이 있으면 사람이 쓰는
-- 프로그램이다. 실측(2026-08-12, 이름 213종 중 27종이 해당):
--
--   걸리는 것   chrome · discord · league of legends · fczf · fm · civilizationvi ·
--               kakaotalk · op.gg · steamwebhelper · explorer · taskmgr …
--   빠지는 것   svchost · conhost · runtimebroker · crashpad_handler · node ·
--               saclient · searchprotocolhost · applicationframehost …
--
-- 경로나 회사명으로는 안 갈린다 — crashpad_handler(사용자 폴더)·node·saclient
-- (Program Files)가 전부 통과하고, Microsoft 를 빼면 탐색기·작업관리자까지 사라진다.
--
-- **한 번 참이면 계속 참이다.** "사람이 쓰는 프로그램"은 뒤집히는 성질이 아니고,
-- 포어그라운드 원본(`process_5m`)은 이틀이 지나면 웜으로 옮겨가 SQLite 에서
-- 사라진다. 매번 다시 판정하려 하면 사흘 전에 한 게임이 목록에서 빠진다.

ALTER TABLE program_info ADD COLUMN foreground_seen INTEGER NOT NULL DEFAULT 0;

-- 웜(Parquet)까지 훑는 백필을 한 번만 하기 위한 표시. `meta` 에 두지 않는 이유는
-- 이 값이 program_info 의 상태이지 전역 설정이 아니기 때문이다.
CREATE TABLE IF NOT EXISTS program_info_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

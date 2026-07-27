-- 네트워크 활동 5분 집계.
--
-- `net_connections` 는 72시간만 보존된다. 프로세스 롤업 때와 같은 문제 — 접지 않으면
-- 네트워크 이력이 3일마다 사라지고, Phase 13(보안)과 Phase 6(지문)이 학습할 것이
-- 남지 않는다.
--
-- **개별 원격 주소를 저장하지 않는다.** 두 가지 이유가 겹친다.
--
-- 1. **신호가 되지 않는다.** 실측: 12.5시간에 고유 원격 주소 781개, (프로그램 × IP)
--    조합 1,214개. /24 로 묶어도 1,029개로 거의 줄지 않는다(CDN 이 여러 대역에
--    흩어져 있다). "처음 보는 주소"를 알리면 시간당 87건이 나온다 — 알림이 아니라
--    소음이다. 의미 있는 단위는 ASN·도메인인데, 그건 외부 조회를 요구한다.
-- 2. **개인정보다.** 네트워크 목적지는 어떤 사이트를 쓰는지 그대로 드러낸다.
--    원본은 72시간 뒤 사라지는데 집계가 그걸 영구 보관하면 보존 정책을 우회하는 셈이다.
--
-- 계획서가 원하는 신호("단시간 다수 신규 원격 IP")는 **개수만으로** 잡힌다.
-- "크롬이 평소 대상 200개인데 지금 2,000개"에 개별 주소는 필요 없다.

CREATE TABLE IF NOT EXISTS net_activity_5m (
    ts_5m         INTEGER NOT NULL,
    name          TEXT    NOT NULL,   -- 프로그램 이름
    sample_count  INTEGER NOT NULL,   -- 이 창에서 관측된 스냅샷 수
    conn_count    INTEGER,            -- 연결 수(스냅샷 평균)
    distinct_remotes INTEGER,         -- 고유 원격 주소 수  ← 급증이 신호다
    distinct_ports   INTEGER,         -- 고유 원격 포트 수
    listen_count     INTEGER,         -- LISTEN 상태 수 (서비스를 새로 여는 것도 신호)
    PRIMARY KEY (ts_5m, name)
);

CREATE INDEX IF NOT EXISTS idx_net_activity_name ON net_activity_5m (name, ts_5m);

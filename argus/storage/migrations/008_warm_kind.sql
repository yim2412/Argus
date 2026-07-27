-- 웜 내보내기 기록에 종류(metrics / process)를 넣는다.
-- 한 날짜에 Parquet 파일이 둘이 되므로 `date_key` 만으로는 키가 되지 않는다.
--
-- **왜 007 에 넣지 않고 새 파일로 分離했는가.** 007 은 이미 이 개발 머신에 적용된
-- 뒤였다. 적용된 마이그레이션은 다시 실행되지 않으므로(`PRAGMA user_version` 기준),
-- 007 을 고쳐도 이미 올린 DB 에는 반영되지 않는다. 개발 중에는 DB 를 지우면 그만이지만
-- **배포 후라면 그 사용자는 변경분을 영영 받지 못한다.** 적용된 마이그레이션은
-- 고치지 않는다 — 새 번호를 붙인다.
--
-- 재생성하는 이유: SQLite 는 PRIMARY KEY 변경을 지원하지 않는다. 006 이 만든 이
-- 테이블은 아직 한 번도 쓰이지 않았으므로(실측 0행) 지금이 바꿀 수 있는 마지막
-- 시점이다. 이미 데이터가 있었다면 옮겨 담아야 했다.

DROP TABLE IF EXISTS warm_exports;
CREATE TABLE warm_exports (
    date_key    TEXT NOT NULL,      -- 'YYYY-MM-DD' (로컬 시각 기준)
    kind        TEXT NOT NULL,      -- 'metrics' | 'process'
    path        TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    bytes       INTEGER,
    exported_at REAL NOT NULL,
    PRIMARY KEY (date_key, kind)
);

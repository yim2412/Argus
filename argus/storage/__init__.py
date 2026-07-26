"""저장소. 핫(SQLite)만 Phase 0 에 있고, 웜(Parquet/DuckDB)은 Phase 1 이후.

`python -m argus.storage.hot` 스모크 실행 시 이중 임포트 경고가 뜨지 않도록
여기서 하위 모듈을 미리 임포트하지 않는다.
"""

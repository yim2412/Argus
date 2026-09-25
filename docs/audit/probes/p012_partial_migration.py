"""F-012 프로브 — 마이그레이션이 중간에 실패하면 앞 문장이 되돌려지는가.

`Database._migrate` 는 `executescript(sql)` 후 실패하면 `rollback()` 한다. 그런데
`sqlite3.Connection.executescript` 는 실행 전에 열린 트랜잭션을 COMMIT 하고 스크립트를
자동 커밋으로 돌린다 — rollback 할 것이 없다. 그러면:
  1) 앞 문장(ALTER TABLE ADD COLUMN 등)은 남고 user_version 은 안 올라간다
  2) 다음 기동에 같은 파일을 다시 돌려 `duplicate column` 으로 **영원히 못 연다**

합성 마이그레이션 2개(정상 001 + 앞 문장 성공·뒷 문장 실패인 002)로 잰다.
PASS = 실패 뒤 앞 문장의 흔적이 없다(원자적). FAIL = 흔적이 남고 재기동도 실패.
"""
import logging
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
sys.stdout.reconfigure(encoding="utf-8")

from argus.storage import hot  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp())
mig = tmp / "migrations"
mig.mkdir()
(mig / "001_init.sql").write_text(
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);\nCREATE TABLE t (a INTEGER);\n",
    encoding="utf-8")
(mig / "002_bad.sql").write_text(
    "ALTER TABLE t ADD COLUMN b INTEGER;\nSELECT * FROM 없는_표;\n", encoding="utf-8")

orig = hot.migration_files
hot.migration_files = lambda: sorted(
    (int(p.name[:3]), p) for p in mig.iterdir())  # 같은 형식으로 대체

db_file = tmp / "x.db"
first = second = None
try:
    hot.Database(db_file).open()
except Exception as e:  # noqa: BLE001
    first = type(e).__name__
cols = [r[1] for r in sqlite3.connect(db_file).execute("PRAGMA table_info(t)")]
ver = sqlite3.connect(db_file).execute("PRAGMA user_version").fetchone()[0]

# 잘못된 002 를 고쳐 배포했다고 치고 다시 연다 — 앞 문장이 남았으면 여기서 막힌다
(mig / "002_bad.sql").write_text("ALTER TABLE t ADD COLUMN b INTEGER;\n", encoding="utf-8")
try:
    hot.Database(db_file).open()
    second = "ok"
except Exception as e:  # noqa: BLE001
    second = f"{type(e).__name__}: {e}"
hot.migration_files = orig

print(f"1차 기동: {first} · 실패 뒤 t 컬럼 {cols} · user_version {ver}")
print(f"고친 002 로 재기동: {second}")
atomic = "b" not in cols
print("[PASS] 실패한 마이그레이션이 원자적으로 되돌려졌다" if atomic
      else "[FAIL] 앞 문장이 남았다 — 고친 마이그레이션도 다시 못 돈다" if second != "ok"
      else "[FAIL] 앞 문장이 남았다")
sys.exit(0 if atomic else 1)

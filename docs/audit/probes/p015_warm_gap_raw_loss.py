"""F-015 프로브 — 웜 내보내기가 하루 실패하면 그날 초 단위 원본이 지워지는가.

`WarmStore.raw_watermark()` 는 종류마다 `MAX(date_key)`(내보낸 **가장 늦은** 날짜)를 쓴다.
중간 날짜 하나가 실패해도(`export_pending` 은 로그만 남기고 다음 날짜로 간다) 워터마크는
그 뒤까지 가고, `Retention` 은 워터마크까지 지운다 → 한 번도 안 나간 날의 원본이 사라진다.

격리한 데이터 폴더에서: 3일치 원본 → 09-02 의 raw_metrics 내보내기만 실패시킴 →
export_pending → 롤업 워터마크는 충분히 앞으로(웜 워터마크만 관문이 되게) → purge_once.

PASS = 09-02 원본이 SQLite 에 남아 있거나 웜 파일로 나가 있다(어느 쪽이든 복구 가능).
FAIL = 둘 다 없다 — 복구 불가능한 손실.
대조: 실패를 주입하지 않으면 09-02 도 웜으로 나가야 한다(그게 안 되면 프로브가 틀렸다).
"""
import datetime as dt
import logging
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.CRITICAL)


def run(inject_failure: bool) -> tuple[int, bool]:
    os.environ["ARGUS_DATA_DIR"] = tempfile.mkdtemp(prefix="p015_")
    from argus.config.loader import RetentionSettings, WarmSettings
    from argus.storage import warm
    from argus.storage.hot import Database
    from argus.storage.retention import Retention

    base = dt.datetime(2026, 9, 1, 12, 0).timestamp()
    with Database() as db:
        for d in range(3):
            t = base + d * 86400
            db.insert_many("metrics_raw", ["ts", "cpu_total"], [(t + i, 10.0) for i in range(10)])
            db.insert_many("gpu_metrics", ["ts", "gpu_index", "util_percent"], [(t + i, 0, 5.0) for i in range(10)])
            db.insert_many("process_metrics", ["ts", "pid", "name", "cpu_percent"], [(t + i, 1, "x", 1.0) for i in range(10)])
        store = warm.WarmStore(db, WarmSettings())
        if inject_failure:
            orig = store.export_date

            def flaky(date_key, kind="metrics"):
                if date_key == "2026-09-02" and kind == "raw_metrics":
                    raise OSError("주입: 파일 잠금(백신) 등으로 교체 실패")
                return orig(date_key, kind)

            store.export_date = flaky
        store.export_pending(dt.datetime(2026, 9, 10, 12, 0).timestamp())

        # 롤업 워터마크는 충분히 앞으로 — 웜 워터마크만 관문이 되게 한다
        now = time.time()
        for name in ("metrics_1m", "process_5m", "daily_report"):
            db.execute("INSERT OR REPLACE INTO rollup_state (name, watermark_ts, updated_at) VALUES (?, ?, ?)",
                       (name, now, now)) if hasattr(db, "execute") else db.conn.execute(
                "INSERT OR REPLACE INTO rollup_state (name, watermark_ts, updated_at) VALUES (?, ?, ?)",
                (name, now, now))
        db.conn.commit()
        Retention(db, RetentionSettings()).purge_once()

        lo = dt.datetime(2026, 9, 2).timestamp()
        hot_rows = db.query("SELECT COUNT(*) AS c FROM metrics_raw WHERE ts >= ? AND ts < ?", (lo, lo + 86400))[0]["c"]
        exported = bool(db.query("SELECT 1 FROM warm_exports WHERE kind='raw_metrics' AND date_key='2026-09-02' AND row_count > 0"))
    return hot_rows, exported


ctrl_hot, ctrl_exported = run(inject_failure=False)
print(f"대조(실패 없음): 09-02 원본 SQLite {ctrl_hot}행 · 웜으로 나감 {ctrl_exported}")
if not ctrl_exported:
    print("[FAIL] 대조가 성립하지 않는다 — 실패 없이도 09-02 가 안 나갔다. 프로브를 먼저 고친다")
    sys.exit(2)
hot, exported = run(inject_failure=True)
print(f"실패 주입: 09-02 원본 SQLite {hot}행 · 웜으로 나감 {exported}")
if hot or exported:
    print("[PASS] 실패한 날의 원본이 남아 있다")
    sys.exit(0)
print("[FAIL] 내보내기가 실패한 날의 원본이 SQLite 에서도 지워졌다 — 복구 불가능")
sys.exit(1)

"""독립 리뷰 #7 프로브 — 사건 하나를 닫다 예외가 나면 융합 전체가 멈추는가.

`Fusion.run_once` 는 신호를 돌며 사건을 열고·붙이고·닫은 **뒤에** 워터마크를 옮긴다. 도중에
`_close`(→ `analyze_incident`)가 예외를 던지면 워터마크가 그대로라, 다음 틱이 같은 신호를 다시 읽는다.
그 사건의 데이터가 계속 예외를 부르면 **매 틱 같은 자리에서 죽고, 그 뒤의 신호는 영영 처리되지 않는다.**

격리한 데이터 폴더에서: 신호 A(t+10) · 신호 B(t+300, gap 120 을 넘어 A 의 사건을 닫게 함).
`close_incident` 가 A 의 사건에서만 예외를 던지게 한다(분석 버그 흉내). 틱 3번.

PASS = B 가 사건으로 열린다(A 의 실패가 B 를 막지 않는다).  FAIL = B 가 안 열린다.
대조: 예외가 없으면 사건 2개(A·B)여야 한다.
"""
import json
import logging
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
logging.disable(logging.CRITICAL)


def run(broken: bool) -> tuple[int, int, bool]:
    os.environ["ARGUS_DATA_DIR"] = tempfile.mkdtemp(prefix="pr07_")
    from argus.decide import fusion
    from argus.storage.hot import Database

    t = 1_790_000_000.0
    with Database() as db:
        db.insert_many("anomaly_signals", ["ts", "detector", "score", "severity", "features"],
                       [(t + 10, "rules", 0.8, "warning", json.dumps({"rule": "A"})),
                        (t + 300, "rules", 0.8, "warning", json.dumps({"rule": "B"}))])
        f = fusion.Fusion(db, fusion.FusionSettings())
        f._set_watermark(t)  # noqa: SLF001
        orig = fusion.close_incident

        def flaky(db_, incident_id, ts_end, settings=None):
            row = db_.query("SELECT ts_start FROM incidents WHERE id = ?", (incident_id,))
            if broken and row and row[0]["ts_start"] == t + 10:
                raise ValueError("주입: 이 사건의 분석이 예외를 던진다")
            return orig(db_, incident_id, ts_end, settings)

        fusion.close_incident = flaky
        errors = 0
        try:
            for k in range(3):
                try:
                    f.run_once(now=t + 400 + k * 10)
                except ValueError:
                    errors += 1
        finally:
            fusion.close_incident = orig
        n = db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"]
        b_open = bool(db.query("SELECT 1 FROM incidents WHERE ts_start = ?", (t + 300,)))
    return n, errors, b_open


ctrl_n, _, ctrl_b = run(broken=False)
print(f"대조(예외 없음): 사건 {ctrl_n}개 · B 열림 {ctrl_b}")
if ctrl_n != 2 or not ctrl_b:
    print("[FAIL] 대조가 성립하지 않는다 — 프로브를 먼저 고친다")
    sys.exit(2)
n, errors, b_open = run(broken=True)
print(f"A 닫기 예외: 틱 3번 중 예외 {errors}번 · 사건 {n}개 · B 열림 {b_open}")
if b_open and errors <= 1:
    print("[PASS] 한 사건의 실패가 뒤의 신호를 막지 않는다")
    sys.exit(0)
print("[FAIL] 사건 하나의 닫기 예외가 매 틱 되풀이되며 융합을 멈춘다")
sys.exit(1)

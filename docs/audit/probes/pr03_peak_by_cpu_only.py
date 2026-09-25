"""독립 리뷰 #3 프로브 — 사건의 "최악 시점"을 CPU 최대 행 하나로 골라 디스크 사건을 놓치는가.

`fusion._peak_and_baselines` 는 구간에서 `cpu_total` 이 가장 큰 행을 병목 판정의 입력으로 쓴다.
디스크가 막힌 순간과 CPU 가 튄 순간이 다르면 판정은 디스크가 멀쩡한 행을 본다.

격리한 데이터 폴더에서: 평소 30분(CPU 10% · 디스크 응답 1ms) → 60초 사건(디스크 응답 200ms ·
큐 6 · CPU 15%) 안에 **1초만** CPU 95% (그 순간 디스크는 평소). 사건은 디스크 룰이 열었다.

PASS = 병목이 IO.  FAIL = IO 가 아니다(무엇으로 나왔는지 찍는다).
대조: CPU 튄 1초를 빼면 IO 여야 한다(아니면 프로브가 틀렸다).
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

COLS = ["ts", "cpu_total", "disk_resp_ms", "disk_queue", "mem_percent", "ctx_switches_ps"]


def run(with_cpu_spike: bool) -> str:
    os.environ["ARGUS_DATA_DIR"] = tempfile.mkdtemp(prefix="pr03_")
    from argus.decide import fusion
    from argus.storage.hot import Database

    t0 = 1_790_000_000.0
    rows = []
    for i in range(1800):                      # 평소 — 작은 흔들림(퇴화 통계가 되지 않게)
        w = (i % 7) / 7
        rows.append((t0 + i, 10 + 2 * w, 1 + 0.3 * w, 0.1 * w, 40 + w, 5000 + 300 * w))
    start = t0 + 1800
    for i in range(60):                        # 사건 — 디스크가 막힌다
        rows.append((start + i, 15.0, 200.0, 6.0, 40.5, 5100.0))
    if with_cpu_spike:                         # 1초만 CPU 가 튀고, 그 순간 디스크는 평소
        rows[1800 + 55] = (start + 55, 95.0, 1.0, 0.0, 40.5, 5100.0)
    with Database() as db:
        db.insert_many("metrics_raw", COLS, rows)
        signal = {"ts": start + 10, "detector": "rules", "score": 0.8}
        iid = fusion.open_incident(db, signal, "warning")
        db.insert_many("anomaly_signals", ["ts", "detector", "score", "severity", "features"],
                       [(signal["ts"], "rules", 0.8, "warning",
                         json.dumps({"rule": "디스크 응답 지연", "evidence": {"disk_resp_ms": 200.0}}))])
        fusion._attach_signal(db, iid, signal)  # noqa: SLF001
        result = fusion.analyze_incident(db, iid, start + 59)
    return result.bottleneck.kind if result and result.bottleneck else "없음"


ctrl = run(with_cpu_spike=False)
print(f"대조(CPU 튐 없음): 병목 {ctrl}")
if ctrl != "IO":
    print("[FAIL] 대조가 성립하지 않는다 — 튐 없이도 IO 가 아니다. 프로브를 먼저 고친다")
    sys.exit(2)
kind = run(with_cpu_spike=True)
print(f"CPU 1초 튐: 병목 {kind}")
if kind == "IO":
    print("[PASS] 디스크 사건이 IO 로 판정된다")
    sys.exit(0)
print(f"[FAIL] 59초 동안 디스크가 막힌 사건이 CPU 1초 튐 때문에 {kind} 로 판정된다")
sys.exit(1)

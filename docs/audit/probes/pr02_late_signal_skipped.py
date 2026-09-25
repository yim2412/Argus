"""독립 리뷰 #2 프로브 — 융합 워터마크를 넘긴 뒤에 기록된 신호가 사건에 영영 안 들어가는가.

`Fusion.run_once` 는 탐지 시각(`ts`)이 워터마크 뒤인 신호만 읽고, 워터마크를 `now - lag_s` 로 옮긴다.
신호가 탐지된 뒤 `lag_s` 보다 늦게 DB 에 쓰이면(락 정체 — 실측 10초 초과 하루 1~3건·최악 115초.
제품은 `lag_s` 를 넘기지 않아 코드 기본 **15초**다 — `__main__.py` 의 `FusionSettings(...)`)
그 신호의 `ts` 는 이미 워터마크 뒤에 있어 다음 실행도 읽지 않는다.

격리한 데이터 폴더에서: 워터마크 T → 융합 실행(now = T+120, 신호 아직 없음) → 탐지 시각 T+100 의
신호가 이제서야(탐지 20초 뒤) 기록됨 → 융합 실행(now = T+400).

PASS = 그 신호가 사건에 들어간다.  FAIL = 사건 0개(신호는 테이블에 있는데 영영 안 읽힌다).
대조: 신호가 제때(첫 실행 전에) 기록되면 사건이 1개 생겨야 한다.
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


def run(late: bool) -> int:
    os.environ["ARGUS_DATA_DIR"] = tempfile.mkdtemp(prefix="pr02_")
    from argus.decide import fusion
    from argus.storage.hot import Database

    t = 1_790_000_000.0
    settings = fusion.FusionSettings()       # 제품과 같다 — lag_s 를 넘기지 않는다(15초)
    with Database() as db:
        f = fusion.Fusion(db, settings)
        f._set_watermark(t)  # noqa: SLF001

        def write_signal():
            db.insert_many("anomaly_signals", ["ts", "detector", "score", "severity", "features"],
                           [(t + 100, "rules", 0.8, "warning", json.dumps({"rule": "x"}))])

        if not late:
            write_signal()
        f.run_once(now=t + 120)              # lag 15 → 워터마크 t+105 (신호 ts t+100 을 지난다)
        if late:
            write_signal()                   # 탐지 20초 뒤에야 기록됨(락 정체)
        f.run_once(now=t + 400)
        return db.query("SELECT COUNT(*) AS c FROM incidents")[0]["c"]


ctrl = run(late=False)
print(f"대조(제때 기록): 사건 {ctrl}개")
if ctrl != 1:
    print("[FAIL] 대조가 성립하지 않는다 — 제때 기록해도 사건이 1개가 아니다. 프로브를 먼저 고친다")
    sys.exit(2)
n = run(late=True)
print(f"늦게 기록(탐지 후 20초): 사건 {n}개")
if n == 1:
    print("[PASS] 늦게 기록된 신호도 사건에 들어간다")
    sys.exit(0)
print("[FAIL] 워터마크를 넘긴 뒤 기록된 신호가 영영 읽히지 않는다")
sys.exit(1)

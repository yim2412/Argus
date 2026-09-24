"""결정론 회귀(골든) — 합성 입력을 탐지 → 융합 → 설명까지 끝까지 통과시킨다 (2026-09-25 감사).

**왜 필요한가.** 이 프로젝트의 테스트는 전부 한 칸짜리다(탐지기 하나, 병목 분류 하나).
문턱·가중치·정렬이 조용히 바뀌어 **사건 제목이나 1순위 원인이 달라져도** 칸마다의 단언은
통과할 수 있다. 여기서는 같은 입력을 제품 경로 전체에 넣고 결과를 통째로 대조한다.
의도한 변경이면 `--update` 로 갱신하고 커밋 메시지에 **무엇이 왜 바뀌었는지** 적는다.
아니면 버그다.

입력은 코드로 만든다(실데이터 0 — 프로세스명은 `synth_*`). 시각은 고정 `T0` 이고
잡음은 고정 seed 다. `ARGUS_DATA_DIR` 을 임시 폴더로 돌려 **이 PC 의 사용자 설정이
결과에 섞이지 않게** 한다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\golden_replay.py            # 대조
    .venv\\Scripts\\python.exe tools\\audit\\golden_replay.py --update   # 갱신
    .venv\\Scripts\\python.exe tools\\audit\\golden_replay.py --print    # 현재 결과만
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "replay_v1.json"

# 고정 시각. 2026-09-01 00:00:00 UTC. 벽시계에 기대는 코드가 있으면 두 번 돌렸을 때
# 결과가 달라지므로 그것도 여기서 드러난다.
T0 = 1_788_220_800.0
BASE_S = 3 * 3600  # 베이스라인이 설 만큼의 정상 구간
SEED = 20260925

PROC_COLS = ("ts", "pid", "name", "cpu_percent", "rss_mb", "io_read_bps", "io_write_bps",
             "handles", "threads", "foreground")
RAW_COLS = ("ts", "cpu_total", "cpu_max_core", "mem_percent", "disk_resp_ms")

# (시나리오, 시작 오프셋 s, 길이 s, 주범 pid, 주범 이름)
FAULTS = [
    ("cpu_spin", BASE_S + 600, 300, 901, "synth_spinner"),
    ("handle_leak", BASE_S + 1800, 600, 902, "synth_leaker"),
    ("memory_leak", BASE_S + 3300, 600, 903, "synth_hog"),
]


def build_db(db) -> None:
    rng = random.Random(SEED)
    end = BASE_S + 4200
    raw, procs = [], []
    for i in range(end):
        ts = T0 + i
        cpu = 12.0 + rng.gauss(0, 1.5)
        mem = 35.0 + rng.gauss(0, 0.3)
        spin = next((f for f in FAULTS if f[0] == "cpu_spin" and f[1] <= i < f[1] + f[2]), None)
        hog = next((f for f in FAULTS if f[0] == "memory_leak" and f[1] <= i < f[1] + f[2]), None)
        if spin:
            cpu += 70.0
        if hog:
            mem += (i - hog[1]) * 0.05
        raw.append((ts, max(cpu, 0.0), min(cpu * 2, 100.0), mem, 0.3 + abs(rng.gauss(0, 0.05))))
        if i % 2:
            continue
        # 정상 배경 프로세스 셋
        for pid, name, c, r in ((100, "synth_browser", 5.0, 900.0), (101, "synth_editor", 2.0, 300.0),
                                (102, "synth_av", 1.0, 150.0)):
            procs.append((ts, pid, name, c + rng.gauss(0, 0.5), r + rng.gauss(0, 2.0), 0, 0,
                          400 + rng.randint(-3, 3), 20, 0))
        for scen, start, dur, pid, name in FAULTS:
            if not (start - 300 <= i < start + dur):
                continue
            k = max(0, i - start)
            active = i >= start
            cpu_p = 70.0 if (scen == "cpu_spin" and active) else 0.5
            rss = 80.0 + (k * 3.0 if scen == "memory_leak" and active else 0.0)
            handles = 300 + (k * 8 if scen == "handle_leak" and active else 0)
            procs.append((ts, pid, name, cpu_p, rss, 0, 0, handles, 4, 0))
    db.insert_many("metrics_raw", RAW_COLS, raw)
    db.insert_many("process_metrics", PROC_COLS, procs)
    db.insert_many(
        "fault_injections",
        ("id", "scenario", "ts_start", "ts_end", "pid", "params", "ramp", "completed"),
        [(n + 1, s, T0 + st, T0 + st + d, pid, "{}", 0, 1)
         for n, (s, st, d, pid, _) in enumerate(FAULTS)],
    )


def _r(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _r(v) for k, v in sorted(x.items())}
    if isinstance(x, (list, tuple)):
        return [_r(v) for v in x]
    return x


def compute() -> dict:
    from argus.decide.fusion import Fusion
    from argus.detection import registry
    from argus.detection.base import run_detector
    from argus.detection.live import SIGNAL_COLUMNS
    from argus.eval import scoring
    from argus.eval.attribution import score_all_product
    from argus.eval.replay import Replayer
    from argus.logging_setup import setup
    from argus.storage.hot import Database

    setup(level="ERROR")
    out: dict = {"input": {"seed": SEED, "t0": T0, "faults": [f[:3] for f in FAULTS]}}
    with tempfile.TemporaryDirectory() as d:
        with Database(pathlib.Path(d) / "golden.db") as db:
            build_db(db)
            rp = Replayer(db)
            window = rp.available_window()
            episodes = scoring.load_episodes(db, window, None)
            obs = list(rp.stream(window))
            out["observations"] = len(obs)
            out["detectors"] = {}
            all_rows = []
            for name in registry.names():
                dets = run_detector(registry.build(name), obs)
                res = scoring.score(name, dets, episodes, window, observations=obs)
                out["detectors"][name] = _r({
                    "tp": res.true_positive, "fp": res.false_positive, "fn": res.false_negative,
                    "alarms": res.alarms, "latencies_s": res.latencies_s, "missed": res.missed,
                })
                if name in ("rules", "procleak"):  # 상주 구성(detection.detector)과 같게
                    all_rows += [x.to_row() for x in dets]
            db.insert_many("anomaly_signals", SIGNAL_COLUMNS, all_rows)
            fusion = Fusion(db)
            fusion._set_watermark(T0 - 1)  # noqa: SLF001 - 리플레이는 워터마크를 명시한다
            fusion.run_once(now=T0 + BASE_S + 4200 + 3600)
            out["incidents"] = [
                _r({k: row[k] for k in ("ts_start", "ts_end", "severity", "bottleneck", "title")})
                for row in db.query("SELECT * FROM incidents ORDER BY ts_start")
            ]
            out["attribution"] = [
                _r({"scenario": v.scenario, "skipped": v.skipped, "resource": v.resource,
                    "top1": v.is_top1, "title": v.title})
                for v in score_all_product(db)
            ]
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="골든 리플레이")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    tmp_home = tempfile.mkdtemp(prefix="argus_golden_")
    os.environ["ARGUS_DATA_DIR"] = tmp_home  # 이 PC 의 사용자 설정이 섞이지 않게
    for k in [k for k in os.environ if k.startswith("ARGUS_") and "__" in k]:
        del os.environ[k]
    sys.path.insert(0, str(ROOT))

    result = compute()
    text = json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True)
    if args.print:
        print(text)
        return 0
    if args.update:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text + "\n", encoding="utf-8")
        print(f"[OK] 골든 갱신: {GOLDEN.relative_to(ROOT)}")
        return 0
    if not GOLDEN.exists():
        print("[FAIL] 골든이 없다 — --update 로 만든다")
        return 1
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if expected == json.loads(text):
        print(f"[OK] golden_replay — 탐지기 {len(result['detectors'])} · "
              f"사건 {len(result['incidents'])} · 귀인 {len(result['attribution'])}")
        return 0
    import difflib

    diff = difflib.unified_diff(
        json.dumps(expected, ensure_ascii=False, indent=1, sort_keys=True).splitlines(),
        text.splitlines(), "golden", "now", lineterm="", n=2)
    print("\n".join(list(diff)[:80]))
    print("[FAIL] golden_replay — 의도한 변경이면 --update, 아니면 버그")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""감사 게이트 — CI 가 없으니 이걸 한 번 돌린다 (2026-09-25 전면 감사).

    pytest            통과 + **건수 하한** (주입·수집이 조용히 실패해 0건 초록이 되는 것을 막는다)
    static_scan       tools/audit/static_scan.py
    doc_numbers       tools/audit/doc_numbers.py
    check_ledger      tools/audit/check_ledger.py — 감사 대장이 있을 때만
    golden            tools/audit/golden_replay.py — 결정론 회귀(74초)
    config_defaults   tools/audit/config_defaults.py — 코드 기본값 ↔ defaults.yaml
    pyc_audit         tools/pyc_audit.py
    tools-import      tools/*.py 를 __main__ 이 아닌 이름으로 임포트 — 08-15 `judge()` 사건
    tools-help        argparse 를 쓰는 도구는 --help 까지 (인자 정의가 import 시점에 안 도는 도구 대비)
    collectors        python -m argus.collector.<name> 이 [OK] 로 끝나는지

`--help` 를 모든 도구에 치지 않는 이유: argparse 가 없는 도구는 인자를 무시하고 **본 동작을
한다**(감사 첫날 `make_icon.py --help` 가 아이콘을 다시 썼다). 그런 도구는 임포트만 한다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\run_gates.py
    .venv\\Scripts\\python.exe tools\\audit\\run_gates.py --skip pytest collectors
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable

# 2026-09-25 실측 644 passed → 감사 수정 판에서 669. 테스트를 지웠으면 이 값을 같이 내린다 — 조용히 줄면 안 된다.
MIN_TESTS = 730
MIN_TOOLS = 16
COLLECTORS = ("gpu", "network", "pdh", "process", "procsource", "proginfo", "system")


def _run(cmd: list[str], timeout: float = 900, extra_env: dict | None = None) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", **(extra_env or {}))
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def gate_pytest() -> tuple[bool, str]:
    # 가짜 APPDATA 로 돌려 테스트가 사용자 데이터 폴더에 쓰는지 본다. 2026-09-27 까지 격리가 없어
    # 주입한 크래시가 진짜 크래시 기록에 섞였고, 사용자 settings.yaml·machine_profile.json 을 읽었다.
    # ARGUS_DATA_DIR 을 비워야 격리가 conftest 에서 오는지를 잰다(밖에서 주면 conftest 가 없어도 통과).
    with tempfile.TemporaryDirectory(prefix="argus_gate_appdata_") as fake:
        env = {"APPDATA": fake, "ARGUS_DATA_DIR": ""}
        rc, out = _run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], extra_env=env)
        leaked = sorted(str(p.relative_to(fake)) for p in pathlib.Path(fake).rglob("*") if p.is_file())
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    failed = re.search(r"(\d+) failed", out)
    ok = rc == 0 and passed >= MIN_TESTS and not failed and not leaked
    return ok, (f"{passed} passed (하한 {MIN_TESTS}) rc={rc}" + (f" · {failed.group(0)}" if failed else "")
                + (f" · 사용자 데이터 폴더로 샘 {len(leaked)}: {', '.join(leaked[:3])}" if leaked else ""))


def _script_gate(rel: str, marker: str) -> tuple[bool, str]:
    rc, out = _run([PY, rel])
    return rc == 0 and marker in out, out.strip().splitlines()[-1] if out.strip() else "(출력 없음)"


def gate_tools_import() -> tuple[bool, str]:
    tools = sorted((ROOT / "tools").glob("*.py"))
    bad = []
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools"))
    for p in tools:
        spec = importlib.util.spec_from_file_location(f"_gate_{p.stem}", p)
        try:
            mod = importlib.util.module_from_spec(spec)
            # 등록하지 않으면 @dataclass 가 모듈을 못 찾아 AttributeError — 도구가 아니라 게이트 탓이다
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)  # __name__ != "__main__" → 본 동작 없음
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            bad.append(f"{p.name}: {type(e).__name__}: {e}")
    ok = not bad and len(tools) >= MIN_TOOLS
    return ok, f"{len(tools)}개 (하한 {MIN_TOOLS})" + ("" if not bad else " · " + " | ".join(bad))


# argparse 없이 둘 수 있는 도구 — **사유 필수.** 없으면 `--help` 가 무시되고 본 동작을 한다
# (감사 F-009: make_icon.py --help 가 아이콘을 다시 썼다).
NO_ARGPARSE_OK = {
    "soak_entry.py": "상주 진입점 — 인자를 argus 의 argparse 로 넘겨 --help 는 사용법만 찍는다",
}


def gate_tools_help() -> tuple[bool, str]:
    all_tools = [p for p in sorted((ROOT / "tools").glob("*.py")) if "__main__" in p.read_text(encoding="utf-8")]
    tools = [p for p in all_tools if "ArgumentParser" in p.read_text(encoding="utf-8")]
    bad = [f"{p.name} argparse 없음" for p in all_tools
           if p not in tools and p.name not in NO_ARGPARSE_OK]
    for p in tools:
        rc, out = _run([PY, str(p.relative_to(ROOT)), "--help"], timeout=120)
        if rc != 0 or "usage:" not in out:
            bad.append(f"{p.name} rc={rc}")
    return not bad, f"{len(tools)}개" + ("" if not bad else " · " + ", ".join(bad))


def gate_collectors() -> tuple[bool, str]:
    bad = []
    for name in COLLECTORS:
        rc, out = _run([PY, "-m", f"argus.collector.{name}"], timeout=180)
        last = [ln for ln in out.splitlines() if ln.startswith(("[OK]", "[FAIL]"))]
        if rc != 0 or not last or not last[-1].startswith("[OK]"):
            bad.append(name)
    return not bad, f"{len(COLLECTORS)}개" + ("" if not bad else " · FAIL " + ", ".join(bad))


# DB 를 쓰는 도구의 **안전한 호출**(미리보기·읽기 전용). 합성 DB 위에서 본체까지 돌린다.
DRYRUN_TOOLS = {
    "autolabel_backfill.py": [],                     # 기본이 미리보기(--apply 없이는 안 쓴다)
    "backfill_rollup.py": [],                        # 기본이 미리보기
    "rescore_incidents.py": ["--hours", "48"],       # 읽기 전용
    "grade_probe.py": [],                            # 읽기 전용
    "inject_progress.py": [],                        # 읽기 전용
    "readiness.py": [],                              # 읽기 전용
    "eval_snapshot.py": ["list"],
    "fault_injector.py": ["--dry-run", "cpu_spin", "--duration", "5"],   # 부하도 라벨도 없다
}
_SYNTH_DB = r'''
import time, json
from argus.storage.hot import Database
from argus.paths import db_path
t = time.time() - 3600
with Database(db_path()) as db:
    db.insert_many("metrics_raw", ("ts", "cpu_total", "mem_percent", "disk_resp_ms"),
                   [(t + i, 10.0 + i % 5, 40.0, 1.0) for i in range(600)])
    db.insert_many("process_metrics", ("ts", "pid", "name", "cpu_percent", "rss_mb", "handles"),
                   [(t + i, 10, "python", 5.0, 100.0 + i, 400) for i in range(0, 600, 5)])
    db.insert_many("anomaly_signals", ("ts", "detector", "score", "severity", "features"),
                   [(t + 300, "rules", 0.8, "warning", json.dumps({"rule": "CPU 과부하", "evidence": {"cpu_total": 90}}))])
    with db._lock:
        cur = db.conn.execute(
            "INSERT INTO incidents (ts_start, ts_end, severity, title, detectors, signal_count, peak_score, notified)"
            " VALUES (?, ?, 'warning', 'CPU 병목 — python 100%', '[\"rules\"]', 1, 0.8, 1)", (t + 300, t + 360))
        db.conn.execute("INSERT INTO incident_signals (incident_id, ts, detector, score) VALUES (?, ?, 'rules', 0.8)",
                        (cur.lastrowid, t + 300))
        db.conn.commit()
print("synth-ok")
'''


def gate_tools_dryrun() -> tuple[bool, str]:
    """DB 도구를 합성 DB 위에서 **본체까지** 돌린다 (감사 F-008).

    임포트·`--help` 게이트는 호출 시점의 시그니처 드리프트를 못 잡는다 — af73bf7 에서 `judge()` 에
    `observer` 가 필수가 되자 `autolabel_backfill` 이 실행 즉시 TypeError 였고 테스트는 전부 초록,
    하루 동안 자동 판정이 멈춘 것이 아무 데도 안 보였다. 합성 DB 에 **알림이 나간 닫힌 사건**을
    넣어 판정 경로를 실제로 탄다. 격리 데이터 폴더라 실제 DB 는 안 건드린다.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="gate_dryrun_") as d:
        env = dict(os.environ, ARGUS_DATA_DIR=d, ARGUS_NO_NOTIFY="1", PYTHONIOENCODING="utf-8")
        made = subprocess.run([PY, "-c", _SYNTH_DB], cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        if made.returncode != 0 or "synth-ok" not in made.stdout:
            return False, "합성 DB 를 못 만들었다: " + (made.stderr or made.stdout)[-200:]
        bad = []
        for tool, args in DRYRUN_TOOLS.items():
            if not (ROOT / "tools" / tool).exists():
                bad.append(f"{tool} 없음")
                continue
            r = subprocess.run([PY, str(ROOT / "tools" / tool), *args], cwd=ROOT, env=env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode not in (0, 1) or "Traceback" in out:
                bad.append(f"{tool} rc={r.returncode}")
    return not bad, f"{len(DRYRUN_TOOLS)}개" + ("" if not bad else " · FAIL " + ", ".join(bad))


def gate_sweep_anchors() -> tuple[bool, str]:
    """mutation_sweep 의 무력화 원문이 소스에 정확히 1회씩 있는가.

    코드를 옮기거나 들여쓰기만 바꿔도 앵커가 끊기고, 끊긴 변이는 그 규칙을 **아무도 안 재게**
    만든다(감사 F-002: 11개가 그 상태였다). 2026-09-25 수정 판에서 창 환경을 함수로 옮기다 또
    하나를 끊었다 — 그때 게이트에 이게 없었다.
    """
    spec = importlib.util.spec_from_file_location("_mutation_sweep", ROOT / "tools" / "mutation_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mutation_sweep"] = mod
    spec.loader.exec_module(mod)
    stale = mod.stale_anchors(mod.MUTANTS)
    return not stale, f"변이 {len(mod.MUTANTS)}개" + (
        "" if not stale else " · 끊김 " + ", ".join(k for k, _, _ in stale))


GATES = {
    "pytest": gate_pytest,
    "static_scan": lambda: _script_gate("tools/audit/static_scan.py", "[OK] static_scan"),
    "doc_numbers": lambda: _script_gate("tools/audit/doc_numbers.py", "[OK] doc_numbers"),
    "config_defaults": lambda: _script_gate("tools/audit/config_defaults.py", "[OK] config_defaults"),
    "check_ledger": lambda: _script_gate("tools/audit/check_ledger.py", "[OK] check_ledger")
        if (ROOT / "docs/audit/FINDINGS.md").exists() else None,
    # 74초 — pytest(57초)보다 길어 테스트 모음에 넣지 않고 게이트로만 둔다
    "golden": lambda: _script_gate("tools/audit/golden_replay.py", "[OK] golden_replay"),
    "pyc_audit": lambda: _script_gate("tools/pyc_audit.py", "[OK]"),
    "sweep_anchors": gate_sweep_anchors,
    "tools-dryrun": gate_tools_dryrun,
    "tools-import": gate_tools_import,
    "tools-help": gate_tools_help,
    "collectors": gate_collectors,
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="감사 게이트")
    ap.add_argument("--skip", nargs="*", default=[], choices=list(GATES))
    args = ap.parse_args()
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from bgprogress import Progress  # 전체는 3분 남짓 — 백그라운드로 던지면 상태줄에서 본다

    todo = [(n, fn) for n, fn in GATES.items() if n not in args.skip]
    with Progress("run_gates", "감사 게이트", len(todo), eta=False) as progress:
        return _run_gates(GATES, args.skip, progress)


def _run_gates(gates, skip, progress) -> int:
    results = []
    done = 0
    for name, fn in gates.items():
        if name in skip:
            print(f"[SKIP] {name}")
            continue
        progress.step(done, name)
        done += 1
        t = time.monotonic()
        try:
            res = fn()
            if res is None:  # 해당 없음 — 감사 대장이 없는 깨끗한 사본 등
                print(f"[SKIP] {name}  (해당 없음)")
                continue
            ok, detail = res
        except Exception as e:  # noqa: BLE001 - 게이트 하나가 죽어도 나머지는 돈다
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append(ok)
        print(f"{'[OK]  ' if ok else '[FAIL]'} {name:12s} {detail}  ({time.monotonic() - t:.0f}초)")
    ok = all(results) and bool(results)
    print("[OK] gates" if ok else "[FAIL] gates")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

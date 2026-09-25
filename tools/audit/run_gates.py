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
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable

# 2026-09-25 실측 644 passed → 감사 수정 판에서 669. 테스트를 지웠으면 이 값을 같이 내린다 — 조용히 줄면 안 된다.
MIN_TESTS = 685
MIN_TOOLS = 16
COLLECTORS = ("gpu", "network", "pdh", "process", "procsource", "proginfo", "system")


def _run(cmd: list[str], timeout: float = 900) -> tuple[int, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def gate_pytest() -> tuple[bool, str]:
    rc, out = _run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    failed = re.search(r"(\d+) failed", out)
    ok = rc == 0 and passed >= MIN_TESTS and not failed
    return ok, f"{passed} passed (하한 {MIN_TESTS}) rc={rc}" + (f" · {failed.group(0)}" if failed else "")


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


def gate_tools_help() -> tuple[bool, str]:
    tools = [p for p in sorted((ROOT / "tools").glob("*.py"))
             if "ArgumentParser" in p.read_text(encoding="utf-8")]
    bad = []
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
    results = []
    for name, fn in GATES.items():
        if name in args.skip:
            print(f"[SKIP] {name}")
            continue
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

"""감사 도구를 과거 사고로 채점한다 (2026-09-25 전면 감사).

지금까지의 확인(selftest·되돌리는 변이)은 전부 **내가 넣은 결함**이라 순환이다. 여기서는
실제로 있었던 결함의 **수정 직전 커밋**(C^)과 **수정 커밋**(C)을 각각 worktree 로 꺼내
현재 감사 도구를 돌리고, **C^ 에만 있고 C 에서 사라진 FAIL** 을 센다. 그게 있으면 도구가
그 사고를 잡았을 것이다.

둘 다에서 똑같이 FAIL 하는 것(옛 코드에 새 API 가 없어서 등)은 사고와 무관하므로 세지 않는다.
적중 여부의 최종 판단(그 FAIL 이 정말 그 사고인가)은 사람이 출력을 읽고 한다 — 도구는
후보만 낸다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\backtest.py                 # 표의 전부
    .venv\\Scripts\\python.exe tools\\audit\\backtest.py dffee7f 7b32e3e # 일부
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable

# (커밋, 사고 한 줄) — CLAUDE.md·PLAN·CHANGELOG 의 "실제로 당한 것"에서 골랐다.
CASES = [
    ("dffee7f", "자식 출력 디코딩을 로캘에 맡김 — stderr 가 조용히 None"),
    ("e2094f6", "load_gates 코드 기본값 ≠ defaults.yaml"),
    ("de7fbee", "msedgewebview2 코드 기본값 누락"),
    ("7b32e3e", "explorer 재시작이 알림을 영구히 죽임"),
    ("a8fd5fb", "지문 갱신 스레드가 몇 주 동안 조용히 죽음"),
    ("e5a547f", "트레이 '대시보드 열기'가 조용히 실패"),
    ("cd336bb", "웜 자식 프로세스에 sys.path 누락"),
    ("09384ad", "dist 를 그대로 복사하면 현장에서 막힘"),
    ("c17dbec", "웜 조회가 Parquet 을 통째로 메모리에"),
    ("822c1fa", "발화 불가능했던 GPU 고온 룰"),
    ("e53f00f", "로그 extra 가 LogRecord 예약 속성을 덮어씀"),
    ("09d8b3c", "채점기가 모르는 시나리오를 cpu 로 때움"),
    ("af73bf7", "judge() 시그니처 변경에 tools/autolabel_backfill 이 죽음"),
]

STEPS = [
    ("static_scan", ["tools/audit/static_scan.py"]),
    ("doc_numbers", ["tools/audit/doc_numbers.py"]),
    ("gates", ["tools/audit/run_gates.py", "--skip", "pytest", "collectors",
               "static_scan", "doc_numbers", "pyc_audit"]),
    ("config_fuzz", ["tools/audit/config_fuzz.py"]),
    ("config_defaults", ["tools/audit/config_defaults.py"]),
]


def _fails(tree: pathlib.Path) -> set[str]:
    env = dict(os.environ, PYTHONPATH=str(tree), PYTHONIOENCODING="utf-8")
    env.pop("ARGUS_DATA_DIR", None)
    out: set[str] = set()
    for name, args in STEPS:
        p = subprocess.run([PY, *args], cwd=tree, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=900)
        text = (p.stdout or "") + (p.stderr or "")
        lines = [ln.strip() for ln in text.splitlines()
                 if "[FAIL]" in ln or ln.strip().startswith(("crash", "silent"))
                 or "Traceback" in ln]
        # 줄 번호는 커밋마다 달라지므로 비교에서 뺀다
        out |= {f"{name}: " + re.sub(r":\d+\b", ":N", ln) for ln in lines}
        if p.returncode != 0 and not lines:
            out.add(f"{name}: rc={p.returncode} (출력에 FAIL 없음)")
    return out


def _checkout(commit: str, dest: pathlib.Path) -> None:
    subprocess.run(["git", "worktree", "add", "--detach", str(dest), commit], cwd=ROOT,
                   check=True, capture_output=True)
    shutil.copytree(ROOT / "tools" / "audit", dest / "tools" / "audit", dirs_exist_ok=True)
    golden = ROOT / "tests" / "golden"
    if golden.exists():
        shutil.copytree(golden, dest / "tests" / "golden", dirs_exist_ok=True)


def _remove(dest: pathlib.Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(dest)], cwd=ROOT,
                   capture_output=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    wanted = sys.argv[1:]
    cases = [c for c in CASES if not wanted or c[0] in wanted]
    base = pathlib.Path(tempfile.mkdtemp(prefix="argus_bt_"))
    caught = 0
    for commit, story in cases:
        before, after = base / f"{commit}_before", base / f"{commit}_after"
        try:
            _checkout(f"{commit}^", before)
            _checkout(commit, after)
            only_before = sorted(_fails(before) - _fails(after))
        finally:
            _remove(before)
            _remove(after)
        caught += bool(only_before)
        print(f"\n{'[후보]' if only_before else '[놓침]'} {commit} — {story}")
        for ln in only_before[:8]:
            print(f"    {ln[:160]}")
    shutil.rmtree(base, ignore_errors=True)
    print(f"\n후보 {caught}/{len(cases)} — 후보가 정말 그 사고인지는 위 줄을 읽고 판정한다")
    return 0


if __name__ == "__main__":
    sys.exit(main())

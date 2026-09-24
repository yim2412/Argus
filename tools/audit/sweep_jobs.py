"""mutation_sweep 을 적응형 풀(`~/.claude/tools/pool.js`)로 나눠 돌린다 (2026-09-25 감사).

`mutation_sweep.py` 는 한 번에 한 변이씩, 코어 하나로 돈다(160개 ≈ 2시간 반). 풀은 코어를
여러 개 쓰되 다른 프로그램이 CPU 를 쓰면 스스로 물러난다. 여기서는 그 풀이 먹을 작업 목록을
만들고, 끝난 뒤 결과를 모은다.

**작업 하나 = 변이 키 N개(`--group`, 기본 5).** `mutation_sweep --only` 는 실행마다 기준선
pytest 와 복원 확인 pytest 를 한 번씩 더 돈다 — 묶지 않으면 그 비용이 변이마다 붙는다.
크게 묶으면 풀이 작업을 끊을 때(게임을 켰을 때) 잃는 양이 커진다. 5개면 작업 하나가
pytest 7회(단독 약 7분)이고 추가 비용은 2/7 이다.

**작업마다 슬롯 폴더를 새로 푼다(`prepare`).** 풀은 작업을 중간에 죽인다. 변이 도중에
죽으면 그 사본의 소스는 **무력화된 채로 남는다** — 다시 풀지 않으면 다음 작업이 망가진
소스 위에서 돌고, 기준선부터 실패하거나(그나마 다행) 조용히 틀린 결과를 낸다.
원본은 `git archive HEAD` — 작업 트리의 커밋 안 된 변경은 들어가지 않는다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\sweep_jobs.py make --out <폴더> [--group 5] [--skip-done <sweep.log>]
    node %USERPROFILE%\\.claude\\tools\\pool.js --jobs <폴더>\\jobs.json --out <폴더>
    .venv\\Scripts\\python.exe tools\\audit\\sweep_jobs.py collect --out <폴더> [--also <sweep.log>]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_GROUP = 5

# 한 변이의 판정 줄. mutation_sweep 출력 형식:
#   [3/149] key — 설명
#         [잡힘] 1 failed, 643 passed …
RESULT_RE = re.compile(r"^\[(\d+)/(\d+)\] (\S+) — .*?\n\s+\[(잡힘|안 잡힘)\]", re.MULTILINE)
STOP_RE = re.compile(r"^\[중단\] (\S+?):", re.MULTILINE)


def _mutants():
    spec = importlib.util.spec_from_file_location("_ms", ROOT / "tools" / "mutation_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ms"] = mod
    spec.loader.exec_module(mod)
    return mod


def runnable_keys() -> tuple[list[str], list[str]]:
    """(돌릴 수 있는 키, 원문이 끊겨 못 돌리는 키). 끊긴 것을 넣으면 그 작업이 통째로 [중단] 된다."""
    ms = _mutants()
    ok, stale = [], []
    for m in ms.MUTANTS:
        live = all((ms.ROOT / rel).read_text(encoding="utf-8").count(orig) == 1
                   for rel, orig, _ in m.edits)
        (ok if live else stale).append(m.key)
    return ok, stale


def parse_results(text: str) -> dict[str, str]:
    return {m.group(3): m.group(4) for m in RESULT_RE.finditer(text)}


def _posix(p: pathlib.Path) -> str:
    return p.resolve().as_posix()


def make(out: pathlib.Path, group: int, skip_log: pathlib.Path | None) -> int:
    keys, stale = runnable_keys()
    done = parse_results(skip_log.read_text(encoding="utf-8")) if skip_log else {}
    todo = [k for k in keys if k not in done]
    py = _posix(ROOT / ".venv" / "Scripts" / "python.exe")
    repo = _posix(ROOT)
    jobs = []
    for i in range(0, len(todo), group):
        chunk = todo[i:i + group]
        jobs.append({
            "id": f"g{i // group:03d}",
            "keys": chunk,
            # 슬롯을 비우고 HEAD 를 새로 푼다 — 죽다 만 변이의 흔적을 지운다
            "prepare": f'rm -rf "{{slot}}/src" && mkdir -p "{{slot}}/src" && '
                       f'git -C "{repo}" archive HEAD | tar -x -C "{{slot}}/src"',
            "cmd": f'cd "{{slot}}/src" && PYTHONPATH="{{slot}}/src" "{py}" tools/mutation_sweep.py '
                   f'--only {" ".join(chunk)}',
        })
    out.mkdir(parents=True, exist_ok=True)
    spec = {"workdir": _posix(out / "slots"), "jobs": jobs}
    (out / "jobs.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"변이 {len(keys) + len(stale)}개 · 원문 끊김 {len(stale)} · 이미 끝남 {len(done)} · "
          f"남은 {len(todo)} → 작업 {len(jobs)}개 (묶음 {group})")
    print(f"  {out / 'jobs.json'}")
    return 0


def collect(out: pathlib.Path, also: list[pathlib.Path]) -> int:
    spec = json.loads((out / "jobs.json").read_text(encoding="utf-8"))
    results: dict[str, str] = {}
    for log in also:
        results.update(parse_results(log.read_text(encoding="utf-8")))
    missing_jobs, stopped = [], []
    # 다른 도구의 작업과 한 풀에 섞여 있을 수 있다 — `keys` 가 있는 것만 우리 것이다
    for job in [j for j in spec["jobs"] if "keys" in j]:
        log = out / "logs" / f"{job['id']}.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        got = parse_results(text)
        results.update(got)
        for m in STOP_RE.finditer(text):
            stopped.append((job["id"], m.group(1)))
        lost = [k for k in job["keys"] if k not in got]
        if lost:
            missing_jobs.append((job["id"], lost))
    keys, stale = runnable_keys()
    caught = sorted(k for k, v in results.items() if v == "잡힘")
    blind = sorted(k for k, v in results.items() if v == "안 잡힘")
    unmeasured = sorted(set(keys) - set(results))
    print(f"측정 {len(results)}/{len(keys)} · 잡힘 {len(caught)} · 안 잡힘 {len(blind)} · "
          f"못 잰 것 {len(unmeasured)} · 원문 끊김 {len(stale)}")
    for k in blind:
        print(f"  [안 잡힘] {k}")
    for jid, lost in missing_jobs:
        print(f"  [결과 없음] 작업 {jid}: {', '.join(lost)}")
    for jid, key in stopped:
        print(f"  [중단] 작업 {jid}: {key}")
    # "전부 잡힘"을 결과로 쓰기 전에 도구가 전부를 쟀는지 — 못 잰 것이 있으면 초록이 아니다
    ok = not unmeasured and not stopped
    print("[OK] 전수 측정" if ok else "[FAIL] 전수 측정이 아니다 — 위 목록을 먼저 본다")
    return 0 if ok else 1


def selftest() -> int:
    sample = (
        "[1/2] alpha — 무엇\n      [잡힘] 1 failed, 643 passed\n        - t::x\n\n"
        "[2/2] beta — 무엇\n      [안 잡힘] 644 passed\n\n"
        "[3/3] gamma — 무엇\n[중단] gamma: argus/x.py 에서 원문이 0회 발견됐다\n"
    )
    got = parse_results(sample)
    checks = [
        ("잡힘 파싱", got.get("alpha") == "잡힘"),
        ("안 잡힘 파싱", got.get("beta") == "안 잡힘"),
        ("중단은 결과가 아니다", "gamma" not in got),
        ("중단 탐지", [m.group(1) for m in STOP_RE.finditer(sample)] == ["gamma"]),
        ("돌릴 수 있는 키 하한", len(runnable_keys()[0]) >= 100),
    ]
    for name, ok in checks:
        print(f"  {'[OK]' if ok else '[FAIL]'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="mutation_sweep 을 풀 작업으로")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--out", required=True, type=pathlib.Path)
    m.add_argument("--group", type=int, default=DEFAULT_GROUP)
    m.add_argument("--skip-done", type=pathlib.Path)
    c = sub.add_parser("collect")
    c.add_argument("--out", required=True, type=pathlib.Path)
    c.add_argument("--also", type=pathlib.Path, nargs="*", default=[])
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "make":
        return make(a.out, a.group, a.skip_done)
    if a.cmd == "collect":
        return collect(a.out, a.also)
    return selftest()


if __name__ == "__main__":
    sys.exit(main())

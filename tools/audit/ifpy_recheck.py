"""if-py 변이에서 살아남은 줄을 **전체 테스트**로 다시 잰다 (2026-09-25 감사).

`.mutate.json` 은 속도 때문에 핵심 테스트 14개 파일만 돈다. 거기서 살아남은 변이도 다른
테스트가 잡을 수 있으므로 그 수는 **상한**이다. 여기서는 살아남은 줄마다 같은 뒤집기를
적용하고 전체 pytest 를 돌려 진짜 사각을 가린다. 적응형 풀(`~/.claude/tools/pool.js`)로 나눠 돈다.

**판정은 종료 코드가 아니라 로그의 `VERDICT:` 줄이다.** 뒤집기 적용이 실패해도 종료 코드는
0 이 아니므로, 종료 코드로 세면 "적용 실패"가 "잡힘"으로 둔갑한다 — 도구가 관대해지는 방향.

    VERDICT: caught     전체 테스트가 실패 — 그 줄을 지키는 단언이 있다
    VERDICT: survived   전체 테스트가 통과 — 진짜 사각
    VERDICT: timeout    테스트가 제한 시간을 넘김 — 잡힌 것으로 본다(mutate.js 와 같은 규약)
    VERDICT: apply-failed  줄이 기대와 달라 뒤집지 못함 — 결과 아님

사용:
    python tools/audit/ifpy_recheck.py make --survivors <mut.log> --out <폴더>
    node ~/.claude/tools/pool.js --jobs <폴더>/jobs.json --out <폴더>
    python tools/audit/ifpy_recheck.py collect --out <폴더>
    python tools/audit/ifpy_recheck.py selftest
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SURVIVOR_RE = re.compile(r"^\s+(argus[\\/][\w\\/]+\.py):(\d+)\s{2}(.*)$")
IF_RE = re.compile(r"^(\s*)if (not )?(.+):\s*$")   # mutate.js 의 if-py 프리셋과 같은 규칙
VERDICT_RE = re.compile(r"^VERDICT: (\S+)", re.MULTILINE)
PYTEST_TIMEOUT_S = 600


def flip(line: str) -> str | None:
    m = IF_RE.match(line.rstrip("\r\n"))
    if not m:
        return None
    indent, neg, cond = m.groups()
    return f"{indent}if {cond}:" if neg else f"{indent}if not ({cond}):"


def apply(root: pathlib.Path, rel: str, lineno: int, expect: str) -> int:
    path = root / rel
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if lineno > len(lines):
        print("VERDICT: apply-failed (줄 번호가 파일보다 길다)")
        return 3
    cur = lines[lineno - 1]
    # mutate.js 는 줄을 잘라 찍는다 — 앞부분이 같은지만 본다
    if not cur.strip().startswith(expect.strip()[:60]):
        print(f"VERDICT: apply-failed (줄이 다르다: {cur.strip()[:80]!r})")
        return 3
    new = flip(cur)
    if new is None:
        print("VERDICT: apply-failed (if 줄이 아니다)")
        return 3
    lines[lineno - 1] = new + ("\n" if cur.endswith("\n") else "")
    path.write_text("".join(lines), encoding="utf-8")
    print(f"적용: {rel}:{lineno}  {new.strip()[:100]}")
    return 0


def parse_survivors(text: str) -> list[tuple[str, int, str]]:
    block = text.split("초록인 줄", 1)[-1] if "초록인 줄" in text else ""
    out = []
    for line in block.splitlines():
        m = SURVIVOR_RE.match(line)
        if m:
            out.append((m.group(1).replace("\\", "/"), int(m.group(2)), m.group(3)))
    return out


def _posix(p: pathlib.Path) -> str:
    return p.resolve().as_posix()


def make(survivors: pathlib.Path, out: pathlib.Path) -> int:
    rows = parse_survivors(survivors.read_text(encoding="utf-8", errors="replace"))
    if not rows:
        print("[FAIL] 살아남은 줄을 하나도 못 읽었다 — 로그 형식이 바뀌었나?")
        return 1
    py = _posix(ROOT / ".venv" / "Scripts" / "python.exe")
    repo = _posix(ROOT)
    me = _posix(pathlib.Path(__file__))
    jobs = []
    for i, (rel, ln, text) in enumerate(rows):
        expect = text.replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
        jobs.append({
            "id": f"m{i:03d}",
            "where": f"{rel}:{ln}",
            "prepare": f'rm -rf "{{slot}}/src" && mkdir -p "{{slot}}/src" && '
                       f'git -C "{repo}" archive HEAD | tar -x -C "{{slot}}/src"',
            "cmd": (
                f'cd "{{slot}}/src" && "{py}" "{me}" apply --root . --file "{rel}" --line {ln} '
                f'--expect "{expect}" || exit 0; '
                f'PYTHONPATH="{{slot}}/src" timeout {PYTEST_TIMEOUT_S} "{py}" -m pytest -q -x '
                f'-p no:cacheprovider > pytest.out 2>&1; rc=$?; tail -3 pytest.out; '
                f'if [ $rc -eq 0 ]; then echo "VERDICT: survived"; '
                f'elif [ $rc -eq 124 ]; then echo "VERDICT: timeout"; '
                f'else echo "VERDICT: caught"; fi'
            ),
        })
    # **대조 작업 — 아무것도 뒤집지 않는다.** 슬롯 환경이 깨져 있으면(경로·의존성) 모든 변이가
    # "caught" 로 나오고, 그건 도구가 관대해지는 방향이라 아무 경고도 없다. 대조가 survived 가
    # 아니면 collect 가 전체를 무효로 본다. 맨 앞에 둬서 가장 먼저 결과가 나오게 한다.
    control = dict(jobs[0], id="ctrl", where="(대조: 뒤집지 않음)")
    control["cmd"] = 'cd "{slot}/src" && ' + control["cmd"].split(" || exit 0; ", 1)[1]
    jobs.insert(0, control)
    out.mkdir(parents=True, exist_ok=True)
    (out / "jobs.json").write_text(
        json.dumps({"workdir": _posix(out / "slots"), "jobs": jobs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"살아남은 줄 {len(rows)}개 → 작업 {len(jobs)}개 · {out / 'jobs.json'}")
    return 0


def collect(out: pathlib.Path) -> int:
    spec = json.loads((out / "jobs.json").read_text(encoding="utf-8"))
    verdicts: dict[str, list[str]] = {}
    missing = []
    # 다른 도구의 작업과 한 풀에 섞여 있을 수 있다 — `where` 가 있는 것만 우리 것이다
    mine = [j for j in spec["jobs"] if "where" in j]
    for job in mine:
        log = out / "logs" / f"{job['id']}.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        v = VERDICT_RE.findall(text)
        if not v:
            missing.append(job["where"])
            continue
        verdicts.setdefault(v[-1], []).append(job["where"])
    ctrl = next((k for k, v in verdicts.items() if "(대조: 뒤집지 않음)" in v), None)
    for v in verdicts.values():
        if "(대조: 뒤집지 않음)" in v:
            v.remove("(대조: 뒤집지 않음)")
    total = len(mine) - 1
    print(f"대조: {ctrl or '판정 없음'} (survived 여야 한다)")
    print(f"변이 {total} · " + " · ".join(f"{k} {len(v)}" for k, v in sorted(verdicts.items()))
          + f" · 판정 없음 {len(missing)}")
    if ctrl != "survived":
        print("[FAIL] 대조가 survived 가 아니다 — 슬롯 환경이 깨졌다. 아래 결과는 무효")
        return 1
    for where in sorted(verdicts.get("survived", [])):
        print(f"  [survived] {where}")
    for where in sorted(verdicts.get("apply-failed", [])):
        print(f"  [apply-failed] {where}")
    for where in missing:
        print(f"  [판정 없음] {where}")
    ok = not missing and "apply-failed" not in verdicts
    print("[OK] 전수 재확인" if ok else "[FAIL] 전수가 아니다 — 판정 없음·적용 실패를 먼저 본다")
    return 0 if ok else 1


def selftest() -> int:
    checks = []
    checks.append(("부정 추가", flip("    if x > 1:") == "    if not (x > 1):"))
    checks.append(("부정 제거", flip("  if not ready:") == "  if ready:"))
    checks.append(("if 아님", flip("    return x") is None))
    sample = ("=== 망가뜨려도 테스트가 전부 초록인 줄 ===\n"
              "  argus\\detection\\base.py:191  if self._first_ts is None:\n"
              "  argus\\decide\\fusion.py:12  if a:\n\n이 줄들을 지키는 단언이 하나도 없다.\n")
    got = parse_survivors(sample)
    checks.append(("로그 파싱", got == [("argus/detection/base.py", 191, "if self._first_ts is None:"),
                                       ("argus/decide/fusion.py", 12, "if a:")]))
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        (root / "argus").mkdir()
        (root / "argus" / "m.py").write_text("def f(x):\n    if x > 1:\n        return 1\n", encoding="utf-8")
        checks.append(("적용 성공", apply(root, "argus/m.py", 2, "if x > 1:") == 0
                       and "if not (x > 1):" in (root / "argus" / "m.py").read_text(encoding="utf-8")))
        checks.append(("줄 불일치 거부", apply(root, "argus/m.py", 3, "if x > 1:") == 3))
    for name, ok in checks:
        print(f"  {'[OK]' if ok else '[FAIL]'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="if-py 생존 변이 전체 테스트 재확인")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--survivors", required=True, type=pathlib.Path)
    m.add_argument("--out", required=True, type=pathlib.Path)
    a_ = sub.add_parser("apply")
    a_.add_argument("--root", required=True, type=pathlib.Path)
    a_.add_argument("--file", required=True)
    a_.add_argument("--line", required=True, type=int)
    a_.add_argument("--expect", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--out", required=True, type=pathlib.Path)
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "make":
        return make(a.survivors, a.out)
    if a.cmd == "apply":
        return apply(a.root, a.file, a.line, a.expect)
    if a.cmd == "collect":
        return collect(a.out)
    return selftest()


if __name__ == "__main__":
    sys.exit(main())

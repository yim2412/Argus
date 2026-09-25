r"""발견 대장·COVERAGE 규약 검사 (2026-09-25 전면 감사).

문서로 적은 규약은 지켜지지 않는다 — 도구로 강제한다(국방 check_protocol 의 형태만 가져왔다).

    필수 필드       위치·요약·근거·재현·반증조건·이력·심각도·수정비용·대상
    심각도 상한     근거가 `실측` 이 아니면 `높음`·`치명` 금지 (재현 없는 발견의 인플레 방지)
    프로브 실재     재현이 `docs/audit/probes/*.py` 를 가리키면 그 파일이 있어야 한다
    앵커 실재       위치의 `path:line — \`앵커\`` 에서 앵커 문자열이 그 파일에 아직 있어야 한다
                    (없으면 코드가 바뀌었다 — 이미 고쳐졌는지 다시 볼 것)
    COVERAGE        추적 중인 argus/*.py 가 전부 COVERAGE.md 에 있고, 없는 파일을 적지 않았다

사용:
    .venv\\Scripts\\python.exe tools\\audit\\check_ledger.py
    .venv\\Scripts\\python.exe tools\\audit\\check_ledger.py --selftest
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
AUDIT = ROOT / "docs" / "audit"
FIELDS = ("위치", "요약", "근거", "재현", "반증조건", "이력", "심각도", "수정비용", "대상")
HEAD_RE = re.compile(r"^### (F-\d{3}) · 영역: (.+?) · 상태: (\S+(?: \S+)?)\s*$")
STATES = {"미처리", "수정중", "수정됨", "기각", "판단 필요"}
UNRESOLVED = {"미처리", "수정중", "판단 필요"}
MIN_FINDINGS = 1


def parse(text: str) -> list[dict]:
    items, cur = [], None
    for line in text.splitlines():
        m = HEAD_RE.match(line)
        if m:
            cur = {"id": m.group(1), "area": m.group(2), "state": m.group(3), "fields": {}}
            items.append(cur)
        elif cur and line.startswith("- ") and ":" in line:
            k, _, v = line[2:].partition(":")
            cur["fields"][k.strip()] = v.strip()
        elif line.startswith("### "):
            items.append({"id": line, "bad_head": True, "fields": {}})
            cur = None
    return items


def check(root: pathlib.Path) -> list[str]:
    errs: list[str] = []
    text = (root / "docs/audit/FINDINGS.md").read_text(encoding="utf-8")
    items = parse(text)
    if len(items) < MIN_FINDINGS:
        errs.append("대장 항목 0건 — 형식이 바뀌어 파서가 아무것도 못 읽었다")
    seen = set()
    check.anchored = 0  # 앵커를 실제로 대조한 항목 수 — 형식이 안 맞아 건너뛴 것이 조용히 초록이 되지 않게
    for it in items:
        if it.get("bad_head"):
            errs.append(f"머리줄 형식 위반: {it['id']}")
            continue
        fid, f = it["id"], it["fields"]
        if fid in seen:
            errs.append(f"{fid}: 번호 중복")
        seen.add(fid)
        if it["state"] not in STATES:
            errs.append(f"{fid}: 알 수 없는 상태 '{it['state']}'")
        for k in FIELDS:
            if not f.get(k):
                errs.append(f"{fid}: 필드 없음 — {k}")
        sev, grade = f.get("심각도", ""), f.get("근거", "")
        if re.match(r"(높음|치명)", sev) and not grade.startswith("실측"):
            errs.append(f"{fid}: 근거가 실측이 아닌데 심각도 {sev.split()[0]}")
        for probe in re.findall(r"docs/audit/probes/[\w./-]+\.py", f.get("재현", "")):
            if not (root / probe).exists():
                errs.append(f"{fid}: 재현 프로브가 없다 — {probe}")
        m = re.match(r"`([^`]+?\.(?:py|yaml|spec|md|ps1))(?::\d+)?`\s*(?:,[^—]*)?—\s*`([^`]+)`",
                     f.get("위치", ""))
        # 앵커 실재는 **아직 안 고친 항목**에만 요구한다. 고친 항목은 그 앵커(옛 코드)가 사라지는 게
        # 정상이다 — 처음엔 상태를 안 봐서 F-012 를 고치자마자 FAIL 이 났다(2026-09-25).
        if m and it["state"] in UNRESOLVED:
            check.anchored += 1
            path, anchor = root / m.group(1), m.group(2)
            if not path.exists():
                errs.append(f"{fid}: 위치 파일이 없다 — {m.group(1)}")
            elif anchor not in path.read_text(encoding="utf-8"):
                errs.append(f"{fid}: 앵커가 사라졌다(코드 변경?) — {m.group(1)}: {anchor[:50]}")

    cov_path = root / "docs/audit/COVERAGE.md"
    if not cov_path.exists():
        errs.append("COVERAGE.md 없음")
    else:
        cov = set(re.findall(r"`(argus/[\w/]+\.py)`", cov_path.read_text(encoding="utf-8")))
        tracked = set(subprocess.run(
            ["git", "ls-files", "argus/*.py", "argus/**/*.py"], cwd=root, capture_output=True,
            text=True, encoding="utf-8").stdout.split())
        if tracked:
            for p in sorted(tracked - cov):
                errs.append(f"COVERAGE 누락: {p}")
            for p in sorted(cov - tracked):
                errs.append(f"COVERAGE 유령(추적 파일 아님): {p}")
        else:
            errs.append("git ls-files 가 비었다 — 대조 불가")
    return errs


def run(root: pathlib.Path) -> int:
    errs = check(root)
    items = parse((root / "docs/audit/FINDINGS.md").read_text(encoding="utf-8"))
    print(f"대장 {len(items)}건 · 앵커 대조 {check.anchored}건 · 위반 {len(errs)}")
    for e in errs:
        print(f"  [FAIL] {e}")
    print("[OK] check_ledger" if not errs else "[FAIL] check_ledger")
    return 0 if not errs else 1


def selftest() -> int:
    import shutil

    base = (ROOT / "docs/audit/FINDINGS.md").read_text(encoding="utf-8")
    # 앵커 주입 대상은 **지금 대장에서** 고른다. 특정 항목을 박아 두면 그 항목을 고치는 순간
    # 주입이 빈 칸을 쏘아 selftest 가 거짓으로 빨개지거나(좋은 쪽) 조용히 무의미해진다.
    anchors = {st: [] for st in STATES}
    for it in parse(base):
        am = re.match(r"`[^`]+`\s*(?:,[^—]*)?—\s*`([^`]+)`", it["fields"].get("위치", ""))
        if am:
            anchors[it["state"]].append(am.group(1))
    open_anchor = next((a for st in UNRESOLVED for a in anchors[st]), None)
    done_anchor = next(iter(anchors["수정됨"]), None)
    cases = {
        "필드 삭제": base.replace("- 반증조건:", "- 반증없음:", 1),
        "심각도 인플레": base.replace("- 근거: 실측", "- 근거: 추정", 1),
        "없는 프로브": base.replace("p001_user_rules_ignored.py", "p999_none.py", 1),
        "사라진 앵커(미해결 항목)": base.replace(f"`{open_anchor}`", "`없는앵커 ZZZ`", 1) if open_anchor else base,
        "머리줄 파손": base.replace("### F-002 · 영역:", "### F-002 영역:", 1),
    }
    results = []
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        # 추적 파일 대조는 실제 저장소를 써야 하므로 git 작업 트리 위에 대장만 바꿔 끼운다
        for name, text in cases.items():
            if text == base:
                results.append((name, False, "주입 실패(치환 대상 없음)"))
                continue
            (tmp / "docs/audit").mkdir(parents=True, exist_ok=True)
            (tmp / "docs/audit/FINDINGS.md").write_text(text, encoding="utf-8")
            shutil.copy(ROOT / "docs/audit/COVERAGE.md", tmp / "docs/audit/COVERAGE.md")
            for sub in ("docs/audit/probes", "argus", "tools", "packaging"):
                if not (tmp / sub).exists():
                    shutil.copytree(ROOT / sub, tmp / sub,
                                    ignore=shutil.ignore_patterns("__pycache__"))
            for f in ("CLAUDE.md", "README.md"):
                shutil.copy(ROOT / f, tmp / f)
            errs = [e for e in _check_without_git(tmp)]
            results.append((name, bool(errs), errs[0] if errs else "위반을 못 봤다"))
        # 음성 대조 — 고친 항목의 앵커는 사라져도 된다(그게 고친 결과다)
        if done_anchor:
            (tmp / "docs/audit/FINDINGS.md").write_text(
                base.replace(f"`{done_anchor}`", "`없는앵커 ZZZ`", 1), encoding="utf-8")
            errs = [e for e in _check_without_git(tmp) if "앵커가 사라졌다" in e]
            results.append(("고친 항목의 사라진 앵커는 통과", not errs, errs[0] if errs else "통과"))
    print("== selftest ==")
    for name, ok, why in results:
        print(f"  {'[OK]' if ok else '[FAIL]'} {name} — {why}")
    return 0 if all(ok for _, ok, _ in results) else 1


def _check_without_git(root: pathlib.Path) -> list[str]:
    return [e for e in check(root) if not e.startswith(("COVERAGE", "git ls-files"))]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="발견 대장 규약 검사")
    ap.add_argument("--selftest", action="store_true")
    return selftest() if ap.parse_args().selftest else run(ROOT)


if __name__ == "__main__":
    sys.exit(main())

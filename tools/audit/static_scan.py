"""이 프로젝트에서 실제로 반복된 실수를 AST 로 찾는다 (2026-09-25 전면 감사).

grep 이 아니라 AST 인 이유: `subprocess.run(` 과 `encoding=` 이 다른 줄에 있으면 줄 단위
검사는 전부 오탐이거나 전부 미탐이 된다. 감사 첫날 grep 으로 센 "인코딩 누락 23건"은
대부분 `Database.open()` 같은 메서드였다.

검사 (각 검사는 실제로 당한 사건에서 나왔다):
    enc-open        내장 open / Path.read_text·write_text 에 encoding 없음 (전역 인코딩 1)
    enc-subprocess  subprocess 에 text=True 인데 encoding 없음 — 실패가 조용하다 (dffee7f)
    enc-filehandler logging.FileHandler 계열에 encoding 없음
    global-import   `global X` 로 재할당되는 전역을 `from m import X` (전역 8번)
    net-import      네트워크 모듈 import — 외부 전송 0 (설계 규칙 5)
    unsafe-eval     eval/exec 호출, yaml.load(SafeLoader 없이) (탐지 규칙 5)
    audit-privacy   감사 산출물에 사용자 경로가 섞임

허용 목록은 `scan_allow.txt` — 한 줄에 `검사|경로|앵커 문자열|사유: …`. 사유가 없으면 FAIL.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\static_scan.py
    .venv\\Scripts\\python.exe tools\\audit\\static_scan.py --selftest
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALLOW_FILE = pathlib.Path(__file__).with_name("scan_allow.txt")

SCAN_DIRS = ("argus", "tools", "packaging")
# 스캔 대상이 이보다 적으면 목록 주입이 끊긴 것이다 — 0건 초록을 막는다.
# 2026-09-25 실측: 109개
MIN_FILES = 100

NET_MODULES = {"socket", "urllib", "urllib.request", "http", "http.client", "requests",
               "httpx", "aiohttp", "ftplib", "smtplib", "websocket", "websockets"}
FILEHANDLERS = {"FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler"}
# 사용자명이 박힌 경로. 감사 산출물(대장·프로브 출력)에 들어가면 안 된다.
PRIVACY_RE = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!<)[^\\/\s`'\"]+", re.IGNORECASE)
AUDIT_DIR = pathlib.Path("docs") / "audit"


@dataclass(frozen=True)
class Hit:
    check: str
    path: str  # ROOT 기준 posix
    line: int
    text: str  # 그 줄 원문 (앵커 대조용)

    def fmt(self) -> str:
        return f"{self.check:16s} {self.path}:{self.line}  {self.text.strip()[:90]}"


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _mode_is_binary(call: ast.Call, pos: int) -> bool:
    mode = _kw(call, "mode")
    node = mode.value if mode else (call.args[pos] if len(call.args) > pos else None)
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and "b" in node.value


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _reassigned_globals(trees: dict[str, ast.Module]) -> dict[str, set[str]]:
    """모듈 경로(점 표기) → 함수 안에서 `global` 로 재할당되는 이름."""
    out: dict[str, set[str]] = {}
    for rel, tree in trees.items():
        mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                out.setdefault(mod, set()).update(node.names)
    return out


def scan_tree(rel: str, tree: ast.Module, lines: list[str],
              reassigned: dict[str, set[str]]) -> list[Hit]:
    hits: list[Hit] = []

    def hit(check: str, node: ast.AST) -> None:
        ln = getattr(node, "lineno", 1)
        hits.append(Hit(check, rel, ln, lines[ln - 1] if ln <= len(lines) else ""))

    pkg = rel[:-3].replace("/", ".").rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            short = name.rsplit(".", 1)[-1]
            has_enc = _kw(node, "encoding") is not None
            if name in ("open", "io.open") and not has_enc and not _mode_is_binary(node, 1):
                hit("enc-open", node)
            elif short in ("read_text", "write_text") and isinstance(node.func, ast.Attribute) \
                    and not has_enc:
                hit("enc-open", node)
            elif short == "open" and isinstance(node.func, ast.Attribute) and not has_enc \
                    and (node.args or _kw(node, "mode")) and not _mode_is_binary(node, 0):
                # Path.open("w") 류. 인자 없는 `.open()` 은 이 앱의 Database/PDH 메서드다.
                hit("enc-open", node)
            elif name.startswith("subprocess.") and short in ("run", "Popen", "check_output", "call"):
                texty = _is_true((_kw(node, "text") or _kw(node, "universal_newlines") or
                                  ast.keyword(value=ast.Constant(False))).value)
                if texty and not has_enc:
                    hit("enc-subprocess", node)
            elif short in FILEHANDLERS and not has_enc:
                hit("enc-filehandler", node)
            elif name in ("eval", "exec"):
                hit("unsafe-eval", node)
            elif name in ("yaml.load", "yaml.unsafe_load", "yaml.full_load"):
                loader = _kw(node, "Loader")
                if name != "yaml.load" or not loader or "Safe" not in _dotted(loader.value):
                    hit("unsafe-eval", node)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in NET_MODULES or a.name.split(".")[0] in NET_MODULES:
                    hit("net-import", node)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # 상대 import → 절대 모듈명
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join(base + ([mod] if mod else []))
            if mod in NET_MODULES or mod.split(".")[0] in NET_MODULES:
                hit("net-import", node)
            names = reassigned.get(mod, set())
            if any(a.name in names for a in node.names):
                hit("global-import", node)
    return hits


def scan_privacy(root: pathlib.Path) -> list[Hit]:
    hits: list[Hit] = []
    base = root / AUDIT_DIR
    if not base.exists():
        return hits
    for p in sorted(base.rglob("*")):
        if not p.is_file() or p.suffix not in (".md", ".txt", ".py", ".json"):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if PRIVACY_RE.search(line):
                hits.append(Hit("audit-privacy", p.relative_to(root).as_posix(), i, line))
    return hits


@dataclass(frozen=True)
class Allow:
    check: str
    path: str
    anchor: str
    reason: str


def load_allow(path: pathlib.Path) -> tuple[list[Allow], list[str]]:
    allows, errors = [], []
    if not path.exists():
        return allows, errors
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [s.strip() for s in line.split("|", 3)]
        if len(parts) != 4 or not parts[3].startswith("사유:") or len(parts[3]) <= len("사유:") + 2:
            errors.append(f"scan_allow.txt:{i} 형식은 `검사|경로|앵커|사유: …` — 사유 필수: {line}")
            continue
        allows.append(Allow(*parts))
    return allows, errors


def run(root: pathlib.Path, allow_path: pathlib.Path, min_files: int = MIN_FILES) -> int:
    files = [p for d in SCAN_DIRS for p in sorted((root / d).rglob("*.py"))
             if "__pycache__" not in p.parts]
    trees: dict[str, ast.Module] = {}
    sources: dict[str, list[str]] = {}
    errors: list[str] = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        text = p.read_text(encoding="utf-8-sig")
        try:
            trees[rel] = ast.parse(text, filename=rel)
        except SyntaxError as e:
            errors.append(f"구문 오류로 스캔 불가: {rel}: {e}")
            continue
        sources[rel] = text.splitlines()

    reassigned = _reassigned_globals(trees)
    hits = [h for rel, t in trees.items() for h in scan_tree(rel, t, sources[rel], reassigned)]
    hits += scan_privacy(root)

    allows, allow_errors = load_allow(allow_path)
    errors += allow_errors
    used: set[Allow] = set()
    remaining = []
    for h in hits:
        match = next((a for a in allows if a.check == h.check and a.path == h.path
                      and a.anchor in h.text), None)
        if match:
            used.add(match)
        else:
            remaining.append(h)
    stale = [a for a in allows if a not in used]

    print(f"스캔 파일 {len(files)}개 (하한 {min_files}) · 허용 목록 {len(allows)}건 "
          f"(사용 {len(used)}) · 위반 {len(remaining)}건")
    for h in remaining:
        print(f"  [FAIL] {h.fmt()}")
    for a in stale:
        errors.append(f"쓰이지 않는 허용 항목(코드가 바뀌었나?): {a.check}|{a.path}|{a.anchor}")
    if len(files) < min_files:
        errors.append(f"스캔 대상 {len(files)}개 < 하한 {min_files} — 목록이 끊겼다")
    for e in errors:
        print(f"  [FAIL] {e}")
    ok = not remaining and not errors
    print("[OK] static_scan" if ok else "[FAIL] static_scan")
    return 0 if ok else 1


# --------------------------------------------------------------------------- selftest
#
# 검사마다 위반 한 건을 임시 사본에 넣고 FAIL 이 나는지 본다. 빨개지지 않으면 그 검사는
# 존재하지 않는 것이다. 먼저 깨끗한 사본이 [OK] 인지 보는 것도 같은 이유다 — 원래 FAIL 이면
# 주입이 FAIL 을 "만든" 게 아니다.

INJECTIONS: dict[str, tuple[str, str]] = {
    "enc-open": ("argus/_audit_inj.py", "open('x.txt', 'w')\n"),
    "enc-open-path": ("argus/_audit_inj.py", "import pathlib\npathlib.Path('x').read_text()\n"),
    "enc-subprocess": ("argus/_audit_inj.py",
                       "import subprocess\nsubprocess.run(['x'],\n    capture_output=True,\n    text=True)\n"),
    "enc-filehandler": ("argus/_audit_inj.py", "import logging\nlogging.FileHandler('x.log')\n"),
    "global-import": ("argus/_audit_inj.py", "from argus.detection.registry import _loaded\n"),
    "global-import-rel": ("argus/detection/_audit_inj.py", "from .registry import _loaded\n"),
    "net-import": ("argus/_audit_inj.py", "import urllib.request\n"),
    "unsafe-eval": ("argus/_audit_inj.py", "eval('1')\n"),
    "unsafe-yaml": ("argus/_audit_inj.py", "import yaml\nyaml.load('a: 1')\n"),
    "audit-privacy": ("docs/audit/_inj.md", "C:\\Users\\someone\\AppData\\x\n"),
}


def selftest() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="scan_selftest_"))
    try:
        for d in SCAN_DIRS:
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns("__pycache__"))
        if (ROOT / AUDIT_DIR).exists():
            shutil.copytree(ROOT / AUDIT_DIR, tmp / AUDIT_DIR)
        results = []
        base = run(tmp, ALLOW_FILE)
        results.append(("깨끗한 사본", base == 0))
        for key, (rel, code) in INJECTIONS.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            rc = run(tmp, ALLOW_FILE)
            target.unlink()
            results.append((key, rc == 1))
        # 사유 없는 허용 항목은 FAIL
        bad = tmp / "allow_bad.txt"
        bad.write_text("enc-open|argus/x.py|open(\n", encoding="utf-8")
        results.append(("allow-사유없음", run(tmp, bad) == 1))
        # 스캔 대상 하한
        results.append(("파일수-하한", run(tmp, ALLOW_FILE, min_files=10_000) == 1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n== selftest ==")
    for key, ok in results:
        print(f"  {'[OK]' if ok else '[FAIL]'} {key}")
    return 0 if all(ok for _, ok in results) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="감사 정적 스캔")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else run(ROOT, ALLOW_FILE)


if __name__ == "__main__":
    sys.exit(main())

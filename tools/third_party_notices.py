"""배포물에 싣는 제3자 고지(THIRD_PARTY_NOTICES.txt)를 만든다 (감사 F-011).

배포물(`dist/argus`, `dist/argus-ui`)에 자체 LICENSE 도 제3자 고지도 없었다. PyInstaller 가
dist-info 몇 개의 라이선스만 우연히 실었다. **PySide6/Qt 는 LGPL-3** 이라 고지와 교체 가능성
안내가 필요하고(onedir 라 교체는 가능하다), pyarrow(Apache-2.0)는 NOTICE 를 요구한다.

설치된 패키지 정보(importlib.metadata)에서 런타임 의존성과 그 하위 의존성을 따라가 이름·버전·
라이선스와 동봉 라이선스 원문을 모은다. 목록을 손으로 두면 의존성이 바뀔 때 어긋난다.

    .venv\\Scripts\\python.exe tools\\third_party_notices.py            # packaging\\THIRD_PARTY_NOTICES.txt
    .venv\\Scripts\\python.exe tools\\third_party_notices.py --check    # 쓰지 않고 누락만 본다
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
from importlib import metadata

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"
# 런타임 뿌리 — pyproject 의 dependencies + ui extras. 개발 전용(pytest·pyinstaller)은 싣지 않는다.
ROOTS = ("psutil", "pywin32", "PyYAML", "pydantic", "nvidia-ml-py", "pyarrow", "duckdb", "PySide6", "pyqtgraph")
LGPL_NOTE = (
    "PySide6 / Qt 는 LGPL-3.0 으로 배포된다. Argus 는 PyInstaller --onedir 로 묶여 Qt·PySide6 라이브러리가\n"
    "별도 파일(_internal\\PySide6\\…)로 들어 있으므로, 사용자는 같은 버전 계열의 라이브러리로 교체할 수 있다.\n"
    "소스: https://code.qt.io · https://code.qt.io/cgit/pyside/pyside-setup.git"
)


def _name(req: str) -> str | None:
    if "extra ==" in req:          # 선택 의존성은 따라가지 않는다
        return None
    m = re.match(r"\s*([A-Za-z0-9_.\-]+)", req)
    return m.group(1) if m else None


def collect() -> dict[str, metadata.Distribution]:
    seen: dict[str, metadata.Distribution] = {}
    todo = list(ROOTS)
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        seen[key] = dist
        for req in dist.requires or []:
            dep = _name(req)
            if dep:
                todo.append(dep)
    return dict(sorted(seen.items()))


def _license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    lic = (meta.get("License") or "").strip()
    if classifiers:
        return ", ".join(classifiers)
    return lic.splitlines()[0][:120] if lic else "(명시 없음)"


def _license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    out = []
    for f in dist.files or []:
        name = pathlib.PurePath(str(f)).name.upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")) and "dist-info" in str(f).lower():
            try:
                out.append((str(f), pathlib.Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return out


def render(dists: dict[str, metadata.Distribution]) -> tuple[str, list[str]]:
    lines = ["Argus — 제3자 구성요소 고지", "=" * 60, "",
             "Argus 자체는 MIT 라이선스다(동봉 LICENSE). 아래는 배포물에 함께 실린 구성요소다.", ""]
    missing = []
    for key, dist in dists.items():
        lic = _license(dist)
        lines.append(f"- {dist.metadata['Name']} {dist.version} — {lic}")
        if lic == "(명시 없음)":
            missing.append(key)
    lines += ["", "-" * 60, LGPL_NOTE, "-" * 60, ""]
    for key, dist in dists.items():
        for path, text in _license_texts(dist):
            lines += [f"==== {dist.metadata['Name']} {dist.version} · {path} ====", text.strip(), ""]
    return "\n".join(lines) + "\n", missing


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="배포물 제3자 고지 생성")
    ap.add_argument("--check", action="store_true", help="쓰지 않고 누락만 본다")
    args = ap.parse_args()
    dists = collect()
    text, missing = render(dists)
    names = set(dists)
    need = {"pyside6", "pyarrow"}
    absent = sorted(need - names)
    print(f"구성요소 {len(dists)}개 · 라이선스 명시 없음 {len(missing)} · 필수 누락 {absent or '없음'}")
    for key in missing:
        print(f"  [명시 없음] {key}")
    if absent or len(dists) < 10:
        print("[FAIL] 의존성을 충분히 못 찾았다 — ui extras(PySide6)까지 설치된 환경에서 돌린다")
        return 1
    if not args.check:
        OUT.write_text(text, encoding="utf-8")
        print(f"  {OUT} ({len(text):,}자)")
    print("[OK] third_party_notices")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

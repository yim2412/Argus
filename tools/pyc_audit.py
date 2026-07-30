"""캐시된 .pyc 의 모듈 수준 상수가 소스와 일치하는지 대조한다.

Python 은 .pyc 유효성을 (소스 mtime 초, 소스 크기) 로만 판단한다. 같은 초 안에
같은 크기로 되돌린 수정(mutation 테스트가 정확히 그렇다)은 캐시를 무효화하지 못하고,
그때부터 소스와 다른 바이트코드가 영구히 실행된다.

모듈 수준 숫자 상수 대입은 반드시 코드 객체의 co_consts 에 그 값이 들어간다.
소스가 말하는 값이 캐시의 co_consts 에 없으면 그 캐시는 낡았다.
"""

from __future__ import annotations

import ast
import importlib.util
import marshal
import pathlib
import sys


def module_constants(source: str) -> dict[str, float | int | str | bool]:
    """모듈 최상단의 `NAME = <리터럴>` 만 모은다."""
    out: dict[str, float | int | str | bool] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(
            node.value.value, (int, float, str, bool)
        ):
            out[target.id] = node.value.value
    return out


def cached_consts(pyc: pathlib.Path) -> set:
    code = marshal.loads(pyc.read_bytes()[16:])
    return {c for c in code.co_consts if isinstance(c, (int, float, str, bool))}


def main() -> int:
    roots = [pathlib.Path(p) for p in (sys.argv[1:] or ["argus", "tools"])]
    stale: list[tuple[str, str, object]] = []
    checked = 0

    for root in roots:
        for src in sorted(root.rglob("*.py")):
            pyc = pathlib.Path(importlib.util.cache_from_source(str(src)))
            if not pyc.exists():
                continue
            checked += 1
            try:
                consts = module_constants(src.read_text(encoding="utf-8"))
                cached = cached_consts(pyc)
            except Exception as exc:
                print(f"[SKIP] {src} — {exc}")
                continue
            for name, value in consts.items():
                if value not in cached:
                    stale.append((str(src), name, value))

    print(f"검사한 모듈 {checked}개")
    if not stale:
        print("[OK] 캐시와 소스의 모듈 상수가 전부 일치한다")
        return 0

    print(f"[FAIL] 캐시가 낡은 상수 {len(stale)}건")
    for path, name, value in stale:
        print(f"  {path}: {name} (소스 = {value!r}) 가 캐시에 없다")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""코드 기본값 ↔ defaults.yaml 대조 (2026-09-25 전면 감사).

배선 테스트는 **두 기본값이 같으면 배선이 끊겨도 참**이다(CLAUDE.md 08-04). 반대로 두 값이
다르면, 사용자 파일이 없거나 설정을 못 읽는 경로(`build()` 의 except 등)에서 **조용히 다른
문턱으로 돈다** — e2094f6(load_gates)·de7fbee(msedgewebview2)가 그 사고다. 둘 다 백테스트에서
기존 감사 도구가 놓쳤다.

`Settings()`(입력 없음 = 코드 기본값)와 `defaults.yaml` 을 잎 단위로 비교한다.
일부러 다르게 둔 곳은 `config_defaults_allow.txt` 에 `경로|사유: …`.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\config_defaults.py
    .venv\\Scripts\\python.exe tools\\audit\\config_defaults.py --selftest
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
ALLOW = pathlib.Path(__file__).with_name("config_defaults_allow.txt")
MIN_LEAVES = 100  # 2026-09-25 실측 잎 수보다 작게. 비교 대상이 비면 0건 초록이 된다


def _leaves(d: Any, path: tuple = ()):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _leaves(v, path + (str(k),))
    else:
        yield path, d


def _get(d: Any, path: tuple):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return KeyError
        d = d[k]
    return d


def _norm(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _load_allow() -> tuple[set[str], list[str]]:
    ok, errs = set(), []
    if ALLOW.exists():
        for i, raw in enumerate(ALLOW.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            path, _, reason = line.partition("|")
            if not reason.strip().startswith("사유:") or len(reason.strip()) < 6:
                errs.append(f"config_defaults_allow.txt:{i} 사유 필수: {line}")
            else:
                ok.add(path.strip())
    return ok, errs


def compare(yaml_data: dict, code: dict, allow: set[str]) -> tuple[int, list[str]]:
    diffs, n = [], 0
    for path, yv in _leaves(yaml_data):
        key = ".".join(path)
        cv = _get(code, path)
        n += 1
        if cv is KeyError:
            # 섹션 자체가 스키마에 없으면 config_fuzz 의 silent 가 센다. 섹션은 있는데
            # 코드 쪽에 키가 없으면 **코드 기본값이 비어 있는 것**이다 — e2094f6 이 정확히
            # 이 모양(`load_gates` 코드 기본값 `{}`)이었고, 처음엔 여기서 건너뛰어 놓쳤다.
            if path[0] in code:
                diffs.append(f"{key}: 코드 기본값에 없음 ≠ yaml {yv!r}")
            continue
        if _norm(cv) != _norm(yv) and key not in allow:
            diffs.append(f"{key}: 코드 {cv!r} ≠ yaml {yv!r}")
    return n, diffs


def run(yaml_data: dict | None = None) -> int:
    sys.path.insert(0, str(ROOT))
    from argus.config.loader import Settings

    if yaml_data is None:
        yaml_data = yaml.safe_load((ROOT / "argus/config/defaults.yaml").read_text(encoding="utf-8"))
    code = Settings().model_dump()
    allow, errs = _load_allow()
    n, diffs = compare(yaml_data, code, allow)
    print(f"잎 {n}개 (하한 {MIN_LEAVES}) · 허용 {len(allow)} · 불일치 {len(diffs)}")
    for d in diffs:
        print(f"  [FAIL] {d}")
    for e in errs:
        print(f"  [FAIL] {e}")
    bad = bool(diffs or errs or n < MIN_LEAVES)
    if n < MIN_LEAVES:
        print(f"  [FAIL] 비교 대상 {n} < 하한 {MIN_LEAVES}")
    print("[FAIL] config_defaults" if bad else "[OK] config_defaults")
    return 1 if bad else 0


def selftest() -> int:
    data = yaml.safe_load((ROOT / "argus/config/defaults.yaml").read_text(encoding="utf-8"))
    results = []
    # 숫자 하나를 틀면 FAIL 이어야 한다 (허용 목록에 없는 잎)
    allow, _ = _load_allow()
    target = next(p for p, v in _leaves(data)
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and ".".join(p) not in allow)
    cur = data
    for k in target[:-1]:
        cur = cur[k]
    cur[target[-1]] = cur[target[-1]] + 12345
    results.append((f"값 틀기({'.'.join(target)})", run(data) == 1))
    results.append(("잎 하한", run({"a": 1}) == 1))
    print("\n== selftest ==")
    for k, ok in results:
        print(f"  {'[OK]' if ok else '[FAIL]'} {k}")
    return 0 if all(ok for _, ok in results) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="코드 기본값 ↔ defaults.yaml")
    ap.add_argument("--selftest", action="store_true")
    return selftest() if ap.parse_args().selftest else run()


if __name__ == "__main__":
    sys.exit(main())

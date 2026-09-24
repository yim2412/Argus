"""문서에 적힌 숫자 ↔ 실제 설정값 대조 (2026-09-25 전면 감사).

문서의 숫자는 설정이 바뀌어도 아무도 안 고친다. 예외도 로그도 없이 문서만 틀어진다 —
그리고 다음 사람은 문서를 믿고 판단한다. 그래서 (문서 문구, 설정 경로) 쌍을 표로 두고
둘이 어긋나면 FAIL 한다.

설정은 **사용자 파일·환경변수를 빼고** 읽는다(`load_settings(use_user_file=False,
use_env=False)`). 이 PC 의 `%APPDATA%` 설정이 문서와 같아서 통과하는 일이 없게.

문구가 문서에서 사라져도 FAIL 이다 — 표가 아무것도 안 재는 채로 초록이면 안 된다.

사용:
    .venv\\Scripts\\python.exe tools\\audit\\doc_numbers.py
    .venv\\Scripts\\python.exe tools\\audit\\doc_numbers.py --selftest
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class Pair:
    doc: str  # ROOT 기준 경로
    pattern: str  # 숫자 하나를 그룹 1 로 잡는 정규식
    source: str  # 사람이 읽는 설정 경로
    get: Callable[[Any], float]  # Settings → 값
    scale: float = 1.0  # 문서 단위 → 설정 단위 (예: 분 → 초 = 60)


def _xml(rel: str, tag: str) -> Callable[[Any], float]:
    def get(_s: Any) -> float:
        text = (ROOT / rel).read_text(encoding="utf-16" if _is_utf16(rel) else "utf-8")
        m = re.search(rf"<{tag}>PT(\d+)M</{tag}>", text)
        return float(m.group(1)) if m else float("nan")
    return get


def _is_utf16(rel: str) -> bool:
    return (ROOT / rel).read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")


PAIRS: list[Pair] = [
    Pair("CLAUDE.md", r"예산\(CPU (\d+(?:\.\d+)?)%", "budget.cpu_percent",
         lambda s: s.budget.cpu_percent),
    Pair("CLAUDE.md", r"예산\(CPU [\d.]+% / RSS (\d+)MB\)", "budget.rss_mb",
         lambda s: s.budget.rss_mb),
    Pair("CLAUDE.md", r"보존 기한\(원본\s*\n?\s*(\d+)시간\)", "retention.raw_hours",
         lambda s: s.retention.raw_hours),
    Pair("README.md", r"metrics_raw\(초, (\d+)시간\)", "retention.raw_hours",
         lambda s: s.retention.raw_hours),
    Pair("README.md", r"process_metrics\(초, (\d+)시간\)", "retention.process_hours",
         lambda s: s.retention.process_hours),
    Pair("README.md", r"`self_telemetry` \| (\d+)초", "self_telemetry.interval_s",
         lambda s: s.self_telemetry.interval_s),
    Pair("README.md", r"`heap_census` \| (\d+)분", "heap_census.interval_s",
         lambda s: s.heap_census.interval_s, scale=60),
    Pair("README.md", r"`metrics_raw` \| (\d+)초", "collector.system_interval_s",
         lambda s: s.collector.system_interval_s),
    Pair("README.md", r"`gpu_metrics` \| (\d+)초", "collector.gpu_interval_s",
         lambda s: s.collector.gpu_interval_s),
    Pair("README.md", r"로그온 (\d+)분 뒤에 시작", "tools/argus_task.xml <Delay>",
         _xml("tools/argus_task.xml", "Delay")),
]


def check(pairs: list[Pair], root: pathlib.Path = ROOT) -> int:
    from argus.config.loader import load_settings

    settings = load_settings(use_user_file=False, use_env=False)
    bad = 0
    for p in pairs:
        text = (root / p.doc).read_text(encoding="utf-8")
        m = re.search(p.pattern, text)
        if not m:
            print(f"  [FAIL] {p.doc}: 문구를 못 찾음 /{p.pattern}/ — 문서가 바뀌었으면 표를 고친다")
            bad += 1
            continue
        doc_val = float(m.group(1)) * p.scale
        try:
            cfg_val = float(p.get(settings))
        except Exception as e:  # noqa: BLE001 - 경로가 끊긴 것도 결과다
            print(f"  [FAIL] {p.source}: 설정을 못 읽음 ({type(e).__name__}: {e})")
            bad += 1
            continue
        ok = abs(doc_val - cfg_val) < 1e-9
        bad += not ok
        print(f"  {'[OK]  ' if ok else '[FAIL]'} {p.doc} {m.group(0)!r} = {doc_val:g}  ↔  "
              f"{p.source} = {cfg_val:g}")
    print(f"대조 {len(pairs)}쌍 · 불일치 {bad}")
    print("[OK] doc_numbers" if bad == 0 else "[FAIL] doc_numbers")
    return 0 if bad == 0 else 1


def selftest() -> int:
    """설정값을 틀어 FAIL 이 나는지, 문구를 없애 FAIL 이 나는지 본다."""
    results = []
    p0 = PAIRS[0]
    wrong = Pair(p0.doc, p0.pattern, p0.source, lambda s: p0.get(s) + 1)
    results.append(("값 불일치", check([wrong]) == 1))
    gone = Pair(p0.doc, r"이런문구는없다(\d+)", p0.source, p0.get)
    results.append(("문구 사라짐", check([gone]) == 1))
    broken = Pair(p0.doc, p0.pattern, "없는.경로", lambda s: s.no_such_section.x)
    results.append(("설정 경로 끊김", check([broken]) == 1))
    results.append(("원래 표", check(PAIRS) in (0, 1)))  # 실행 자체가 되는지만
    print("\n== selftest ==")
    for k, ok in results:
        print(f"  {'[OK]' if ok else '[FAIL]'} {k}")
    return 0 if all(ok for _, ok in results) else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="문서 수치 ↔ 설정값 대조")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else check(PAIRS)


if __name__ == "__main__":
    sys.exit(main())

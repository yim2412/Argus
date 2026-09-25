"""F-005 프로브 — 룰 알림 문장(explain)의 시간이 실제 지속 조건(for)과 같은가.

문장에 "N초/N분"이 있는데 그 값이 `for` 와 다르면 사용자에게 사실과 다른 말을 한다.
PASS = 불일치 0. FAIL = 불일치 룰 목록.
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from argus.detection.rules import parse_duration  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
data = yaml.safe_load((ROOT / "argus/config/rules.yaml").read_text(encoding="utf-8"))
bad = []
for r in data["rules"]:
    for_s = parse_duration(r.get("for", "30s"), field_name="for")
    said = [int(n) * (60 if u == "분" else 1)
            for n, u in re.findall(r"(\d+)\s*(초|분)", r.get("explain", ""))]
    if said and for_s not in said:
        bad.append(f"{r['name']}: 문장 {said}초 ≠ for {for_s:g}초")
print(f"룰 {len(data['rules'])}개 · 불일치 {len(bad)}")
for b in bad:
    print("  " + b)
print("[PASS]" if not bad else "[FAIL] 알림 문장과 지속 조건이 다르다")
sys.exit(0 if not bad else 1)

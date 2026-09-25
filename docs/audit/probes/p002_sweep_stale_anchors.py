"""F-002 프로브 — mutation_sweep 의 무력화 원문이 소스에 정확히 1회씩 있는가.

0회면 그 변이는 실행할 수 없고, 전체 실행이 거기서 `[중단]` 된다.
PASS = 전부 1회. FAIL = 끊긴 변이 목록.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("_ms", ROOT / "tools" / "mutation_sweep.py")
ms = importlib.util.module_from_spec(spec)
sys.modules["_ms"] = ms
spec.loader.exec_module(ms)
sys.stdout.reconfigure(encoding="utf-8")

stale = [(m.key, rel, (ROOT / rel).read_text(encoding="utf-8").count(orig))
         for m in ms.MUTANTS for rel, orig, _ in m.edits
         if (ROOT / rel).read_text(encoding="utf-8").count(orig) != 1]
print(f"변이 {len(ms.MUTANTS)}개 · 끊긴 원문 {len(stale)}")
for key, rel, n in stale:
    print(f"  {key}: {rel} ({n}회)")
print("[PASS]" if not stale else "[FAIL] 앵커가 끊긴 변이가 있다")
sys.exit(0 if not stale else 1)

r"""F-001 프로브 — 사용자 rules.yaml 이 읽히는가.

rules.yaml 머리말: "사용자 룰은 %APPDATA%\Argus\rules.yaml 에 두면 이 파일을 대체한다".
데이터 폴더(ARGUS_DATA_DIR 로 격리)에 룰 하나짜리 파일을 두고, 제품 생성자(registry.build)
가 만든 엔진의 룰 이름을 본다. 사용자 파일이 쓰였으면 룰은 1개(`__사용자룰__`)다.

PASS = 사용자 파일이 쓰였다. FAIL = 동봉본이 그대로 쓰였다(약속 위반).
"""
import os, sys, tempfile, pathlib, logging

tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["ARGUS_DATA_DIR"] = str(tmp)
(tmp / "rules.yaml").write_text(
    "version: 1\nrules:\n  - name: __사용자룰__\n    when: {all: [{metric: cpu_total, op: '>', value: 1}]}\n",
    encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
logging.disable(logging.CRITICAL)
from argus.detection import registry  # noqa: E402

names = [r.name for r in registry.build("rules").rules]
sys.stdout.reconfigure(encoding="utf-8")
print(f"룰 {len(names)}개: {names[:3]}…")
ok = names == ["__사용자룰__"]
print("[PASS] 사용자 룰이 쓰였다" if ok else "[FAIL] 사용자 rules.yaml 이 무시됐다 — 동봉본이 쓰였다")
sys.exit(0 if ok else 1)

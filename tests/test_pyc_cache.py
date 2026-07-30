"""바이트코드 캐시가 소스와 어긋나지 않았는지 본다.

**조용히 깨지는 규칙의 극단이다.** 예외도, 로그도, 값의 이상함도 없다. 소스에는
`_SCORE_SATURATION_Z = 8.0` 이 있는데 실행되는 것은 `1.0` 인 상태가 며칠 유지된다.

Python 은 `.pyc` 의 유효성을 **(소스 mtime 초 단위, 소스 크기)** 로만 판단한다.
mutation 테스트는 상수를 바꿔 한 번 실행하고 되돌리는데, 되돌림이 같은 초 안에
일어나고 글자 수가 같으면(`8.0` → `1.0`) 두 값 다 그대로여서 캐시가 무효화되지 않는다.
그 순간부터 **테스트도, 상주 프로세스도 무력화된 상수로 돈다.**

2026-07-30 에 실제로 겪었다. 02:25 의 mutation 이 `_SCORE_SATURATION_Z` 를 1.0 으로
남겼고, "36개 통과"라는 mutation 측정 결과 자체가 이것 때문에 오염됐다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _audit():
    """`tools/pyc_audit.py` 를 경로로 불러온다 — `tools` 는 패키지가 아니다."""
    spec = importlib.util.spec_from_file_location("pyc_audit", ROOT / "tools" / "pyc_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bytecode_cache_matches_source_constants():
    """캐시된 모듈 상수가 소스와 다르면 실패한다.

    고치는 방법: `__pycache__` 를 지운다. mutation 테스트를 한 뒤에는 항상 지운다.
    """
    audit = _audit()
    stale = []
    for root in (ROOT / "argus", ROOT / "tools"):
        for src in sorted(root.rglob("*.py")):
            pyc = Path(importlib.util.cache_from_source(str(src)))
            if not pyc.exists():
                continue
            consts = audit.module_constants(src.read_text(encoding="utf-8"))
            cached = audit.cached_consts(pyc)
            stale += [
                f"{src.relative_to(ROOT)}: {name} — 소스 {value!r} 가 캐시에 없다"
                for name, value in consts.items()
                if value not in cached
            ]
    assert not stale, "바이트코드 캐시가 소스와 어긋났다:\n" + "\n".join(stale)


def test_audit_detects_a_planted_mismatch(tmp_path):
    """감사 자체가 실제로 어긋남을 잡는지 본다 — 통과는 증거가 아니다.

    소스를 컴파일해 캐시를 만든 뒤 **크기를 유지한 채** 상수만 바꾼다.
    실제 함정을 그대로 재현한 것이다.
    """
    audit = _audit()
    src = tmp_path / "victim.py"
    src.write_text("THRESHOLD = 8.0\n", encoding="utf-8")

    pyc = tmp_path / "victim.pyc"
    code = compile(src.read_text(encoding="utf-8"), str(src), "exec")
    import marshal

    pyc.write_bytes(b"\x00" * 16 + marshal.dumps(code))

    assert audit.module_constants(src.read_text(encoding="utf-8")) == {"THRESHOLD": 8.0}
    assert 8.0 in audit.cached_consts(pyc)

    src.write_text("THRESHOLD = 1.0\n", encoding="utf-8")  # 글자 수 동일
    consts = audit.module_constants(src.read_text(encoding="utf-8"))
    assert consts == {"THRESHOLD": 1.0}
    assert 1.0 not in audit.cached_consts(pyc), "감사가 어긋남을 놓쳤다"


@pytest.mark.parametrize("literal", ["8.0", "'문자열'", "True", "300"])
def test_module_constants_reads_supported_literals(literal):
    audit = _audit()
    assert audit.module_constants(f"NAME = {literal}\n")["NAME"] == eval(literal)  # noqa: S307

"""로그 `extra` 가 `LogRecord` 예약 속성을 덮어쓰지 않는지 본다.

**DEBUG 로 바꾸면 죽는 종류의 결함이다.** `logging` 은 `extra` 의 키가 `LogRecord` 의
기존 속성과 겹치면 `KeyError` 를 던진다. 그런데 `log.debug(...)` 는 레벨이 INFO 이면
레코드를 만들기 전에 조기 반환하므로, **운영 기본 설정에서는 아무 일도 일어나지 않는다.**
사용자가 `log_level: DEBUG` 로 바꾸는 순간 그 자리에서 스레드가 죽는다.

2026-07-30 에 `procleak` 의 지문 억제 로그가 `extra={"process": ...}` 를 써서 실제로
터졌다. 하필 "조용히 삼키지 않는다"는 목적으로 넣은 로그였다 — 보이게 하면 터지는 코드였다.

그래서 여기서는 **소스를 훑어 예약 키 사용을 금지**한다. 실행 경로를 다 타서 확인하려면
모든 로그 지점을 DEBUG 로 재현해야 하는데, 억제·실패 경로는 재현 조건이 까다롭다.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# `logging.LogRecord` 가 이미 쓰는 이름들. 하드코딩하지 않고 실제 레코드에서 뽑는다 —
# 파이썬 버전이 올라가며 늘어날 수 있고(3.12 의 `taskName`), 목록을 손으로 관리하면
# 새 버전에서 조용히 놓친다.
RESERVED = set(
    vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None)).keys()
) | {"message", "asctime"}


def _extra_keys(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                continue
            for key in kw.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    out.append((key.lineno, key.value))
    return out


def test_no_log_extra_overwrites_reserved_record_attributes():
    offenders = []
    for root in (ROOT / "argus", ROOT / "tools"):
        for src in sorted(root.rglob("*.py")):
            if "__pycache__" in str(src):
                continue
            for lineno, key in _extra_keys(src):
                if key in RESERVED:
                    offenders.append(f"{src.relative_to(ROOT)}:{lineno} — extra 키 {key!r}")
    assert not offenders, (
        "LogRecord 예약 속성을 덮어쓴다 (DEBUG 레벨에서 KeyError):\n" + "\n".join(offenders)
    )


def test_logging_really_raises_on_reserved_key():
    """전제를 확인한다 — 실제로 예외가 나는지. 안 나면 위 테스트는 무의미하다."""
    logger = logging.getLogger("argus.test.reserved")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.NullHandler())
    with pytest.raises(KeyError):
        logger.debug("겹치는 키", extra={"process": "python"})
    logger.debug("겹치지 않는 키", extra={"proc_name": "python"})  # 이건 통과해야 한다

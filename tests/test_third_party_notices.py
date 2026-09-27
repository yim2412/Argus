"""배포물에 라이선스·제3자 고지가 실린다 (감사 F-011).

배포물(`dist/argus`, `dist/argus-ui`)에 자체 LICENSE 도 제3자 고지도 없었다. PySide6/Qt 는 LGPL-3 이라
고지와 교체 가능성 안내가, pyarrow(Apache-2.0)는 NOTICE 가 필요하다. 빌드는 느리므로 여기서는 생성기와
빌드 정의(spec·배포 스크립트)가 두 파일을 싣는지를 잰다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))


def test_generator_carries_lgpl_note_and_pyarrow_notice():
    pytest.importorskip("PySide6")
    pytest.importorskip("pyarrow")
    import third_party_notices as tpn

    dists = tpn.collect()
    assert {"pyside6", "pyarrow", "pydantic"} <= set(dists), f"런타임 의존성을 못 따라갔다: {sorted(dists)}"
    text, missing = tpn.render(dists)
    assert "LGPL-3.0" in text and "교체할 수 있다" in text, "Qt(LGPL) 교체 가능성 안내가 없다"
    assert "pyarrow" in text and "NOTICE" in text, "pyarrow NOTICE 가 안 실렸다"
    assert "pytest" not in dists, "개발 전용 도구가 배포 고지에 섞였다"


@pytest.mark.parametrize("spec", ["argus.spec", "argus_ui.spec"])
def test_specs_ship_license_and_notices(spec):
    text = (ROOT / "packaging" / spec).read_text(encoding="utf-8")
    assert '"LICENSE"' in text and '"THIRD_PARTY_NOTICES.txt"' in text, f"{spec} 가 고지를 안 싣는다"


def test_deploy_script_copies_and_requires_notices():
    text = (ROOT / "packaging" / "make_deploy.ps1").read_text(encoding="utf-8-sig")
    assert "THIRD_PARTY_NOTICES.txt" in text and '"LICENSE"' in text
    assert "배포물에 실을 고지가 없습니다" in text, "고지가 없을 때 멈추지 않는다 — 조용히 빠진 채 배포된다"


def test_notices_file_is_committed_and_fresh_enough():
    """생성된 파일이 저장소에 있어야 배포 스크립트가 싣는다. 비어 있거나 핵심이 빠졌으면 다시 만든다."""
    path = ROOT / "packaging" / "THIRD_PARTY_NOTICES.txt"
    assert path.exists(), "packaging/THIRD_PARTY_NOTICES.txt 가 없다 — tools/third_party_notices.py 를 돌린다"
    text = path.read_text(encoding="utf-8")
    assert "PySide6" in text and "pyarrow" in text and "MIT" in text

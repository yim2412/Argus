"""사용자 settings.yaml — 업데이트된 기본값이 먹는다 (감사 F-026).

첫 실행이 defaults.yaml **전체**를 복사하던 시절에는 사본의 모든 키가 사용자 값이 되어, 업데이트로
바뀐 기본값이 기존 설치에 영영 안 먹었다 — 오류도 없이. 배포가 전제라 두 번째 릴리스부터 모든
사용자에게 해당하는 문제였다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from argus.config import loader

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = (ROOT / "argus" / "config" / "defaults.yaml").read_text(encoding="utf-8")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """격리 데이터 폴더 + 바꿔 끼울 수 있는 '패키지 기본값' 파일."""
    monkeypatch.setenv("ARGUS_DATA_DIR", str(tmp_path / "data"))
    pkg = tmp_path / "pkg"
    (pkg / "config").mkdir(parents=True)
    (pkg / "config" / "defaults.yaml").write_text(DEFAULTS, encoding="utf-8")
    monkeypatch.setattr(loader, "resource_path", lambda rel: pkg / rel)
    return pkg / "config" / "defaults.yaml"


def _bump(text: str) -> str:
    """'다음 릴리스'에서 기본값 하나가 바뀐 것처럼."""
    assert "  cpu_percent: 2.0" in text
    return text.replace("  cpu_percent: 2.0", "  cpu_percent: 1.5", 1)


def test_first_run_template_lets_updated_defaults_through(env):
    first = loader.load_settings(use_env=False)                  # 첫 실행 — 템플릿을 만든다
    user = loader.user_config_path().read_text(encoding="utf-8")
    assert yaml.safe_load(user) == {"config_version": loader.CONFIG_VERSION}, "템플릿에 살아 있는 키가 있다"
    assert "# budget:" in user and "cpu_percent" in user, "설명서(주석)가 사라졌다"
    assert first.budget.cpu_percent == 2.0
    env.write_text(_bump(DEFAULTS), encoding="utf-8")            # 업데이트
    assert loader.load_settings(use_env=False).budget.cpu_percent == 1.5, "업데이트된 기본값이 안 먹는다"


def test_old_full_copy_would_have_frozen_the_default(env):
    """대조 — 옛 방식(전체 사본)이면 업데이트가 안 먹는다. 이게 참이어야 위 테스트가 무언가를 잰다."""
    loader.user_config_path().write_text(DEFAULTS.replace("config_version: 1\n", ""), encoding="utf-8")
    env.write_text(_bump(DEFAULTS), encoding="utf-8")
    assert loader.load_settings(use_env=False).budget.cpu_percent == 2.0


def test_prune_keeps_only_changed_values_and_behaviour(env):
    sys.path.insert(0, str(ROOT / "tools"))
    from settings_prune import pruned_text

    old = DEFAULTS.replace("config_version: 1\n", "").replace("  log_level: INFO", "  log_level: DEBUG", 1)
    new_text, changed, unknown = pruned_text(old, DEFAULTS)
    data = yaml.safe_load(new_text)
    assert data == {"config_version": loader.CONFIG_VERSION, "general": {"log_level": "DEBUG"}}, data
    assert changed == ["general.log_level: 'INFO' → 'DEBUG'"] and unknown == []
    # 정리 전후로 실제 설정이 같다 — 동작을 바꾸지 않는다
    loader.user_config_path().write_text(old, encoding="utf-8")
    before = loader.load_settings(use_env=False).model_dump()
    loader.user_config_path().write_text(new_text, encoding="utf-8")
    assert loader.load_settings(use_env=False).model_dump() == before
    # 그리고 정리 뒤에는 업데이트가 먹는다
    env.write_text(_bump(DEFAULTS), encoding="utf-8")
    assert loader.load_settings(use_env=False).budget.cpu_percent == 1.5


def test_typo_keys_are_reported_not_silently_ignored(env):
    """오타 키는 무시되되 **드러난다** (감사 F-007). 막지는 않는다 — 은퇴한 키가 기동을 막으면 안 된다."""
    loader.load_settings(use_env=False)                           # 템플릿 생성
    path = loader.user_config_path()
    assert loader.user_config_unknown_keys() == [], "대조: 템플릿에는 모르는 키가 없다"
    path.write_text(
        "config_version: 1\ngeneral:\n  log_levl: DEBUG\ndetecton:\n  enabled: false\n"
        "detection:\n  load_gates:\n    gpu_temp_c: {metric: gpu_util_percent, min: 50}\n",
        encoding="utf-8")
    assert loader.load_settings(use_env=False).general.log_level == "INFO", "오타 키가 기동을 막거나 값을 바꿨다"
    assert loader.user_config_unknown_keys() == ["general.log_levl", "detecton"]

    import time

    from argus.desktop.app import _health_line

    now = time.time()
    text, detail, _c, _id = _health_line({"sample_ts": now - 1, "open": None, "last_end_ts": None, "unlabeled": 0,
                                          "broken": [], "config_unknown": ["general.log_levl"]}, now)
    assert text == "설정 확인이 필요합니다" and "general.log_levl" in detail

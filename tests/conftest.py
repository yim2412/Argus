"""테스트 전체를 사용자 데이터 폴더에서 떼어 놓는다.

이게 없던 동안(2026-09-27 에 발견) 테스트는 실제 `%APPDATA%\\Argus` 를 썼다:
- 주입한 기동 실패가 `logs/crash_*.json` 으로 떨어져 **진짜 크래시 기록에 섞였다**(pytest 한 번에 1개).
- 사용자 `settings.yaml`·`machine_profile.json` 을 **읽었다** — 테스트 결과가 이 PC 의 설정과
  하드웨어(RTX 3080)에 따라 달라질 수 있었다. 설계 규칙 2 "하드웨어를 가정하지 않는다"의 테스트판.

세션 하나에 빈 폴더 하나. 이미 설정돼 있어도 덮어쓴다 — 테스트가 실제 데이터를 볼 이유는 없다.
개별 테스트가 `monkeypatch.setenv(ENV_DATA_DIR, tmp_path)` 로 더 좁히는 것은 그대로 된다.
재발 방지는 `tools/audit/run_gates.py` 의 pytest 게이트 — 가짜 APPDATA 에 뭐라도 생기면 FAIL.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from argus.paths import ENV_DATA_DIR

_ISOLATED = tempfile.mkdtemp(prefix="argus_test_data_")


def pytest_configure(config) -> None:
    os.environ[ENV_DATA_DIR] = _ISOLATED


def pytest_unconfigure(config) -> None:
    shutil.rmtree(_ISOLATED, ignore_errors=True)

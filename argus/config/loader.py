"""설정 로드·병합·검증.

우선순위:  패키지 기본값  <  사용자 settings.yaml  <  환경변수(ARGUS_*)

첫 실행 시 기본값 사본을 `%APPDATA%\\Argus\\settings.yaml` 에 써 준다. 사용자가
어떤 값을 조절할 수 있는지 파일을 열어보면 알 수 있어야 하기 때문이다(주석 포함).

검증은 pydantic 이 한다. 사용자가 직접 편집하는 파일이므로 잘못된 값이 들어오면
조용히 넘어가지 않고 어느 키가 왜 틀렸는지 알려주고 멈춘다.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from ..paths import resource_path, user_config_path

ENV_PREFIX = "ARGUS_"
ENV_NESTED_SEP = "__"


class GeneralSettings(BaseModel):
    log_level: str = "INFO"
    console_log: bool = True

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level 은 {sorted(allowed)} 중 하나여야 합니다 (받은 값: {v!r})")
        return upper


class StorageSettings(BaseModel):
    flush_interval_ms: int = Field(default=200, ge=10, le=60_000)
    flush_max_rows: int = Field(default=500, ge=1)
    queue_max_rows: int = Field(default=20_000, ge=100)


class BudgetSettings(BaseModel):
    cpu_percent: float = Field(default=2.0, gt=0)
    rss_mb: int = Field(default=300, gt=0)
    check_interval_s: float = Field(default=5.0, gt=0)
    breach_streak_to_throttle: int = Field(default=3, ge=1)
    calm_streak_to_relax: int = Field(default=12, ge=1)
    throttle_multipliers: list[float] = Field(default=[1.0, 2.0, 4.0, 10.0])

    @field_validator("throttle_multipliers")
    @classmethod
    def _valid_multipliers(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("throttle_multipliers 는 비어 있을 수 없습니다")
        if v[0] != 1.0:
            raise ValueError("throttle_multipliers 의 첫 값(정상 상태)은 1.0 이어야 합니다")
        if any(b < a for a, b in zip(v, v[1:])):
            raise ValueError("throttle_multipliers 는 증가 순서여야 합니다")
        return v


class SelfTelemetrySettings(BaseModel):
    enabled: bool = True
    interval_s: float = Field(default=5.0, gt=0)


class CalibrationSettings(BaseModel):
    enabled: bool = True
    disk_bench_mb: int = Field(default=16, ge=1, le=512)
    reuse_days: int = Field(default=90, ge=0)


class Settings(BaseModel):
    general: GeneralSettings = GeneralSettings()
    storage: StorageSettings = StorageSettings()
    budget: BudgetSettings = BudgetSettings()
    self_telemetry: SelfTelemetrySettings = SelfTelemetrySettings()
    calibration: CalibrationSettings = CalibrationSettings()


class ConfigError(Exception):
    """설정 파일이 잘못됐을 때. 메시지를 사람이 읽을 수 있게 담는다."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """override 를 base 위에 얹는다. dict 는 재귀 병합, 그 외는 통째로 교체."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(text: str) -> Any:
    """환경변수 문자열을 YAML 규칙으로 해석한다 (`true`, `3`, `[1, 2]` 등)."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def _env_overrides() -> dict[str, Any]:
    """ARGUS_<섹션>__<키> 형태의 환경변수를 중첩 dict 로 바꾼다."""
    out: dict[str, Any] = {}
    for name, raw in os.environ.items():
        if not name.startswith(ENV_PREFIX):
            continue
        key = name[len(ENV_PREFIX) :]
        # ARGUS_DATA_DIR 은 경로 결정용이라 설정 스키마에 없다. 건너뛴다.
        if ENV_NESTED_SEP not in key:
            continue
        parts = [p.lower() for p in key.split(ENV_NESTED_SEP) if p]
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _coerce(raw)
    return out


def _read_yaml(path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {path}\n  {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"설정 파일의 YAML 문법이 잘못됐습니다: {path}\n  {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일의 최상위는 매핑이어야 합니다: {path}")
    return data


def ensure_user_config() -> bool:
    """사용자 설정 파일이 없으면 기본값 사본을 만든다. 새로 만들었으면 True."""
    target = user_config_path()
    if target.exists():
        return False
    source = resource_path("config/defaults.yaml")
    try:
        shutil.copyfile(source, target)
    except OSError:
        # 사본 생성 실패가 실행을 막을 이유는 없다. 기본값으로 계속 돌면 된다.
        return False
    return True


def load_settings(*, use_user_file: bool = True, use_env: bool = True) -> Settings:
    """설정을 병합·검증해 돌려준다."""
    merged = _read_yaml(resource_path("config/defaults.yaml"))

    if use_user_file:
        ensure_user_config()
        path = user_config_path()
        if path.exists():
            merged = _deep_merge(merged, _read_yaml(path))

    if use_env:
        merged = _deep_merge(merged, _env_overrides())

    try:
        return Settings.model_validate(merged)
    except ValidationError as e:
        lines = [f"설정 값이 잘못됐습니다 ({user_config_path()}):"]
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"])
            lines.append(f"  - {where}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from e


if __name__ == "__main__":  # 스모크: python -m argus.config.loader
    import json

    created = ensure_user_config()
    print(f"  사용자 설정: {user_config_path()} ({'생성됨' if created else '기존'})")
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"[FAIL] {e}")
        raise SystemExit(1)
    print(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2))
    print("[OK] config.loader")

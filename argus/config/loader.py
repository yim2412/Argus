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


class GapMonitorSettings(BaseModel):
    enabled: bool = True
    interval_s: float = Field(default=1.0, gt=0)
    threshold_s: float = Field(default=30.0, gt=0)


class CalibrationSettings(BaseModel):
    enabled: bool = True
    disk_bench_mb: int = Field(default=16, ge=1, le=512)
    reuse_days: int = Field(default=90, ge=0)


class ProcessCollectorSettings(BaseModel):
    enabled: bool = True
    collect_interval_s: float = Field(default=1.0, gt=0)
    top_cpu: int = Field(default=15, ge=0)
    top_memory: int = Field(default=10, ge=0)
    full_store_interval_s: float = Field(default=30.0, gt=0)
    fallback_interval_s: float = Field(default=15.0, gt=0)

    @field_validator("full_store_interval_s")
    @classmethod
    def _slower_than_collect(cls, v: float, info) -> float:
        collect = info.data.get("collect_interval_s")
        if collect is not None and v < collect:
            raise ValueError("full_store_interval_s 는 collect_interval_s 보다 작을 수 없습니다")
        return v


class NetworkCollectorSettings(BaseModel):
    enabled: bool = True
    interval_s: float = Field(default=30.0, gt=0)
    max_rows_per_snapshot: int = Field(default=500, ge=1)


class CollectorSettings(BaseModel):
    system_interval_s: float = Field(default=1.0, gt=0)
    pdh_enabled: bool = True
    gpu_enabled: bool = True
    gpu_interval_s: float = Field(default=1.0, gt=0)
    process: ProcessCollectorSettings = ProcessCollectorSettings()
    network: NetworkCollectorSettings = NetworkCollectorSettings()


class RollupSettings(BaseModel):
    """1분 롤업. 장기 데이터가 존재할 수 있게 하는 유일한 경로다."""

    enabled: bool = True
    interval_s: float = Field(default=60.0, gt=0)
    # 진행 중인 분을 접으면 반쪽이 된다. 배치 writer 가 큐를 비우는 시간까지 감안한 여유.
    lag_s: float = Field(default=90.0, ge=60.0)
    # 한 틱에 접을 버킷 상한. 오래 꺼져 있다 켜면 수천 분이 밀려 있는데,
    # 그걸 한 번에 처리하면 우리가 만든 디스크 IO 가 관측 대상을 오염시킨다.
    max_buckets_per_run: int = Field(default=720, ge=1)
    # 프로세스 5분 롤업에서 버킷마다 남길 프로그램 수(CPU 상위 N + 메모리 상위 N).
    # 조건 필터가 아니라 상위 N 인 이유: 프로세스가 500개인 PC 에서도 하루 행 수에
    # 상한이 있어야 한다. 하드웨어를 가정하지 않는다.
    process_top_n: int = Field(default=40, ge=1)


class WarmSettings(BaseModel):
    """웜 스토어(Parquet + DuckDB). 완전히 끝난 날짜만 내보낸다."""

    enabled: bool = True
    interval_s: float = Field(default=3600.0, gt=0)
    # 오늘과 어제는 건드리지 않는다. Parquet 은 append 가 안 되므로 한 번 쓰면
    # 불변이어야 하고, 그러려면 그 날짜에 더 들어올 데이터가 없어야 한다.
    export_after_days: int = Field(default=1, ge=1)
    compression: str = "zstd"
    # 내보낸 날짜를 metrics_1m 에서 지울지. 끄면 두 곳에 중복 보관된다.
    purge_after_export: bool = True


class RetentionSettings(BaseModel):
    raw_hours: int = Field(default=24, ge=1)
    process_hours: int = Field(default=24, ge=1)
    network_hours: int = Field(default=72, ge=1)
    events_days: int = Field(default=30, ge=1)
    self_telemetry_days: int = Field(default=7, ge=1)
    interval_s: float = Field(default=300.0, gt=0)


class DetectionSettings(BaseModel):
    """탐지 엔진. 임계값은 여기가 아니라 `rules.yaml` 에 있다 — 이건 엔진 설정이다."""

    enabled: bool = True
    detector: str = "rules"
    # 저장된 관측을 따라 읽는 주기. 룰의 지속 조건이 30초 이상이라 촘촘할 필요가 없다.
    interval_s: float = Field(default=10.0, gt=0)
    # "평소"를 계산할 창. 길수록 안정적이지만 최근 변화에 둔해진다.
    baseline_window_s: float = Field(default=1800.0, gt=0)
    # 이만큼 표본이 모여야 판정한다. 표본 3개의 중앙값은 중앙값이 아니라 우연이다.
    min_samples: int = Field(default=60, ge=2)
    # 알림 발송. Phase 9 까지는 꺼 둔다 — 오탐률이 검증되기 전에 알리면 되돌릴 수 없다.
    notify: bool = False


class Settings(BaseModel):
    general: GeneralSettings = GeneralSettings()
    storage: StorageSettings = StorageSettings()
    budget: BudgetSettings = BudgetSettings()
    self_telemetry: SelfTelemetrySettings = SelfTelemetrySettings()
    gap_monitor: GapMonitorSettings = GapMonitorSettings()
    calibration: CalibrationSettings = CalibrationSettings()
    collector: CollectorSettings = CollectorSettings()
    rollup: RollupSettings = RollupSettings()
    warm: WarmSettings = WarmSettings()
    retention: RetentionSettings = RetentionSettings()
    detection: DetectionSettings = DetectionSettings()


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

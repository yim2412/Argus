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
    # 트레이 아이콘. 상주 프로그램인데 살아 있는지 보이지 않으면 사용자는 껐는지도 모른다.
    # 알림(풍선)도 이 아이콘을 통해 나가므로, 끄면 알림 경로가 함께 사라진다.
    tray: bool = True

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
    wake_granularity_s: float = Field(default=5.0, gt=0)

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
    top_handle_growth: int = Field(default=10, ge=0)
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
    gpu_recover_after_failures: int = Field(default=5, ge=1)
    gpu_recover_backoff_s: float = Field(default=60.0, gt=0)
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
    # 결함 주입 구간 앞뒤로 지키는 여유. 채점이 보는 창이 주입 구간보다 넓다 —
    # 비교 창(주입 150초 전부터)과 선행성 조회(±300초)가 밖으로 나간다.
    fault_guard_s: float = Field(default=900.0, ge=0)


class ThermalDriftSettings(BaseModel):
    """냉각 열화 — 같은 부하에서 예전보다 뜨거운가.

    절대 온도 문턱이 없다는 것이 요점이다(규칙 2). 노트북(평소 87도)이든 데스크탑
    (평소 70도)이든 자기 과거와만 비교하므로 같은 값이 그대로 맞는다.
    """

    enabled: bool = True
    # 판정 주기. 하루 단위 비교라 자주 볼 이유가 없고, 웜(Parquet)까지 훑는 작업이다.
    interval_s: float = Field(default=21600.0, gt=0)

    # **부하를 맞춰 비교한다.** 유휴를 섞으면 "요즘 게임을 많이 했다"가 "냉각이
    # 나빠졌다"로 둔갑한다. 이 사용률 이상인 1분 버킷만 본다.
    min_gpu_util: float = Field(default=80.0, gt=0, le=100)
    # 그런 버킷이 하루에 이만큼 없으면 그날은 표본에서 뺀다. 5분짜리 부하의 중앙값은
    # 중앙값이 아니라 우연이다.
    min_busy_minutes: int = Field(default=20, ge=1)

    # 비교 구간. 최근 N일 대 그 이전 M일.
    recent_days: int = Field(default=3, ge=1)
    baseline_days: int = Field(default=14, ge=1)
    # 표본이 이만큼 모이기 전에는 판정하지 않는다 — 부트스트랩 기간(탐지 규칙 4).
    min_days: int = Field(default=7, ge=2)

    # 이만큼 올랐을 때 알린다. 실측에서 이 PC 의 부하 시 온도는 6일간 변동이 0 이었다
    # (93.0도 고정). 산포가 거의 없는 지표라 3도면 뚜렷한 변화다.
    rise_c: float = Field(default=3.0, gt=0)


class BottleneckSettings(BaseModel):
    """병목 분류의 판정 문턱. "무엇에 막혔나"를 가르는 값들이다.

    **점수 가중치(0.5·0.3·0.7 …)는 여기 없다.** 그것은 튜닝 값이 아니라 분류 알고리즘의
    구조다 — YAML 로 노출하면 사용자가 병목 판정을 망가뜨릴 수 있고 되돌릴 방법이 없다.
    규칙 3 이 요구하는 것은 "임계값을 코드 수정 없이 튜닝"이지 알고리즘 노출이 아니다.

    기본값은 이관 이전의 코드 상수와 같다. 옮기는 것이 목적이지 값을 바꾸는 것이
    아니다 — `disk_resp_floor_ms` 하나만 예외이고, 그 이유는 아래 주석에 있다.
    """

    # 상대 판정의 문턱. 이 배수를 넘어야 "평소와 다르다"고 본다.
    z_high: float = Field(default=3.0, gt=0)

    # **디스크 응답의 체감 하한.** `rules.yaml` 의 디스크 룰이 같은 개념을 `3` 으로
    # 써서 **한 개념에 두 값**이었다. 5.0 으로 통일한다 — 3ms 는 NVMe 에서 흔한
    # 값이라 룰 쪽이 더 자주 울렸고, 둘 중 엄격한 쪽을 남기는 것이 오탐을 줄인다.
    disk_resp_floor_ms: float = Field(default=5.0, gt=0)

    cpu_high_percent: float = Field(default=70.0, gt=0)
    """이 이상이면 절대값만으로 CPU 병목 근거가 된다."""
    cpu_elevated_percent: float = Field(default=40.0, gt=0)
    """절대값은 낮지만 평소 대비 크게 높을 때 요구하는 최소선."""

    mem_high_percent: float = Field(default=85.0, gt=0)
    """이 이상이면 메모리 압박이 확실하다."""
    mem_tight_percent: float = Field(default=60.0, gt=0)
    """메모리가 '빠듯하다'고 볼 선. 스왑·상대 조건이 이 위에서만 근거가 된다.

    **스왑 단독으로는 근거가 되지 않는다**(2026-08-02, PLAN §8 18번). Windows 는
    메모리가 남아돌아도 페이지파일을 쓴다.
    """

    disk_queue_high: float = Field(default=2.0, gt=0)
    """응답시간 근거가 이미 있을 때 큐가 이 이상이면 근거를 더한다."""
    disk_queue_alone: float = Field(default=4.0, gt=0)
    """큐 길이는 단위가 하드웨어 독립적이라 이 값 이상이면 혼자서도 근거가 된다."""

    gpu_busy_percent: float = Field(default=90.0, gt=0)
    gpu_hot_c: float = Field(default=80.0, gt=0)
    cpu_perf_low_percent: float = Field(default=80.0, gt=0)
    """실효 클럭이 이 아래로 떨어지면 스로틀링을 의심한다."""

    contention_cpu_ceiling_percent: float = Field(default=60.0, gt=0)
    """경합은 "자원은 남는데 지연"이다. CPU 가 이 위면 경합이 아니라 CPU 병목이다."""

    # 방아쇠가 지목한 자원을 지표 근거가 뒤집으려면 이만큼 더 강해야 한다.
    # 근소한 우위는 뒤집을 근거가 못 된다 — `bottleneck._OVERRIDE_*` 주석 참조.
    override_ratio: float = Field(default=1.5, gt=1.0)
    override_margin: float = Field(default=0.3, ge=0)


class IncidentSettings(BaseModel):
    """사건을 닫고 설명할 때의 문턱. 분류가 아니라 서술에 관한 값들이다."""

    max_extend_before_s: float = Field(default=300.0, gt=0)
    """지표에서 추정한 시작 경계를 앞으로 늘릴 수 있는 최대. 다른 사건을 삼키지 않게 한다."""
    max_extend_after_s: float = Field(default=600.0, gt=0)

    lead_min_share: float = Field(default=0.10, ge=0, le=1)
    """기여도가 이 아래인 후보에는 선행 시간을 붙이지 않는다 — 잡음이라 오해를 부른다."""
    lead_resolution_s: float = Field(default=30.0, gt=0)
    """이 안쪽 차이는 "거의 동시"로 쓴다. 샘플링 주기보다 촘촘하게 말하지 않는다."""

    stock_drop_reset_ratio: float = Field(default=0.5, gt=0, lt=1)
    """저량 지표가 최고치의 이 비율 아래로 떨어지면 PID 재사용으로 보고 시계열을 끊는다."""


class DetectionSettings(BaseModel):
    """탐지 엔진. 임계값은 여기가 아니라 `rules.yaml` 에 있다 — 이건 엔진 설정이다."""

    enabled: bool = True
    # 쉼표로 여러 개를 적으면 함께 돈다. 관측은 한 번만 읽어 공유하므로 탐지기를
    # 늘려도 DB 읽기는 그대로다. `rules` 는 시스템 전역 메트릭을, `procleak` 은
    # 프로세스별 지표를 본다 — 서로 대체재가 아니라 보는 영역이 다르다.
    detector: str = "rules,procleak"
    # 저장된 관측을 따라 읽는 주기. 룰의 지속 조건이 30초 이상이라 촘촘할 필요가 없다.
    interval_s: float = Field(default=10.0, gt=0)
    # "평소"를 계산할 창. 길수록 안정적이지만 최근 변화에 둔해진다.
    baseline_window_s: float = Field(default=1800.0, gt=0)
    # 이만큼 표본이 모여야 판정한다. 표본 3개의 중앙값은 중앙값이 아니라 우연이다.
    min_samples: int = Field(default=60, ge=2)
    # 알림 발송. Phase 9 까지는 꺼 둔다 — 오탐률이 검증되기 전에 알리면 되돌릴 수 없다.
    # 알림 발송. 판정(`notified`)과는 별개다 — 판정은 항상 돌아 대시보드와 채점이 쓰고,
    # 이 값은 사용자에게 실제로 띄울지만 정한다. 2026-08-03 에 켰다(근거는 defaults.yaml).
    notify: bool = True


class FingerprintSettings(BaseModel):
    """프로세스 지문(Phase 6-B). "이 프로그램의 평소는 어디까지인가"를 학습한다.

    `procleak` 의 오탐을 줄이는 데 쓴다. 억제는 한쪽으로만 작동한다 — 지문이 없으면
    아무것도 막지 않는다.
    """

    enabled: bool = True
    # 3일 이상 관측된 것만 지문이 되므로 한 시간 사이에 결과가 달라질 일이 없고,
    # 웜(Parquet)까지 훑는 작업이라 싸지도 않다.
    interval_s: float = Field(default=21600.0, gt=0)
    # 자격 조건. 3일에 걸쳐 보였어도 매번 5분씩이면 p99 를 세울 표본이 못 된다.
    min_days: int = Field(default=3, ge=1)
    min_buckets: int = Field(default=100, ge=10)
    # 하루로 세는 최소 관측 시간. 값의 근거는 `fingerprint.MIN_DAY_HOURS` 주석.
    min_day_hours: float = Field(default=1.0, ge=0)


class SeveritySettings(BaseModel):
    """등급을 가르는 문턱. **두 축(현재 손실 · 방치 시 위험)의 경계다.**

    **이 값들은 2026-08-03 에 실사용 122건으로 정했다** — 근거는 `CHANGELOG.md`.
    근거 없이 넣으면 07-30 에 배수 문턱 5 를 넣지 않고 멈춘 것과 같은 자리가 된다.
    """

    enabled: bool = True

    # --- 위험 축: 그 프로그램 자기 p99 대비 위치
    # 실측: medal(정상 동작) 0.12, 주입 python(진짜 누수) 2.16. 둘 사이가 넓어
    # 문턱을 어디에 둬도 갈리지만, 1.0(= 평소 상한)이 뜻이 분명하다.
    risk_warning_ratio: float = Field(default=1.0, gt=0)
    risk_critical_ratio: float = Field(default=2.0, gt=0)
    # 이보다 등락하면 한 단계 내린다. 계단식으로만 오르는 것이 누수의 모양이다.
    risk_monotonic_floor: float = Field(default=0.95, ge=0.0, le=1.0)
    # 지문이 없는 프로세스의 등급. 모르는 것을 조용히 info 로 내리면 진짜 누수가 묻히고,
    # critical 로 올리면 신규 프로세스마다 운다. 지금까지의 고정값이 warning 이었다.
    unknown_leak: str = "warning"

    # --- 현재 손실 축: 같은 부하에서 클럭이 얼마나 깎였나
    impact_warning_loss: float = Field(default=0.10, gt=0, lt=1)
    impact_critical_loss: float = Field(default=0.25, gt=0, lt=1)


class LeakMetricSettings(BaseModel):
    """지표 하나의 누수 판정 기준. 전부 상대값이다.

    `min_delta` 만 절대값인데, 핸들 수는 RAM 이나 코어 수에 비례하지 않으므로 그게 맞다.
    RSS 는 비례하므로 `min_delta_ram_ratio` 로 이 PC 의 RAM 비율로 환산한다.
    """

    growth_ratio: float = Field(default=3.0, gt=1.0)
    min_delta: float = Field(default=500.0, ge=0)
    monotonic_ratio: float = Field(default=0.85, ge=0.0, le=1.0)
    # 지정하면 min_delta 를 `RAM(MB) × 이 비율` 로 대체한다 (하드웨어를 가정하지 않기 위해).
    min_delta_ram_ratio: float | None = Field(default=None, ge=0)


class ProcessLeakSettings(BaseModel):
    """프로세스 누수 탐지(Phase 6-A). 시스템 룰로는 닿지 않는 프로세스별 지표를 본다."""

    enabled: bool = True
    window_s: float = Field(default=900.0, gt=0)
    # 이만큼 계속 자라야 발화한다. 프로그램을 켤 때의 급증은 누수가 아니다.
    min_duration_s: float = Field(default=300.0, gt=0)
    min_samples: int = Field(default=20, ge=2)
    # 같은 프로세스·지표의 재발화 억제. 누수는 고칠 때까지 계속되므로 이게 없으면
    # 한 번 새는 프로그램이 알림을 영원히 반복한다.
    cooldown_s: float = Field(default=1800.0, ge=0)
    # 동시 추적 상한. **프로세스 수가 아니라 트랙 수다(프로세스 × 지표).**
    # 상한에 닿으면 가장 오래된 트랙을 버리고 새 것을 받는다.
    max_tracked: int = Field(default=800, ge=1)
    # 창 최대치의 이 비율 아래로 떨어지면 추적을 리셋한다(PID 재사용·정상 해제).
    drop_reset_ratio: float = Field(default=0.5, gt=0, lt=1.0)
    # 판정·정리 주기. 누수는 분 단위 현상이라 초당 판정할 이유가 없고, 판정 한 번이
    # 트랙 수백 개의 중앙값 계산이라 매 틱 돌리면 관측자가 병목이 된다.
    eval_interval_s: float = Field(default=30.0, gt=0)
    handles: LeakMetricSettings = LeakMetricSettings()
    rss_mb: LeakMetricSettings = LeakMetricSettings(
        growth_ratio=3.0, min_delta=512.0, monotonic_ratio=0.9, min_delta_ram_ratio=0.02
    )


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
    bottleneck: BottleneckSettings = BottleneckSettings()
    thermal_drift: ThermalDriftSettings = ThermalDriftSettings()
    incident: IncidentSettings = IncidentSettings()
    process_leak: ProcessLeakSettings = ProcessLeakSettings()
    severity: SeveritySettings = SeveritySettings()
    fingerprint: FingerprintSettings = FingerprintSettings()


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

"""병목 분류 — 무엇에 막혔나.

"CPU 가 높다"와 "CPU 에 막혔다"는 다르다. 디스크를 기다리느라 CPU 가 노는 동안에도
사용자는 느리다고 느낀다. 무엇을 늘려야 빨라지는지 답하려면 자원 종류를 특정해야 한다.

**증상과 원인을 함께 본다.** 사용률(원인)만으로 판정하면 NVMe 에 600MB/s 를 퍼부어도
응답이 0.2ms 인 경우를 "디스크 병목"이라 부르게 된다. 실제로 Phase 2·3 에서 그 일이
있었고, 그래서 이 PC 의 `disk_thrash` 시나리오는 증상 없음으로 채점에서 빠진다.
사용자가 느끼지 못하는 것은 병목이 아니다.

판정은 규칙이다. 학습 모델을 쓰지 않는 이유는 라벨이 없기 때문이다 — "이 구간은
IO 병목이었다"고 말해 줄 사람이 없다. 규칙으로 시작해 결함 주입으로 검증하고,
규칙이 못 가르는 경우가 실제로 쌓이면 그때 학습으로 옮긴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..detection.baseline import BaselineSet
from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Bottleneck:
    """병목 판정 결과."""

    kind: str
    """CPU / IO / MEMORY / GPU / THERMAL / CONTENTION / NONE"""
    confidence: float
    """0~1. 근거가 여럿 겹칠수록 높다."""
    evidence: list[str]
    resource: str
    """기여도 분해에 쓸 자원 이름(`attribution.RESOURCE_COLUMNS` 의 키)."""

    attributable: bool = True
    """`resource` 가 이 병목의 자원과 실제로 같은가.

    GPU·THERMAL 은 프로세스별 GPU 사용량을 얻을 방법이 없어 CPU 로 대신 분해한다
    (Phase 12 전까지). 그 결과를 **원인이라고 말하면 안 된다.** 실측에서
    "발열 스로틀링 — svchost 19%" 가 나왔는데, svchost 는 CPU 를 2% 썼을 뿐이고
    GPU 를 태운 것은 게임이었다. 모르는 것을 아는 척하는 답이 가장 나쁘다.
    """

    trigger_kinds: tuple[str, ...] = ()
    """이 사건을 연 룰이 지목한 자원들. 비어 있으면 방아쇠 정보 없이 판정한 것이다."""

    overridden_from: str | None = None
    """방아쇠가 지목한 자원을 지표 근거가 뒤집었을 때, 그 원래 자원.

    뒤집는 것 자체는 정당할 수 있다(메모리 룰이 열었지만 실제로는 디스크가 막힌 경우).
    다만 **말없이 뒤집으면 안 된다** — 사용자는 GPU 온도 알림을 기다렸는데 CPU 이야기를
    듣게 된다. 뒤집었으면 리포트가 그 사실을 밝힌다.
    """

    @property
    def label(self) -> str:
        return label_for(self.kind)


_LABELS = {
    "CPU": "CPU 병목",
    "IO": "디스크 IO 병목",
    "MEMORY": "메모리 압박",
    "GPU": "GPU 병목",
    "THERMAL": "발열 스로틀링",
    "CONTENTION": "경합 (자원 포화 없이 지연)",
    "NONE": "병목 없음",
}


def label_for(kind: str) -> str:
    return _LABELS.get(kind, kind)


def _z(baselines: BaselineSet, metric: str, value: float | None) -> float | None:
    stats = baselines.stats(metric)
    return stats.z(value) if stats else None


# 디스크 응답시간의 "체감 하한". 이 아래면 어떤 하드웨어에서도 사용자가 느끼지 못한다.
#
# **상대 조건만으로는 안 된다.** NVMe 의 평소 응답은 0.1ms 라 산포가 거의 0 이고,
# 0.1 → 0.2ms 가 20σ 로 계산된다. 그걸 병목이라 부르면 아무도 느끼지 못한 일에
# 알림을 보내게 된다(실측에서 실제로 그렇게 판정됐다).
#
# 반대로 절대값만 쓰면 HDD 사용자(평소 10ms)에게 상시 오탐이 된다. 그래서 **둘 다**
# 요구한다 — 평소와 다르고(상대), 실제로 아플 만큼 느리다(절대).
DISK_RESP_FLOOR_MS = 5.0


# 병목 종류 → (기여도 분해에 쓸 자원, 그 자원이 병목의 자원과 같은가)
#
# GPU·THERMAL 이 CPU 로 내려가는 것은 **대체지 답이 아니다.** NVML 의 per-process
# 사용량은 지원이 들쭉날쭉해 Phase 12 전까지 프로세스별 GPU 를 알 수 없다.
# 그 사실을 값에 실어 보내야 리포트가 거짓말을 하지 않는다.
_RESOURCE_BY_KIND: dict[str, tuple[str, bool]] = {
    "CPU": ("cpu", True),
    "IO": ("io_write", True),
    "MEMORY": ("rss", True),
    "GPU": ("cpu", False),
    "THERMAL": ("cpu", False),
    # 경합은 정의상 "누가 자원을 많이 썼나"로 설명되지 않는다. CPU 상위는 참고일 뿐이다.
    "CONTENTION": ("cpu", False),
    # **`NONE` 은 "전역 지표에서 아무것도 못 찾았다"는 뜻이다.** 그 상태에서 CPU 로
    # 분해한 1위를 원인이라고 확신하면, 구간에 CPU 를 많이 쓴 무관한 프로세스가 범인이
    # 된다. 2026-07-30 에 핸들 누수 4건이 전부 그렇게 틀린 프로세스를 발표했다
    # (`attributable=True` 였다). GPU·THERMAL 과 같은 이유로 `False` 가 맞다.
    #
    # 탐지기가 자기 자원을 말해 준 경우에는 `fusion.close_incident` 가 그 자원으로
    # 바꾸면서 `attributable` 도 되살린다 — 그때는 근거가 있다.
    "NONE": ("cpu", False),
}


# 룰이 참조한 지표 → 그 지표가 지목하는 병목 종류.
#
# **룰 이름이 아니라 지표로 매핑한다.** 룰 이름은 사용자가 `%APPDATA%\Argus\rules.yaml`
# 에서 자유롭게 바꿀 수 있어서, "GPU 고온 지속" 같은 문자열을 코드에 박으면 사용자 룰에서
# 조용히 깨진다. 지표 이름은 룰 정의에서 그대로 따라오고 로케일·작명에 독립적이다.
#
# 키는 룰이 쓰는 이름(`rules.yaml`)과 원본 테이블 컬럼명을 **둘 다** 받는다. 신호의
# evidence 는 전자를, 스냅샷은 후자를 쓰는데 어느 쪽이 들어와도 같은 답이 나와야 한다.
_KIND_BY_METRIC: dict[str, str] = {
    "cpu_total": "CPU",
    "cpu_max_core": "CPU",
    "mem_percent": "MEMORY",
    "mem_avail_mb": "MEMORY",
    "swap_used_mb": "MEMORY",
    "disk_resp_ms": "IO",
    "disk_queue": "IO",
    "ctx_switches_ps": "CONTENTION",
    "gpu_util": "GPU",
    "gpu_temp_c": "THERMAL",
    "gpu_temp": "THERMAL",
    "gpu_throttle_reasons": "THERMAL",
    "gpu_throttle_reason": "THERMAL",
    "cpu_perf_percent": "THERMAL",
}


# 방아쇠가 지목한 자원을 뒤집으려면 다른 자원의 근거가 이만큼 더 강해야 한다.
#
# 실측에서 GPU 온도 룰이 연 사건이 "CPU 병목 — deltaforce 53%" 로 나갔다. 게임 중에는
# CPU 가 늘 70% 를 넘어 CPU 점수(0.8)가 THERMAL(0.7)을 상시 근소하게 앞선다. **근소한
# 우위는 뒤집을 근거가 못 된다** — 방아쇠는 "평소와 다르다"를 확인하고 울린 것이고,
# 병목 점수는 "지금 값이 높다"만 본다. 후자가 전자를 이기려면 큰 차이가 필요하다.
_OVERRIDE_RATIO = 1.5
_OVERRIDE_MARGIN = 0.3


def kinds_for_metrics(metrics: Iterable[str]) -> tuple[str, ...]:
    """지표 이름들이 지목하는 병목 종류. 순서를 유지하고 중복을 없앤다."""
    kinds: list[str] = []
    for metric in metrics:
        kind = _KIND_BY_METRIC.get(metric)
        if kind and kind not in kinds:
            kinds.append(kind)
    return tuple(kinds)


def classify(
    metrics: Mapping[str, float | None],
    baselines: BaselineSet,
    *,
    z_high: float = 3.0,
    disk_resp_floor_ms: float = DISK_RESP_FLOOR_MS,
    trigger_metrics: Iterable[str] = (),
) -> Bottleneck:
    """한 시점의 지표로 병목을 판정한다.

    절대 임계값을 최소한으로만 쓴다. `disk_queue`·`cpu_perf_percent` 처럼 단위 자체가
    하드웨어에 독립적인 지표만 절대값으로 다루고, 나머지는 이 PC 의 평소 대비로 본다.

    `trigger_metrics` 는 이 사건을 연 룰이 참조한 지표들이다. 주어지면 **그것이 지목한
    자원을 우선**한다. 지표만 보고 판정하면 게임 중처럼 여러 자원이 동시에 높은 구간에서
    늘 CPU 가 이겨, GPU 온도로 열린 사건이 "CPU 병목" 으로 보고된다(실측 3/6).
    """
    # 근거는 **판정별로** 모은다. 한 리스트에 섞으면 "CPU 병목 — 근거: 스왑 220MB"
    # 처럼 판정과 무관한 근거가 붙어, 읽는 사람이 판단을 검증할 수 없게 된다.
    evidence_by_kind: dict[str, list[str]] = {}
    scores: dict[str, float] = {}

    def add(kind: str, weight: float, why: str) -> None:
        scores[kind] = scores.get(kind, 0.0) + weight
        evidence_by_kind.setdefault(kind, []).append(why)

    cpu = metrics.get("cpu_total")
    cpu_z = _z(baselines, "cpu_total", cpu)
    resp = metrics.get("disk_resp_ms")
    resp_z = _z(baselines, "disk_resp_ms", resp)
    queue = metrics.get("disk_queue")
    mem = metrics.get("mem_percent")
    mem_z = _z(baselines, "mem_percent", mem)
    swap = metrics.get("swap_used_mb")
    gpu = metrics.get("gpu_util")
    perf = metrics.get("cpu_perf_percent")
    gpu_temp = metrics.get("gpu_temp")
    throttle = str(metrics.get("gpu_throttle_reason") or "")
    ctx = metrics.get("ctx_switches_ps")
    ctx_z = _z(baselines, "ctx_switches_ps", ctx)

    # CPU: 평소보다 크게 높고, 절대적으로도 여유가 없어야 한다.
    if cpu is not None and cpu >= 70.0:
        add("CPU", 0.5, f"CPU {cpu:.0f}%")
        if cpu_z is not None and cpu_z >= z_high:
            add("CPU", 0.3, f"평소의 {cpu_z:.1f}σ")
    elif cpu_z is not None and cpu_z >= z_high and (cpu or 0) >= 40.0:
        add("CPU", 0.4, f"CPU {cpu:.0f}% (평소 {cpu_z:.1f}σ)")

    # IO: **응답시간이 증상이다.** 처리량만 높은 것은 병목이 아니고,
    # 평소보다 높기만 한 것도 아니다(0.1 → 0.2ms 는 20σ 지만 아무도 못 느낀다).
    if (
        resp is not None
        and resp_z is not None
        and resp_z >= z_high
        and resp >= disk_resp_floor_ms
    ):
        add("IO", 0.5, f"디스크 응답 {resp:.1f}ms (평소의 {resp_z:.1f}σ)")
        if queue is not None and queue >= 2.0:
            add("IO", 0.3, f"큐 깊이 {queue:.1f}")
    elif queue is not None and queue >= 4.0:
        # 큐 길이는 단위가 하드웨어 독립적이라 절대값으로 다뤄도 된다.
        add("IO", 0.4, f"큐 깊이 {queue:.1f}")

    # 메모리: 여유가 실제로 없을 때만. 스왑은 **혼자서는 근거가 되지 못한다.**
    if mem is not None and mem >= 85.0:
        add("MEMORY", 0.5, f"메모리 {mem:.0f}%")

    # **스왑 사용량은 메모리 압박의 신호가 아니다 — Windows 에서는 상시 값이다.**
    #
    # 원래 이 줄은 `if swap:` 이었고, 스왑이 0보다 크기만 하면 0.3점을 줬다. 다른 병목이
    # 없으면 그 0.3 이 최고점이 되어 병목이 `MEMORY` 로 확정된다. 이 기계 실측(64,866
    # 표본)에서 `swap_used_mb` 는 중앙값 448MB 이고 0 인 표본이 9.2% 뿐이라, **시간의
    # 90.8% 동안 메모리 병목 점수가 붙어 있었다.** 메모리는 32% 밖에 안 쓰는데 그렇다.
    #
    # 2026-08-02 주입 배치 8건이 전부 이것 때문에 `메모리 압박 — chrome 70%` 처럼
    # 발표됐고(주입과 무관한 프로세스), 제품 경로 귀인이 12.5% 로 떨어졌다. 그 압박은
    # 실재하지 않았다 — 주입 구간 안팎의 `mem_percent` 가 똑같다(중앙값 27.8 vs 27.8).
    #
    # **z 로 바꾸는 것으로는 부족하다.** 스왑은 값이 거의 일정해 산포가 작고, 448 → 449
    # 가 큰 z 로 계산된다 — 디스크 응답의 `0.1 → 0.2ms = 20σ` 와 같은 함정이다.
    #
    # 그래서 **메모리가 실제로 빠듯할 때만** 스왑을 근거로 센다. 스왑이 압박의 신호이려면
    # "메모리가 부족해서" 페이지아웃이 일어나야 하는데, Windows 의 페이지파일 사용량은
    # 압박과 무관하게 존재한다. 진짜 신호는 페이지폴트 **속도**지 사용량이 아니고, 그건
    # 지금 수집하지 않는다. 수집하게 되면 이 조건을 그쪽으로 옮긴다.
    if swap and (mem or 0) >= 60.0:
        add("MEMORY", 0.3, f"스왑 {swap:.0f}MB")
    if mem_z is not None and mem_z >= z_high and (mem or 0) >= 60.0:
        add("MEMORY", 0.3, f"메모리 평소의 {mem_z:.1f}σ")

    if gpu is not None and gpu >= 90.0:
        add("GPU", 0.6, f"GPU {gpu:.0f}%")

    # 발열: 클럭이 떨어졌는데 온도가 높거나 스로틀 사유가 잡힌 경우.
    if "THERMAL" in throttle.upper():
        # 온도를 함께 적는다. 이 근거가 제목으로도 쓰이는데(귀인이 불가능해 프로세스를
        # 넣을 수 없다), "스로틀 사유에 THERMAL" 만으로는 얼마나 나쁜지 알 수 없다.
        detail = f"GPU {gpu_temp:.0f}°C 열 스로틀링" if gpu_temp is not None else "GPU 열 스로틀링"
        add("THERMAL", 0.7, detail)
    if perf is not None and perf < 80.0:
        add("THERMAL", 0.3, f"실효 클럭 {perf:.0f}%")
        if gpu_temp is not None and gpu_temp >= 80.0:
            add("THERMAL", 0.2, f"GPU {gpu_temp:.0f}°C")

    # 경합: 자원은 남는데 컨텍스트 스위치만 폭증.
    if ctx_z is not None and ctx_z >= z_high and (cpu or 0) < 60.0:
        add("CONTENTION", 0.5, f"컨텍스트 스위치 평소의 {ctx_z:.1f}σ")

    trigger_kinds = kinds_for_metrics(trigger_metrics)

    if not scores:
        # 자원·귀인 가능 여부를 여기서 따로 쓰지 않는다 — 표와 어긋나면 조용히 갈린다.
        # (2026-07-30: 표만 고쳤더니 이 줄이 dataclass 기본값 `attributable=True` 를
        # 그대로 써서 "병목 없음 — cpu_eater 100%" 가 계속 나왔다.)
        none_resource, none_attributable = _RESOURCE_BY_KIND["NONE"]
        return Bottleneck(
            "NONE", 0.0, [], none_resource, none_attributable, trigger_kinds=trigger_kinds
        )

    top = max(scores.items(), key=lambda kv: kv[1])[0]
    kind, overridden_from = _choose(scores, top, trigger_kinds)

    evidence = evidence_by_kind.get(kind, [])
    resource, attributable = _RESOURCE_BY_KIND[kind]
    return Bottleneck(
        kind,
        min(1.0, scores[kind]),
        evidence,
        resource,
        attributable,
        trigger_kinds=trigger_kinds,
        overridden_from=overridden_from,
    )


def _choose(
    scores: Mapping[str, float], top: str, trigger_kinds: tuple[str, ...]
) -> tuple[str, str | None]:
    """방아쇠와 지표 점수를 조정해 최종 병목을 고른다.

    돌려주는 둘째 값은 "방아쇠를 뒤집었다면 그 원래 자원"이다.
    """
    if not trigger_kinds:
        return top, None

    candidates = [(kind, scores[kind]) for kind in trigger_kinds if kind in scores]
    if not candidates:
        # 방아쇠가 지목한 자원에서 이 구간의 지표 근거가 하나도 잡히지 않았다. 판정을
        # 강요할 재료가 없으니 지표를 따르되, **다른 답을 냈다는 사실은 남긴다** —
        # 사용자가 기다린 것은 방아쇠 쪽 이야기다. (평탄한 지표는 σ가 0 이라 z 가 서지
        # 않아 점수가 비는데, 실제로 흔하다.)
        return top, trigger_kinds[0]

    best_trigger = max(candidates, key=lambda kv: kv[1])[0]
    if best_trigger == top:
        return top, None

    # 다른 자원이 압도적일 때만 뒤집는다. 압도의 기준은 배수와 절대차를 **둘 다** 넘는 것 —
    # 배수만 보면 0.1 대 0.2 같은 작은 값에서 쉽게 뒤집히고, 절대차만 보면 근거가 약한
    # 구간에서 큰 차이가 나오지 않아 영영 뒤집히지 않는다.
    if (
        scores[top] >= scores[best_trigger] * _OVERRIDE_RATIO
        and scores[top] - scores[best_trigger] >= _OVERRIDE_MARGIN
    ):
        return top, best_trigger
    return best_trigger, None

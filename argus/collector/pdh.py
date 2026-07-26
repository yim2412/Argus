"""Windows 성능 카운터(PDH).

**왜 psutil 로 부족한가**: psutil 은 "얼마나 썼는가"(사용률·처리량)를 준다. 하지만
느림의 증상은 "얼마나 기다렸는가"(응답시간·큐 길이)다. 디스크 사용률이 30% 여도
응답시간이 70ms 면 시스템은 버벅인다. 그 증상 지표가 PDH 에만 있다.

**로케일 함정과 그 해법**

카운터 경로는 `\\LogicalDisk(_Total)\\Avg. Disk sec/Transfer` 처럼 이름으로 쓰는데,
이 이름은 OS 언어별로 번역되어 있다. 영문 문자열을 하드코딩하면 한국어 Windows 에서
전부 실패한다. 그래서 보통 숫자 인덱스를 쓰라고 하는데, **인덱스를 상수로 박는 것도
위험하다** — 실제로 이 프로젝트에서 "Context Switches/sec = 146", "Avg. Disk
sec/Transfer = 208" 로 알려진 값을 넣었더니 각각 전혀 다른 카운터(14340, 206)였고,
그런데도 그럴듯한 숫자가 나와서 눈으로는 알아챌 수 없었다.

해법은 두 단계다.
  1. 레지스트리의 **영문(009) 카운터 목록**에서 이름 → 인덱스를 역조회한다.
     009 는 OS 언어와 무관하게 항상 영문으로 존재한다.
  2. 그 인덱스를 `LookupPerfNameByIndex` 에 넣어 **이 OS 의 지역화된 이름**을 얻고
     경로를 조립한다.

이러면 인덱스를 추측하지 않으면서 로케일 독립성도 유지된다.

카운터 하나가 실패해도 나머지는 계속 쓴다. OS 버전·하드웨어에 따라 없는 카운터가 있다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

try:  # pywin32 가 없거나 Windows 가 아니면 이 모듈 전체가 비활성화된다
    import winreg

    import win32pdh

    _HAVE_PDH = sys.platform == "win32"
except ImportError:  # pragma: no cover - 플랫폼 의존
    win32pdh = None  # type: ignore[assignment]
    winreg = None  # type: ignore[assignment]
    _HAVE_PDH = False

_PERFLIB_009 = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Perflib\009"

_english_index_cache: dict[str, int] | None = None


def english_counter_indices() -> dict[str, int]:
    """영문 카운터 이름 → 인덱스. 레지스트리 009 키에서 읽는다(결과 캐시).

    값은 REG_MULTI_SZ 이고 `[인덱스, 이름, 인덱스, 이름, ...]` 로 번갈아 들어 있다.
    """
    global _english_index_cache
    if _english_index_cache is not None:
        return _english_index_cache

    mapping: dict[str, int] = {}
    if _HAVE_PDH:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _PERFLIB_009) as key:
                data, _ = winreg.QueryValueEx(key, "Counter")
            for i in range(0, len(data) - 1, 2):
                try:
                    mapping[data[i + 1]] = int(data[i])
                except (ValueError, TypeError):
                    continue
        except OSError as e:
            log.warning("영문 카운터 목록을 읽지 못했다 — 영문 이름으로 직접 시도한다",
                        extra={"error": str(e)})

    _english_index_cache = mapping
    return mapping


@dataclass
class CounterSpec:
    """수집할 카운터. 인덱스가 아니라 **영문 이름**으로 지정한다."""

    key: str
    object_name: str
    counter_name: str
    instance: str | None = None
    scale: float = 1.0  # 저장 단위로 변환 (예: 초 → 밀리초)
    note: str = ""


DEFAULT_COUNTERS: list[CounterSpec] = [
    CounterSpec(
        "ctx_switches_ps", "System", "Context Switches/sec",
        note="경합 신호. 리소스는 여유로운데 느린 경우의 단서",
    ),
    CounterSpec(
        "disk_queue", "LogicalDisk", "Current Disk Queue Length", instance="_Total",
        note="대기 중인 디스크 요청 수",
    ),
    CounterSpec(
        "disk_resp_ms", "LogicalDisk", "Avg. Disk sec/Transfer", instance="_Total",
        scale=1000.0,
        note="디스크 응답시간. 사용률보다 훨씬 중요한 '증상' 지표",
    ),
    CounterSpec(
        "cpu_perf_percent", "Processor Information", "% Processor Performance",
        instance="_Total",
        note="공칭 클럭 대비 실제 성능. 100 미만이면 스로틀링·절전 상태",
    ),
    # 프로세스/스레드 총수. psutil 로 세면 전체 순회가 필요하지만 PDH 는 이미 열어둔
    # System 객체에서 공짜로 나온다.
    CounterSpec("proc_count", "System", "Processes"),
    CounterSpec("thread_count", "System", "Threads"),
]


class PdhCounters:
    """PDH 질의 하나에 여러 카운터를 달아 함께 읽는다."""

    def __init__(self, specs: list[CounterSpec] | None = None) -> None:
        self.specs = specs if specs is not None else list(DEFAULT_COUNTERS)
        self._query: Any = None
        self._handles: dict[str, Any] = {}
        self._scales: dict[str, float] = {}
        self._failures: dict[str, str] = {}
        self._resolved: dict[str, str] = field(default_factory=dict)  # type: ignore[assignment]
        self._resolved = {}
        self._primed = False

    # ------------------------------------------------------------------ 준비

    @property
    def available(self) -> bool:
        return bool(self._handles)

    @property
    def failures(self) -> dict[str, str]:
        """실패한 카운터와 이유. 조용히 사라지지 않게 밖에서 볼 수 있어야 한다."""
        return dict(self._failures)

    @property
    def resolved_paths(self) -> dict[str, str]:
        """실제로 사용 중인 (지역화된) 카운터 경로. 진단용."""
        return dict(self._resolved)

    def _localized_name(self, english: str) -> str:
        """영문 이름 → 이 OS 의 지역화된 이름.

        레지스트리에서 인덱스를 못 찾으면 영문 이름을 그대로 쓴다(영문 Windows 대응).
        """
        index = english_counter_indices().get(english)
        if index is None:
            return english
        try:
            return win32pdh.LookupPerfNameByIndex(None, index)
        except Exception:
            return english

    def open(self) -> "PdhCounters":
        if not _HAVE_PDH:
            self._failures["_module"] = "pywin32 없음 또는 Windows 아님"
            return self

        try:
            self._query = win32pdh.OpenQuery()
        except Exception as e:
            self._failures["_query"] = f"OpenQuery 실패: {e}"
            log.warning("PDH 질의를 열 수 없다", extra={"error": str(e)})
            return self

        for spec in self.specs:
            try:
                obj = self._localized_name(spec.object_name)
                counter = self._localized_name(spec.counter_name)
                path = win32pdh.MakeCounterPath((None, obj, spec.instance, None, -1, counter))
                handle = win32pdh.AddCounter(self._query, path)
            except Exception as e:
                # 이 OS 버전·하드웨어에 없는 카운터일 수 있다. 그것만 빼고 계속 간다.
                self._failures[spec.key] = str(e)
                log.info(
                    "PDH 카운터를 쓸 수 없다 — 이 항목만 비활성화",
                    extra={"key": spec.key, "counter": spec.counter_name, "error": str(e)},
                )
                continue
            self._handles[spec.key] = handle
            self._scales[spec.key] = spec.scale
            self._resolved[spec.key] = path

        if self._handles:
            # 속도형 카운터는 두 번 표본을 떠야 값이 나온다. 여기서 첫 표본을 뜬다.
            try:
                win32pdh.CollectQueryData(self._query)
                self._primed = True
            except Exception as e:
                log.debug("PDH 초기 표본 실패", extra={"error": str(e)})
        return self

    def close(self) -> None:
        if self._query is not None:
            try:
                win32pdh.CloseQuery(self._query)
            except Exception:
                pass
            self._query = None
        self._handles.clear()

    # ------------------------------------------------------------------ 수집

    def collect(self) -> dict[str, float]:
        """현재 값들. 아직 준비되지 않은 카운터는 결과에서 빠진다."""
        if not self._handles or self._query is None:
            return {}

        try:
            win32pdh.CollectQueryData(self._query)
        except Exception as e:
            # 표본이 하나뿐인 시점에는 정상적으로 실패한다. 다음 호출에서 값이 나온다.
            if self._primed:
                log.debug("PDH 표본 수집 실패", extra={"error": str(e)})
            return {}

        out: dict[str, float] = {}
        for key, handle in self._handles.items():
            try:
                _type, value = win32pdh.GetFormattedCounterValue(handle, win32pdh.PDH_FMT_DOUBLE)
            except Exception:
                # 첫 표본 직후·인스턴스 소멸 등에서 발생. 이번 틱만 건너뛴다.
                continue
            if value is None or value < 0:
                continue
            out[key] = value * self._scales.get(key, 1.0)
        return out

    def __enter__(self) -> "PdhCounters":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


if __name__ == "__main__":  # 스모크: python -m argus.collector.pdh
    import time

    from ..logging_setup import setup

    setup(level="INFO")

    index_map = english_counter_indices()
    print(f"  영문 카운터 목록: {len(index_map)}개")

    with PdhCounters() as pdh:
        if not pdh.available:
            print(f"[FAIL] 사용 가능한 PDH 카운터가 없다: {pdh.failures}")
            raise SystemExit(1)

        print("  해석된 경로:")
        for key, path in sorted(pdh.resolved_paths.items()):
            print(f"    {key:18} {path}")
        if pdh.failures:
            print(f"  비활성: {pdh.failures}")

        seen: dict[str, float] = {}
        for i in range(4):
            time.sleep(0.5)
            values = pdh.collect()
            seen.update(values)
            rendered = "  ".join(f"{k}={v:.3f}" for k, v in sorted(values.items()))
            print(f"  [{i}] {rendered or '(아직 값 없음)'}")

        missing = set(pdh.resolved_paths) - set(seen)
        if missing:
            print(f"[FAIL] 값을 한 번도 못 읽은 카운터: {sorted(missing)}")
            raise SystemExit(1)
    print("[OK] collector.pdh")

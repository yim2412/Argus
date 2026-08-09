"""규칙을 하나씩 무력화하고 테스트가 잡는지 측정한다 (mutation sweep).

**통과는 증거가 아니다.** 아무것도 검증하지 않는 테스트도 통과한다. 그래서 지키려는
규칙을 코드에서 실제로 지운 뒤 테스트가 빨간불이 되는지를 확인한다. 빨간불이 안 켜지면
그 규칙은 검증된 적이 없는 것이다.

**손으로 하지 않는 이유는 2026-07-29 사고다.** 그날 `_SCORE_SATURATION_Z` 를 8.0 →
1.0 으로 바꿔 컴파일한 뒤 같은 초 안에 되돌렸는데, Python 은 `.pyc` 유효성을
(소스 mtime 초, 소스 크기) 로만 판단하고 `8.0`/`1.0` 은 크기까지 같아서 **캐시가
무효화되지 않았다.** 그 뒤 며칠간 모든 룰 점수가 1.0 으로 포화된 채 돌았고, 그날의
mutation 측정 자체가 오염됐다. 이 스크립트는 무력화 전후로 매번 `__pycache__` 를
지우고, 끝나고 `tools/pyc_audit.py` 로 확인한다.

사용:
    .venv\\Scripts\\python.exe tools\\mutation_sweep.py            # 전체
    .venv\\Scripts\\python.exe tools\\mutation_sweep.py --list     # 대상만 보기
    .venv\\Scripts\\python.exe tools\\mutation_sweep.py --only mad_to_sigma rule_for
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutant:
    """규칙 하나를 무력화하는 소스 치환."""

    key: str
    rule: str  # 무력화되는 규칙 (사람 말로)
    edits: tuple[tuple[str, str, str], ...]  # (상대경로, 원문, 무력화문)
    note: str = ""
    expect_caught: bool = True  # False = 못 잡히는 것이 이미 알려진 것

    def paths(self) -> list[pathlib.Path]:
        return [ROOT / rel for rel, _, _ in self.edits]


# --------------------------------------------------------------------------- 대상
#
# 07-30 점검에서 "안 잡힘"으로 분류된 7개와 "잡힘"으로 분류된 7개를 모두 다시 잰다.
# 후자를 다시 재는 이유: 그 판정이 캐시 오염 상태에서 나왔다.
MUTANTS: list[Mutant] = [
    # ---- 07-30 에 "안 잡힘" → 그 뒤 테스트를 붙였다. 회귀 확인이다.
    Mutant(
        "mad_to_sigma",
        "MAD → σ 환산 계수 (모든 z 점수의 기반)",
        (("argus/detection/baseline.py", "MAD_TO_SIGMA = 1.4826", "MAD_TO_SIGMA = 1.0"),),
    ),
    Mutant(
        "min_days",
        "지문 자격 — 최소 관측 일수",
        (
            ("argus/detection/fingerprint.py", "DEFAULT_MIN_DAYS = 3", "DEFAULT_MIN_DAYS = 0"),
            ("argus/config/defaults.yaml", "min_days: 3 ", "min_days: 0 "),
        ),
    ),
    Mutant(
        "min_buckets",
        "지문 자격 — 최소 누적 버킷",
        (
            (
                "argus/detection/fingerprint.py",
                "DEFAULT_MIN_BUCKETS = 100",
                "DEFAULT_MIN_BUCKETS = 0",
            ),
            ("argus/config/defaults.yaml", "min_buckets: 100 ", "min_buckets: 0   "),
        ),
    ),
    Mutant(
        "min_day_hours",
        "지문 자격 — 짧게 켠 날은 하루로 세지 않는다 (판정 로직)",
        (
            (
                "argus/detection/fingerprint.py",
                "            days = sum(1 for buckets in by_day.values() "
                "if buckets >= min_buckets_per_day)\n",
                "            days = len(by_day)  # MUTANT: 짧은 날도 하루로 센다\n",
            ),
        ),
    ),
    Mutant(
        "min_day_hours_wiring",
        "지문 자격 — config → 빌더 배선",
        (
            (
                "argus/detection/fingerprint.py",
                "                min_day_hours=self.min_day_hours,\n",
                "                # MUTANT: 배선 제거 — 기본값으로만 돈다\n",
            ),
        ),
    ),
    Mutant(
        "score_saturation_z",
        "점수 포화 z (등급 구분의 근거)",
        (("argus/detection/rules.py", "_SCORE_SATURATION_Z = 8.0", "_SCORE_SATURATION_Z = 1.0"),),
    ),
    Mutant(
        "override_ratio",
        "병목 뒤집기 문턱 — 지표가 방아쇠를 이기려면 이만큼 강해야 한다",
        (
            (
                "argus/config/loader.py",
                "override_ratio: float = Field(default=1.5, gt=1.0)",
                "override_ratio: float = Field(default=1.001, gt=1.0)",
            ),
            ("argus/config/defaults.yaml", "override_ratio: 1.5", "override_ratio: 1.001"),
        ),
    ),
    Mutant(
        "max_extend_before_s",
        "사건 구간을 앞으로 늘리는 상한",
        (
            (
                "argus/config/loader.py",
                "max_extend_before_s: float = Field(default=300.0, gt=0)",
                "max_extend_before_s: float = Field(default=864000.0, gt=0)",
            ),
            (
                "argus/config/defaults.yaml",
                "max_extend_before_s: 300.0",
                "max_extend_before_s: 864000.0",
            ),
        ),
    ),
    # ---- 07-30 에 "잡힘" → 오염된 환경의 판정이라 다시 잰다.
    Mutant(
        "rule_for",
        "룰 지속 조건 (`for`) — 순간 스파이크는 이상이 아니다",
        (
            (
                "argus/detection/rules.py",
                "            if obs.ts - since < rule.for_s:\n                continue\n",
                "            if False:  # MUTANT: 지속 조건 무력화\n                continue\n",
            ),
        ),
    ),
    Mutant(
        "rule_cooldown",
        "룰 재발화 억제 (`cooldown`)",
        (
            (
                "argus/detection/rules.py",
                "            if last is not None and obs.ts - last < rule.cooldown_s:\n"
                "                continue\n",
                "            if False:  # MUTANT: 쿨다운 무력화\n                continue\n",
            ),
        ),
    ),
    Mutant(
        "disk_resp_floor",
        "디스크 응답의 체감 하한 — 이 아래는 병목이라 부르지 않는다",
        (
            (
                "argus/config/loader.py",
                "disk_resp_floor_ms: float = Field(default=5.0, gt=0)",
                "disk_resp_floor_ms: float = Field(default=0.001, gt=0)",
            ),
            ("argus/config/defaults.yaml", "disk_resp_floor_ms: 5.0", "disk_resp_floor_ms: 0.001"),
        ),
    ),
    Mutant(
        "lead_min_share",
        "선행성 최소 기여도 — 용의자가 아닌 것의 선행성은 잡음이다",
        (
            (
                "argus/config/loader.py",
                "lead_min_share: float = Field(default=0.10, ge=0, le=1)",
                "lead_min_share: float = Field(default=0.0, ge=0, le=1)",
            ),
            ("argus/config/defaults.yaml", "lead_min_share: 0.10", "lead_min_share: 0.0"),
        ),
    ),
    Mutant(
        "procleak_monotonic_logic",
        "누수 단조성 — 판정 로직 자체",
        (
            (
                "argus/detection/procleak.py",
                "    if non_decreasing < rule.monotonic_ratio:\n",
                "    if False:  # MUTANT: 단조성 판정 무력화\n",
            ),
        ),
        note="`test_procleak.py` 의 톱니 케이스가 겨냥하는 것",
    ),
    Mutant(
        # 로직과 나눠서 잰다. 2026-08-03 스윕에서 로직 테스트는 있는데 배선은 비어 있어,
        # YAML 을 고쳐도 판정이 안 바뀌는 상태였다(튜닝이 조용히 무시된다 = 규칙 3 위반).
        "procleak_monotonic",
        "누수 단조성 — config → 탐지기 배선",
        (
            (
                "argus/config/loader.py",
                "monotonic_ratio: float = Field(default=0.85, ge=0.0, le=1.0)",
                "monotonic_ratio: float = Field(default=0.0, ge=0.0, le=1.0)",
            ),
            (
                "argus/config/loader.py",
                "growth_ratio=3.0, min_delta=512.0, monotonic_ratio=0.9, min_delta_ram_ratio=0.02",
                "growth_ratio=3.0, min_delta=512.0, monotonic_ratio=0.0, min_delta_ram_ratio=0.02",
            ),
            ("argus/config/defaults.yaml", "monotonic_ratio: 0.85", "monotonic_ratio: 0.0 "),
            ("argus/config/defaults.yaml", "monotonic_ratio: 0.9\n", "monotonic_ratio: 0.0\n"),
        ),
    ),
    Mutant(
        "procleak_drop_reset",
        "누수 급락 리셋 — 급락은 PID 재사용이거나 정상 해제다",
        (
            (
                "argus/detection/procleak.py",
                "DEFAULT_DROP_RESET_RATIO = 0.5",
                "DEFAULT_DROP_RESET_RATIO = 0.001",
            ),
            (
                "argus/config/loader.py",
                "drop_reset_ratio: float = Field(default=0.5, gt=0, lt=1.0)",
                "drop_reset_ratio: float = Field(default=0.001, gt=0, lt=1.0)",
            ),
            ("argus/config/defaults.yaml", "drop_reset_ratio: 0.5 ", "drop_reset_ratio: 0.001 "),
        ),
    ),
    Mutant(
        "stock_drop_reset",
        "귀인의 저량 급락 리셋 — 반납한 양을 증가분으로 세지 않는다",
        (
            (
                "argus/config/loader.py",
                "stock_drop_reset_ratio: float = Field(default=0.5, gt=0, lt=1)",
                "stock_drop_reset_ratio: float = Field(default=0.001, gt=0, lt=1)",
            ),
            (
                "argus/config/defaults.yaml",
                "stock_drop_reset_ratio: 0.5",
                "stock_drop_reset_ratio: 0.001",
            ),
        ),
    ),
    Mutant(
        "severity_risk_axis",
        "등급의 위험 축 — 자기 p99 대비 위치로 등급을 가른다",
        (
            (
                "argus/detection/procleak.py",
                "        severity, risk_reason = leak_risk(\n",
                '        severity, risk_reason = "warning", ""  # MUTANT: 등급 고정\n'
                "        _unused_leak_risk = leak_risk(\n",
            ),
        ),
        note="고정 warning 으로 되돌린다 — 07-30 이전 상태",
    ),
    Mutant(
        "severity_risk_threshold",
        "등급 문턱 — config → 탐지기 배선",
        (
            (
                "argus/config/loader.py",
                "    risk_critical_ratio: float = Field(default=2.0, gt=0)",
                "    risk_critical_ratio: float = Field(default=1000.0, gt=0)",
            ),
            ("argus/config/defaults.yaml", "risk_critical_ratio: 2.0", "risk_critical_ratio: 1000.0"),
        ),
    ),
    Mutant(
        "private_growth_basis",
        "누수 증가율은 RSS 가 아니라 private 으로 잰다",
        (
            (
                "argus/desktop/pages/selfstate.py",
                '    with_private = [r for r in rows if r.get("private_mb") is not None]\n',
                '    with_private = [r for r in rows if r.get("rss_mb") is not None]\n',
            ),
            (
                "argus/desktop/pages/selfstate.py",
                '    delta = float(last["private_mb"]) - float(first["private_mb"])\n',
                '    delta = float(last["rss_mb"]) - float(first["rss_mb"])\n',
            ),
        ),
        note="RSS 는 워킹셋 트림에 따라 내려가 누수를 가린다 (07-27 실측 63→18MB)",
    ),
    Mutant(
        "selfstate_alert",
        "유실·스로틀은 경고로 드러낸다 (규칙 1 이 깨지는 신호다)",
        (
            (
                "argus/desktop/pages/selfstate.py",
                "        self._alert.setVisible(bool(messages))\n",
                "        self._alert.setVisible(False)  # MUTANT: 경고를 숨긴다\n",
            ),
        ),
    ),
    Mutant(
        "overlay_reset",
        "오버레이를 다시 그릴 때 이전 것을 지운다 (누적되면 화면이 덮인다)",
        (
            (
                "argus/desktop/widgets.py",
                "        for item in self._overlays:\n"
                "            self._plot.removeItem(item)\n"
                "        self._overlays.clear()\n",
                "        pass  # MUTANT: 이전 오버레이를 남긴다\n",
            ),
        ),
    ),
    Mutant(
        "incomplete_injection_faint",
        "증상 없는 주입은 흐리게 (채점 제외 구간이 탐지 실패로 보이면 안 된다)",
        (
            (
                "argus/desktop/widgets.py",
                '            colour.setAlphaF(0.22 if band.get("strong") else 0.07)\n',
                "            colour.setAlphaF(0.22)  # MUTANT: 항상 진하게\n",
            ),
        ),
    ),
    Mutant(
        "feedback_cache_invalidation",
        "피드백을 저장하면 캐시를 비운다 (이름이 어긋나면 조용히 죽는다)",
        (
            (
                "argus/dashboard/data.py",
                "    incidents.cache_clear()\n",
                "    pass  # MUTANT: 캐시를 비우지 않는다\n",
            ),
        ),
        note="st.cache_data 의 .clear() → ttl_cache 의 cache_clear() 로 바뀌며 어긋났다",
    ),
    Mutant(
        "incident_lead_threshold",
        "기여가 작은 후보에는 선행 시간을 붙이지 않는다",
        (
            (
                "argus/desktop/pages/incidents.py",
                '        "lead": f"{lead:.0f}초" if lead is not None and share >= 0.1 else "",\n',
                '        "lead": f"{lead:.0f}초" if lead is not None else "",\n',
            ),
        ),
        note="실측: 기여도 5% 짜리가 255초 선행으로 나왔다",
    ),
    Mutant(
        "table_query_order",
        "표는 조회가 준 순서를 지킨다 (setSortingEnabled 가 0번 열로 뒤집는다)",
        (
            (
                "argus/desktop/widgets.py",
                "            self.horizontalHeader().setSortIndicator(-1, QtCore.Qt.AscendingOrder)\n"
                "            self._proxy.sort(-1)\n",
                "            pass  # MUTANT: 기본 정렬을 그대로 둔다\n",
            ),
        ),
        note="프로세스 표가 CPU 순이 아니라 이름 역순으로 떴다",
    ),
    Mutant(
        "table_value_sort",
        "정렬은 표시 문자열이 아니라 원본 값으로 (9MB 가 10MB 뒤로 가면 안 된다)",
        (
            (
                "argus/desktop/widgets.py",
                "        a = self.sourceModel().data(left, QtCore.Qt.UserRole)\n"
                "        b = self.sourceModel().data(right, QtCore.Qt.UserRole)\n",
                "        a = self.sourceModel().data(left, QtCore.Qt.DisplayRole)\n"
                "        b = self.sourceModel().data(right, QtCore.Qt.DisplayRole)\n",
            ),
        ),
    ),
    Mutant(
        "realtime_live_count",
        "백필과 실시간 표본을 따로 센다 (합치면 갱신 판정이 무너진다)",
        (
            (
                "argus/desktop/pages/realtime.py",
                "            self._backfilled = len(ts)\n",
                "            self._live = len(ts)  # MUTANT: 백필을 실시간으로 센다\n",
            ),
        ),
        note="첫 측정에서 608개 중 600개가 백필이었다",
    ),
    Mutant(
        "realtime_duplicate_guard",
        "같은 타임스탬프를 다시 그리지 않는다 (수집이 멈추면 같은 행이 계속 온다)",
        (
            (
                "argus/desktop/pages/realtime.py",
                "        if self._last_ts is not None and ts <= self._last_ts:\n            return",
                "        if False:\n            return",
            ),
        ),
    ),
    Mutant(
        "realtime_cache_ttl",
        "실시간 조회 캐시가 수집 주기를 넘지 않는다",
        (
            (
                "argus/dashboard/data.py",
                "@ttl_cache(1.0)\ndef latest_metrics() -> dict | None:",
                "@ttl_cache(5.0)\ndef latest_metrics() -> dict | None:",
            ),
        ),
        note="예광탄 실측: TTL 2초일 때 12초에 6개만 그렸다",
    ),
    Mutant(
        "dashboard_pythonpath",
        "창에 sys.path 를 물려준다 (base 인터프리터엔 PySide6 가 없다)",
        (
            (
                "argus/ui/tray.py",
                '            env["PYTHONPATH"] = f"{inherited}{os.pathsep}{existing}"'
                " if existing else inherited\n",
                "            pass  # MUTANT: 경로를 물려주지 않는다\n",
            ),
        ),
    ),
    Mutant(
        "dashboard_death_report",
        "창이 곧바로 죽으면 사용자에게 말한다 (띄운 것과 뜬 것은 다르다)",
        (
            (
                "argus/ui/tray.py",
                '        self.notify("Argus", f"창을 열지 못했습니다 — {reason}", "warning")\n',
                "        pass  # MUTANT: 조용히 삼킨다\n",
            ),
        ),
    ),
    Mutant(
        "component_contract",
        "Component 규약 검사 — 없으면 스레드가 조용히 죽는다",
        (
            (
                "argus/runtime/supervisor.py",
                '        missing = [attr for attr in self._REQUIRED if not hasattr(component, attr)]\n',
                "        missing = []  # MUTANT: 규약 검사 무력화\n",
            ),
        ),
        note="2026-08-03 에 FingerprintBuilder 가 몇 주간 이 상태였다",
    ),
    Mutant(
        "notify_gate",
        "발송 게이트 — 판정(`notified`)과 실제 발송은 별개다",
        (
            (
                "argus/decide/fusion.py",
                "        if not self.notify_enabled or self.notifier is None:\n",
                "        if self.notifier is None:  # MUTANT: 게이트 무시\n",
            ),
        ),
        note="꺼져 있어도 보내게 만든다 — 켜기 전에 알림량을 재는 경로가 무너진다",
    ),
    Mutant(
        "notify_failure_isolation",
        "알림 실패가 융합을 죽이지 않는다 (탐지가 알림보다 중요하다)",
        (
            (
                "argus/decide/fusion.py",
                "        except Exception as exc:\n"
                "            # 알림 실패가 융합을 죽이면 사건 기록이 통째로 멈춘다."
                " 탐지가 알림보다 중요하다.\n"
                '            log.warning("알림 발송 실패", extra={"incident": incident_id,'
                ' "error": str(exc)})\n'
                "            return\n",
                "        except Exception:  # MUTANT: 격리 제거\n            raise\n",
            ),
        ),
    ),
    Mutant(
        "rollup_gpu_clock",
        "GPU 클럭을 롤업에 남긴다 (원본은 24시간 뒤 사라진다)",
        (
            (
                "argus/storage/rollup.py",
                '                    _min(g.get("clock_sm_mhz", [])),\n',
                "                    None,  # MUTANT: 클럭 최저값을 버린다\n",
            ),
        ),
    ),
    Mutant(
        "rollup_watermark",
        "삭제는 롤업 워터마크를 넘지 못한다 — 접히기 전에 지우면 영구 손실이다",
        (
            (
                "argus/storage/retention.py",
                "            if rollup is not None:\n"
                "                watermark = watermarks.get(rollup)\n",
                "            if False:  # MUTANT: 워터마크 무력화\n"
                "                watermark = watermarks.get(rollup)\n",
            ),
        ),
    ),
    Mutant(
        "throttle_wakeup",
        "스로틀이 풀리면 자던 스레드가 깬다 (안 깨면 관측이 조용히 빈다)",
        (
            (
                "argus/runtime/supervisor.py",
                "            self._stop.wait(min(remaining, self._wake_granularity_s))\n",
                "            self._stop.wait(remaining)  # MUTANT: 한 번에 자 버린다\n",
            ),
        ),
    ),
    Mutant(
        "selftel_active_clear",
        "예외로 끝난 tick 도 실행 중에서 지운다 (안 지우면 계측이 아무나 가리킨다)",
        (
            (
                "argus/runtime/supervisor.py",
                "            finally:\n"
                "                # tick 이 어떻게 끝나든 지운다. 예외 경로에서 빠뜨리면 그 컴포넌트가\n"
                "                # **영원히 실행 중으로 보여** 계측이 거짓말을 한다.\n"
                "                self._active.pop(name, None)\n",
                "                self._active.pop(name, None)  # MUTANT: 예외 경로에서만 안 지운다\n",
            ),
        ),
    ),
    Mutant(
        "selftel_active_column",
        "표본 시점의 실행 중 컴포넌트를 남긴다 (RSS 봉우리의 주인을 찾는 유일한 근거)",
        (
            (
                "argus/runtime/selftel.py",
                "                    self._active(),\n",
                "                    None,  # MUTANT: 실행 중 컴포넌트를 버린다\n",
            ),
        ),
    ),
    Mutant(
        "per_program_split",
        "프로그램별로 나눠 학습한다 (안 나누면 조용히 전역으로만 돈다)",
        (
            (
                "argus/detection/baseline.py",
                "            if self.per_program and program and name in self.program_metrics:\n"
                "                self._get_program(program, name).add(ts, float(value))\n",
                "            # MUTANT: 프로그램별 학습 제거\n",
            ),
        ),
    ),
    Mutant(
        "per_program_fallback",
        "표본이 설 때까지만 전역으로 (문턱을 지우면 표본 3개로 판정한다)",
        (
            (
                "argus/detection/baseline.py",
                "                if baseline is not None and baseline.ready:\n",
                "                if baseline is not None:  # MUTANT: 표본 문턱 제거\n",
            ),
        ),
    ),
    Mutant(
        "per_program_lru_cap",
        "프로그램 수 상한 (없으면 메모리가 무한히 는다 — 규칙 1)",
        (
            (
                "argus/detection/baseline.py",
                "            while len(self._by_program) >= self.max_programs:\n"
                "                self._by_program.pop(next(iter(self._by_program)))\n",
                "            pass  # MUTANT: 상한 제거\n",
            ),
        ),
    ),
    Mutant(
        "rules_registry_config",
        "레지스트리가 생성자를 등록한다 (클래스를 등록하면 config 가 통째로 무시된다)",
        (
            (
                "argus/detection/registry.py",
                '    register("rules", build_rules)\n',
                "    from .rules import RuleEngine\n"
                '    register("rules", RuleEngine)  # MUTANT: config 배선 제거\n',
            ),
        ),
    ),
    Mutant(
        "warm_inline_params",
        "웜 조회는 값을 박아 넣는다 (바인딩하면 Parquet 을 통째로 메모리에 올린다)",
        (
            (
                "argus/storage/warm.py",
                "        return con.execute(_inline_params(sql, list(params or []))).fetchall()\n",
                "        return con.execute(sql, params or []).fetchall()  # MUTANT: 바인딩으로\n",
            ),
        ),
    ),
    Mutant(
        "warm_union_by_name",
        "파티션을 이름으로 합친다 (없으면 스키마가 바뀐 날 과거가 사라진다)",
        (
            (
                "argus/storage/warm.py",
                '                "hive_partitioning = true, union_by_name = true)"\n',
                '                "hive_partitioning = true)"  # MUTANT: 이름 병합 제거\n',
            ),
        ),
    ),
    Mutant(
        "log_level_field",
        "로그레벨 자리를 extra 가 덮어쓰지 못한다 (거르면 통째로 누락된다)",
        (
            (
                "argus/logging_setup.py",
                '            payload[f"extra_{key}" if key in self._RESERVED else key] = value\n',
                "            payload[key] = value  # MUTANT: 예약 자리 보호 제거\n",
            ),
        ),
    ),
    # ---- 2026-08-06: 레이아웃. 전부 **그림으로 보기 전에는 신호가 없던** 것들이다.
    # 갱신 표본 수는 화면이 읽을 수 없는 상태에서도 정상으로 나왔다.
    Mutant(
        "chart_note_fixed_height",
        "차트 주석의 높이를 고정한다 (안 하면 그리드 한 행이 다른 행의 세 배가 된다)",
        (
            (
                "argus/desktop/widgets.py",
                "    label.setWordWrap(False)\n    label.setFixedHeight(NOTE_HEIGHT)\n",
                "    label.setWordWrap(True)  # MUTANT: 높이 고정 제거\n",
            ),
        ),
    ),
    Mutant(
        "chart_min_height",
        "차트에 읽을 수 있는 최소 높이를 준다 (없으면 55px 로 눌려 못 읽는다)",
        (
            (
                "argus/desktop/widgets.py",
                "MIN_PLOT_HEIGHT = 132",
                "MIN_PLOT_HEIGHT = 1",
            ),
        ),
    ),
    Mutant(
        "legend_outside_plot",
        "범례를 플롯 밖에 둔다 (안에 두면 차트가 작아질 때 데이터를 덮는다)",
        (
            (
                "argus/desktop/widgets.py",
                "    if len(names) > 1:\n"
                "        for index, name in enumerate(names):\n"
                "            row.addWidget(legend_chip(name, theme.SERIES[index % len(theme.SERIES)]))\n",
                "    # MUTANT: 제목 줄 범례 제거\n",
            ),
        ),
    ),
    Mutant(
        "table_min_rows",
        "표에 최소 행 수를 보장한다 (없으면 헤더와 한 줄만 남는다)",
        (
            (
                "argus/desktop/widgets.py",
                "        self.setMinimumHeight(height_for(min_rows))\n",
                "        # MUTANT: 표 최소 높이 제거\n",
            ),
        ),
    ),
    Mutant(
        "tile_fixed_height",
        "타일은 세로로 자라지 않는다 (자라면 100px 짜리가 460px 가 된다)",
        (
            (
                "argus/desktop/widgets.py",
                "        self.setSizePolicy(\n"
                "            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed\n"
                "        )\n",
                "        # MUTANT: 타일 세로 고정 제거\n",
            ),
        ),
    ),
    Mutant(
        "window_fits_screen",
        "창을 화면보다 크게 열지 않는다 (규칙 2 — 하드웨어를 가정하지 않는다)",
        (
            (
                "argus/desktop/app.py",
                "    return (\n"
                "        min(_WANTED_W, int(available.width() * 0.92)),\n"
                "        min(_WANTED_H, int(available.height() * 0.92)),\n"
                "    )",
                "    return _WANTED_W, _WANTED_H  # MUTANT: 화면 크기 무시",
            ),
        ),
    ),
    # ---- 2026-08-06: 알림 억제. **테스트가 실사용자 화면에 풍선을 띄우던 것을 막았다.**
    # 양쪽 다 조용히 깨진다 — 억제가 사라지면 알림이 "그냥 좀 많이" 뜨고, 억제가
    # 항상 켜지면 제품이 알림 없는 앱이 되는데 알림은 원래 드물어 구분되지 않는다.
    Mutant(
        "notify_suppression_switch",
        "억제 스위치가 발송을 막는다 (없으면 테스트가 사용자 화면에 풍선을 띄운다)",
        (
            (
                "argus/ui/tray.py",
                "        if notifications_suppressed():\n",
                "        if False:  # MUTANT: 억제 무시\n",
            ),
        ),
    ),
    Mutant(
        "notify_suppression_default_off",
        "억제는 기본이 꺼짐이다 (항상 켜지면 제품이 조용히 알림 없는 앱이 된다)",
        (
            (
                "argus/paths.py",
                '    return os.environ.get(ENV_NO_NOTIFY, "").strip().lower() not in ("", "0", "false", "no")',
                "    return True  # MUTANT: 항상 억제",
            ),
        ),
    ),
    Mutant(
        "shutdown_test_suppresses_notifications",
        "종료 테스트가 억제를 켜고 상주를 띄운다 (안 켜면 실행마다 풍선이 뜬다)",
        (
            (
                "tests/test_shutdown.py",
                '        ARGUS_NO_NOTIFY="1",\n',
                "        # MUTANT: 억제 미설정\n",
            ),
        ),
        expect_caught=False,
        note="테스트 자신의 배선이라 무력화해도 조용하다 — 풍선이 뜨는지는 사람이 본다",
    ),
    # ---- 2026-08-06: 아이콘·앱 정체. 셋 다 **예외 없이 파이썬 아이콘으로 돌아가는**
    # 종류다 — 빌드해서 눈으로 보기 전까지 아무 신호가 없다.
    Mutant(
        "tray_own_icon",
        "트레이가 전용 아이콘을 읽는다 (폴백으로 돌아가면 시스템 느낌표가 된다)",
        (
            (
                "argus/ui/tray.py",
                "        path = icon_path()\n        if path.exists():\n",
                "        path = icon_path()\n        if False:  # MUTANT: 전용 아이콘 사용 제거\n",
            ),
        ),
    ),
    Mutant(
        "app_id_daemon",
        "상주가 AppUserModelID 를 밝힌다 (없으면 알림 발신자가 파이썬이다)",
        (
            (
                "argus/__main__.py",
                "    set_app_id()\n",
                "    # MUTANT: 상주 AppUserModelID 제거\n",
            ),
        ),
    ),
    Mutant(
        "app_id_window_order",
        "창이 QApplication **전에** 정체를 밝힌다 (뒤로 밀면 API 는 성공하고 그룹만 파이썬으로 남는다)",
        (
            (
                "argus/desktop/app.py",
                "    set_app_id()\n\n    app = QtWidgets.QApplication(sys.argv)\n",
                "    app = QtWidgets.QApplication(sys.argv)\n    set_app_id()  # MUTANT: 순서 뒤집기\n",
            ),
        ),
    ),
    # ---- 2026-08-06: 실행 중 알림 켬/끔. 판정과 발송을 나눠 둔 것이 이 기능의 전제다.
    Mutant(
        "live_notify_at_send",
        "발송 시점마다 값을 다시 본다 (기동 시 값을 들고 있으면 껐는데도 알림이 나간다)",
        (
            (
                "argus/decide/fusion.py",
                "        if self.live is not None:\n            return bool(self.live.notify_enabled)\n",
                "        # MUTANT: 실행 중 값 무시\n",
            ),
        ),
    ),
    Mutant(
        "live_judgement_survives",
        "끄는 것은 발송뿐 — 판정(notified)은 계속 돈다 (멈추면 알림량 측정이 조용히 죽는다)",
        (
            (
                "argus/decide/fusion.py",
                "        decision = self.budget.decide(self.db, dict(rows[0]))\n",
                "        if not self.notify_enabled:  # MUTANT: 꺼지면 판정까지 중단\n"
                "            return\n"
                "        decision = self.budget.decide(self.db, dict(rows[0]))\n",
            ),
        ),
    ),
    Mutant(
        "live_reload_watch",
        "파일이 바뀌면 다시 읽는다 (안 읽으면 창에서 바꾼 값이 상주에 영영 안 닿는다)",
        (
            (
                "argus/runtime/livecfg.py",
                "        if not force and stamp == self._stamp:\n            return False\n",
                "        if not force:  # MUTANT: 변경 감지 제거\n            return False\n",
            ),
        ),
    ),
    Mutant(
        "live_menu_state",
        "메뉴는 열 때마다 현재 값을 읽는다 (고정하면 체크 표시가 거짓말을 한다)",
        (
            (
                "argus/ui/tray.py",
                "                if self.live.notify_enabled:\n                    flags |= win32con.MF_CHECKED\n",
                "                flags |= win32con.MF_CHECKED  # MUTANT: 항상 체크\n",
            ),
        ),
    ),
    Mutant(
        "live_window_sync",
        "창이 트레이의 변경을 따라온다 (한쪽만 보면 두 화면이 다른 값을 보여 준다)",
        (
            (
                "argus/desktop/pages/settings.py",
                "        changed = self._live.reload()\n",
                "        changed = False  # MUTANT: 파일 변경 무시\n",
            ),
        ),
    ),
    # ---- 2026-08-09: 첫 실행 안내. Streamlit 홈에만 있던 것을 창으로 옮겼다.
    # **조용히 되돌아간다** — 안내가 사라져도 창은 정상 동작하고, 처음 켠 사용자만
    # "…기다리는 중" 앞에서 막힌다. 개발 PC 에는 DB 가 있어 영영 안 보이는 경로다.
    Mutant(
        "first_run_path",
        "안내가 찾은 경로를 말한다 (없으면 '안 켠 것'과 '엉뚱한 곳을 보는 것'이 같아진다)",
        (
            (
                "argus/desktop/app.py",
                'return f"수집이 아직 시작되지 않았습니다 — {how}.\\n찾은 위치: {path}"',
                'return f"수집이 아직 시작되지 않았습니다 — {how}."  # MUTANT: 경로 제거',
            ),
        ),
    ),
    Mutant(
        "first_run_frozen_command",
        "exe 사용자에게 실행 가능한 명령을 안내한다 (`python -m argus` 는 그들에게 없다)",
        (
            (
                "argus/desktop/app.py",
                '    how = "argus.exe 를 실행하세요" if is_frozen() else "`python -m argus` 로 시작하세요"',
                '    how = "`python -m argus` 로 시작하세요"  # MUTANT: 실행 형태 무시',
            ),
        ),
    ),
    Mutant(
        "first_run_banner_clears",
        "DB 가 생기면 안내가 사라진다 (남으면 정상 상태에 경고가 붙박인다)",
        (
            (
                "argus/desktop/app.py",
                "        if text is None:\n            self.hide()\n            return\n",
                "        if text is None:\n            return  # MUTANT: 숨기지 않는다\n",
            ),
        ),
    ),
    # ---- 2026-08-09: 알림 → 사건 → 평가. 라벨이 이 경로로만 들어온다.
    # **전부 조용히 되돌아간다** — 끊겨도 알림은 그대로 뜨고 창도 그대로 열린다.
    # 안 생기는 것은 피드백뿐이고, 그건 원래도 0건이라 아무 신호가 없다.
    Mutant(
        "notify_incident_id",
        "알림이 자기 사건 id 를 들고 간다 (없으면 눌러도 갈 곳이 없다)",
        (
            (
                "argus/decide/fusion.py",
                "severity, incident_id=incident_id)",
                "severity)  # MUTANT: 사건 id 를 버린다",
            ),
        ),
    ),
    Mutant(
        "balloon_click_route",
        "풍선을 누르면 그 사건이 열린다 (id 를 안 넘기면 평범한 창 열기가 된다)",
        (
            (
                "argus/ui/tray.py",
                "            self._open_dashboard(incident_id=self._balloon_incident)\n",
                "            self._open_dashboard()  # MUTANT: 어느 사건인지 버린다\n",
            ),
        ),
    ),
    Mutant(
        "focus_beats_default_selection",
        "알림으로 연 사건이 첫 줄 자동 선택에 덮이지 않는다 (덮이면 틀린 라벨이 남는다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                "            self._select_pending()\n            return\n",
                "            self._select_pending()  # MUTANT: 아래로 흘려보낸다\n",
            ),
        ),
    ),
    Mutant(
        "focus_widens_range",
        "구간 밖 사건이면 한 번 넓혀 본다 (안 넓히면 오래된 알림은 영영 못 찾는다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                "        if not self._widened and int(self._days.currentData()) != widest:",
                "        if False:  # MUTANT: 구간을 넓히지 않는다",
            ),
        ),
    ),
    Mutant(
        "spec_icon_resource",
        "빌드가 아이콘을 동봉한다 (빠지면 트레이가 런타임에 못 읽는다)",
        (
            (
                "packaging/argus.spec",
                '    (ICON, "assets"),\n',
                "    # MUTANT: 아이콘 동봉 제거\n",
            ),
        ),
    ),
]


# --------------------------------------------------------------------------- 실행
def clear_pycache() -> int:
    """`__pycache__` 를 전부 지운다. **이 절차를 빠뜨린 것이 07-29 사고의 원인이다.**"""
    removed = 0
    for path in ROOT.rglob("__pycache__"):
        if ".venv" in path.parts:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def apply(mutant: Mutant, originals: dict[pathlib.Path, str]) -> None:
    """치환을 적용한다. 원문이 정확히 1회 나오지 않으면 멈춘다 —
    0회면 코드가 바뀐 것이고, 2회 이상이면 무엇을 무력화했는지 말할 수 없다."""
    for rel, old, new in mutant.edits:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"[중단] {mutant.key}: {rel} 에서 원문이 {count}회 발견됐다 (1회여야 한다)\n"
                f"        원문: {old!r}"
            )
        path.write_text(text.replace(old, new), encoding="utf-8")
        assert path in originals


def restore(originals: dict[pathlib.Path, str]) -> None:
    for path, text in originals.items():
        path.write_text(text, encoding="utf-8")


_SUMMARY = re.compile(r"(\d+) (passed|failed|xfailed|xpassed|error)")


def run_pytest() -> tuple[bool, str, list[str]]:
    """(하나라도 실패했나, 요약 줄, 실패한 테스트 목록)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in out.splitlines() if _SUMMARY.search(ln)]
    summary = tail[-1].strip() if tail else "(요약 없음)"
    failed = sorted({ln.split(" ")[1] for ln in out.splitlines() if ln.startswith("FAILED ")})
    return proc.returncode != 0, summary, failed


@dataclass
class Result:
    mutant: Mutant
    caught: bool
    summary: str
    failed: list[str] = field(default_factory=list)
    seconds: float = 0.0


def sweep(targets: list[Mutant]) -> list[Result]:
    results: list[Result] = []
    for i, mutant in enumerate(targets, 1):
        originals = {p: p.read_text(encoding="utf-8") for p in mutant.paths()}
        started = time.time()
        print(f"\n[{i}/{len(targets)}] {mutant.key} — {mutant.rule}", flush=True)
        try:
            apply(mutant, originals)
            clear_pycache()
            caught, summary, failed = run_pytest()
        finally:
            restore(originals)
            clear_pycache()
        elapsed = time.time() - started
        mark = "[잡힘]" if caught else "[안 잡힘]"
        print(f"      {mark} {summary}  ({elapsed:.0f}초)", flush=True)
        for name in failed[:4]:
            print(f"        - {name}", flush=True)
        if len(failed) > 4:
            print(f"        … 외 {len(failed) - 4}개", flush=True)
        results.append(Result(mutant, caught, summary, failed, elapsed))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="규칙 무력화 스윕")
    parser.add_argument("--list", action="store_true", help="대상만 출력")
    parser.add_argument("--only", nargs="+", metavar="KEY", help="지정한 것만")
    args = parser.parse_args()

    if args.list:
        for m in MUTANTS:
            print(f"{m.key:22} {m.rule}")
        return 0

    targets = MUTANTS
    if args.only:
        unknown = set(args.only) - {m.key for m in MUTANTS}
        if unknown:
            print(f"[중단] 모르는 대상: {', '.join(sorted(unknown))}")
            return 2
        targets = [m for m in MUTANTS if m.key in set(args.only)]

    # 무력화 전에 기준을 잡는다. 여기서 이미 빨간불이면 스윕 결과를 읽을 수 없다.
    clear_pycache()
    print("기준선 (무력화 없음) …", flush=True)
    failing, summary, _ = run_pytest()
    print(f"  {summary}", flush=True)
    if failing:
        print("[중단] 무력화 전부터 테스트가 실패한다. 먼저 그것부터 고친다.")
        return 1

    results = sweep(targets)

    print("\n" + "=" * 72)
    print(f"{'대상':22} {'결과':10} 규칙")
    print("-" * 72)
    surprises: list[Result] = []
    for r in results:
        mark = "잡힘" if r.caught else "안 잡힘"
        print(f"{r.mutant.key:22} {mark:10} {r.mutant.rule}")
        if r.caught != r.mutant.expect_caught:
            surprises.append(r)
    caught_n = sum(1 for r in results if r.caught)
    print("-" * 72)
    print(f"잡힘 {caught_n}/{len(results)}")

    blind = [r for r in results if not r.caught]
    if blind:
        print("\n검증되지 않는 규칙 — 무력화해도 테스트가 조용하다:")
        for r in blind:
            tail = f"  ({r.mutant.note})" if r.mutant.note else ""
            print(f"  - {r.mutant.key}: {r.mutant.rule}{tail}")

    if surprises:
        print("\n예상과 다른 것:")
        for r in surprises:
            expected = "잡힐 것으로" if r.mutant.expect_caught else "못 잡을 것으로"
            print(f"  - {r.mutant.key}: {expected} 적혀 있었는데 반대로 나왔다")

    # 되돌림이 실제로 반영됐는지. 이 검사가 없으면 07-29 사고를 다시 낸다.
    #
    # **감사 전에 테스트를 한 번 더 돌린다.** 스윕은 끝날 때 `__pycache__` 를 지우므로
    # 그 상태로 감사하면 검사할 `.pyc` 가 하나도 없어 "검사한 모듈 0개 [OK]" 가 나온다.
    # 아무것도 보지 않고 통과하는 검사다 — 첫 실행(2026-08-03)이 실제로 그랬다.
    # 복원된 소스로 한 번 컴파일시킨 뒤 그 캐시를 소스와 대조해야 의미가 생긴다.
    print("\n복원 확인 (캐시를 다시 만든다) …", flush=True)
    failing, summary, _ = run_pytest()
    print(f"  {summary}", flush=True)
    if failing:
        print("[FAIL] 복원 후에도 테스트가 실패한다 — 소스가 무력화된 채 남았을 수 있다.")
        return 1

    print("캐시 감사 …", flush=True)
    audit = subprocess.run(
        [sys.executable, "tools/pyc_audit.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print((audit.stdout or "").strip())
    if audit.returncode != 0:
        print("[FAIL] 캐시가 소스와 어긋난다 — __pycache__ 를 지우고 다시 확인한다.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

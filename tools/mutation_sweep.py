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
        # 2026-08-17. 이 축이 처음 들어왔을 때 문턱이 코드 상수(1536MB)였고, 바로 옆
        # PID별 문턱은 `min_delta_ram_ratio` 로 1,303.6MB 가 되어 있었다. 배수가
        # 3.0 이 아니라 1.17 이라 실주입 `#65`·`#66` 이 둘 다 미탐이었는데,
        # **테스트 10개가 전부 통과했다** — 문턱을 상수에서 읽어 단언했기 때문이다.
        "procleak_group_multiple",
        "그룹 축 문턱 — PID별 문턱의 배수여야 한다 (따로 두면 갈린다)",
        (
            (
                "argus/detection/procleak.py",
                "            min_delta=rule.min_delta * multiple,",
                "            min_delta=1536.0,  # MUTANT: 배수를 무시하고 절대값으로",
            ),
        ),
        note="갈렸을 때 무엇이 미탐이 되는지는 `test_group_threshold_is_derived_from_the_pid_threshold`",
    ),
    Mutant(
        "procleak_group_wiring",
        "그룹 축 문턱 — config → 탐지기 배선",
        (
            # **config 값을 바꾸는 것은 배선을 끊는 것이 아니다.** 2026-08-17 에
            # `min_delta_multiple` 을 0.8 → 99.0 으로 바꿔 봤다가 565개가 전부
            # 통과했는데, 그건 배선이 멀쩡해서 탐지기 값도 같이 99.0 이 됐기
            # 때문이다. 재야 할 것은 `build()` 가 **config 를 안 읽고 코드
            # 기본값으로 도는 상황**이다 — 2026-08-04 의 레지스트리 구멍이 그 형태다.
            (
                "argus/detection/procleak.py",
                "group_rules_from(rules, multiple=group.min_delta_multiple)",
                "group_rules_from(rules, multiple=DEFAULT_GROUP_MULTIPLE)",
            ),
        ),
    ),
    Mutant(
        "procleak_group_axis",
        "그룹 축 자체 — 흩어진 누수는 PID별 축이 전부 놓친다",
        (
            (
                "argus/detection/procleak.py",
                "        best = self._evaluate_groups(obs, best)",
                "        pass  # MUTANT: 그룹 축 판정 제거",
            ),
        ),
    ),
    Mutant(
        # 2026-08-17. 표본이 안 모인 프로그램에서 전역으로 물러나면 유휴가 섞인
        # 낮은 문턱으로 판정된다 — `#188` 이 그렇게 +7.69%p 로 발화했다. 되돌리는
        # 방향(폴백 부활)이 곧 그 오탐을 되살리는 것이라 그쪽으로 무력화한다.
        "per_program_strict_logic",
        "표본 없는 프로그램에서는 판정하지 않는다 — 판정 로직",
        (
            (
                "argus/detection/baseline.py",
                "            if self.program_strict:\n                return None\n",
                "            if False:  # MUTANT: 전역 폴백 부활\n                return None\n",
            ),
        ),
        note="되살아나는 것은 예외가 아니라 낮은 문턱이다 — 조용히 깨지는 쪽",
    ),
    Mutant(
        "per_program_strict_wiring",
        "표본 없는 프로그램에서는 판정하지 않는다 — config → 탐지기 배선",
        (
            (
                "argus/detection/rules.py",
                "            program_strict=cfg.per_program_strict,",
                "            program_strict=True,  # MUTANT: 설정을 안 읽는다",
            ),
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
        "observer_guard",
        "관측자가 예산을 넘은 구간은 자동 라벨이 덮지 않는다 (설계 규칙 1)",
        (
            (
                "argus/decide/autolabel.py",
                "        if not observer.clean:\n",
                "        if False:  # MUTANT: 관측자가 더러워도 판정한다\n",
            ),
        ),
        note=(
            "모니터가 병목이 된 상황을 자동 라벨이 normal 로 덮으면 제품 실패가 묻힌다. "
            "무력화하면 스로틀·드롭 구간까지 normal 이 붙는다"
        ),
    ),
    Mutant(
        "observer_needs_telemetry",
        "관측자 실측이 없으면 결백을 단정하지 않는다",
        (
            (
                "argus/decide/autolabel.py",
                "        if observer is None or observer.samples == 0:\n",
                "        if observer is None:  # MUTANT: 빈 표본을 실측으로 본다\n",
            ),
        ),
        note=(
            "self_telemetry 는 7일 보존이라 오래된 사건은 증명할 방법이 없다. "
            "`observer is None` 은 남긴다 — 지우면 다음 줄이 None.clean 으로 AttributeError 를 "
            "내서 논리가 아니라 예외가 잡히고, 그건 측정이 아니다"
        ),
    ),
    Mutant(
        "finished_batch_has_no_remaining",
        "끝난 주입 배치에 남은 시간을 적지 않는다 (전역 규칙 1)",
        (
            (
                "tools/inject_progress.py",
                "    if pending:\n",
                "    if True:  # MUTANT: 끝난 배치에도 남음을 적는다\n",
            ),
        ),
        note=(
            "진행을 재는 도구가 끝난 것을 두고 '남음 약 17분'이라 말하면 그것이 추측 "
            "보고다. 2026-08-15 에 13일 전 배치가 실제로 그렇게 발표됐다"
        ),
    ),
    Mutant(
        "aborted_round_counts_as_planned",
        "중단된 회차를 '덜 진행된 것'으로 세지 않는다",
        (
            (
                "tools/inject_progress.py",
                "        total_done += min(span, planned) if running else planned\n",
                "        total_done += min(span, planned)  # MUTANT: 실측 길이로 센다\n",
            ),
        ),
        note=(
            "개별 줄은 전부 '완료 100%' 인데 전체만 85% 가 된다. 그 회차는 덜 돈 것이 "
            "아니라 더 돌 계획이 없는 것이다"
        ),
    ),
    Mutant(
        "contributor_head_follows_attribution",
        "귀인이 성립하지 않는 사건의 기여도 표를 '원인 후보'라 부르지 않는다",
        (
            (
                "argus/desktop/pages/incidents.py",
                "            _CONTRIBUTORS_HEAD if incident.get(\"attributable\")"
                " else _CONTRIBUTORS_HEAD_REFERENCE\n",
                "            _CONTRIBUTORS_HEAD  # MUTANT: 발열도 원인 후보라 부른다\n",
            ),
        ),
        note=(
            "리포트 본문은 '원인 프로세스는 특정할 수 없습니다'라고 적는데 표 제목이 "
            "그것을 뒤집었다. 실측 #59 에서 GPU 90°C 사건의 1위가 관측자 자신(pythonw 22%)"
        ),
    ),
    Mutant(
        "incidents_carry_attributable",
        "조회 계층이 attributable 을 화면에 넘긴다 (배선)",
        (
            (
                "argus/dashboard/data.py",
                '        row["attributable"] = is_attributable(row.get("bottleneck"))\n',
                "        pass  # MUTANT: 파생 필드를 안 붙인다\n",
            ),
        ),
        note=(
            "안 붙이면 화면의 .get('attributable') 이 조용히 None 이 되어 모든 사건이 "
            "'참고'로 보인다. 예외가 안 나므로 실행만 해서는 안 드러난다"
        ),
    ),
    Mutant(
        "backfill_shares_judgement",
        "백필 미리보기가 저장과 같은 판정 경로를 지난다",
        (
            (
                "tools/autolabel_backfill.py",
                '            verdict = autolabel.evaluate(db, int(row["id"]), cfg.autolabel)\n',
                '            verdict = autolabel.Verdict(None, "")  # MUTANT: 미리보기 전용 경로\n',
            ),
        ),
        note=(
            "이 도구에는 테스트가 없어서, 08-15 에 judge 가 observer 를 필수로 받게 되자 "
            "미리보기만 TypeError 로 죽었는데도 전체 531개가 통과했다. "
            "미리보기가 저장 결과와 다른 답을 찍으면 미리보기를 볼 이유가 없다"
        ),
    ),
    Mutant(
        "autolabel_gate_does_not_stamp",
        "판정 대상이 아닌 사건은 auto_label 칸을 건드리지 않는다",
        (
            (
                "argus/decide/autolabel.py",
                "    if not storable:\n        return verdict\n",
                "    if False:  # MUTANT: 게이트 사유까지 칸에 찍는다\n        return verdict\n",
            ),
        ),
        note=(
            "사람이 답한 사건에 '사람이 이미 답했다'가 근거로 찍히면 "
            "사람 답과 기계 답이 같은 화면에서 서로를 설명하게 된다"
        ),
    ),
    Mutant(
        "warm_child_encoding",
        "자식 출력을 로캘이 아니라 UTF-8 로 디코딩한다",
        (
            # **인자 두 줄을 지운다.** `pass` 로 치환하면 인자 자리에서 SyntaxError 가 나
            # 모듈이 임포트조차 안 되고, 그러면 "잡힘"이 나와도 그건 테스트가 잡은 게
            # 아니라 문법 오류가 잡힌 것이다 (2026-08-15 첫 등록에서 실제로 그랬다).
            # 무력화는 **돌아가는 코드**여야 측정이 성립한다.
            (
                "argus/storage/warm.py",
                '                encoding="utf-8",\n'
                '                errors="replace",  # 로그 한 줄 때문에 내보내기 결과를 잃지 않는다\n',
                "",
            ),
        ),
        note=(
            "개발 PC 가 UTF-8 로캘(2026-08-15~)이라 실행만으로는 절대 안 드러난다. "
            "CP949 PC 에서 stderr 가 None 이 되어 실패 원인이 조용히 사라진다"
        ),
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
    # ---- 부하 조건부 베이스라인 (2026-08-12). 상한이 걸린 지표(GPU 온도)에서
    #      σ 기반 상대 조건이 도달 불가가 되어 룰이 9일간 발화 불능이었다.
    #      **조용히 깨지는 유형이다** — 판정 불가는 예외가 아니라 "이상 없음"처럼 보인다.
    Mutant(
        "load_gate_logic",
        "부하 축이 유휴 표본을 배제한다 (섞이면 문턱이 다시 도달 불가가 된다)",
        (
            (
                "argus/detection/baseline.py",
                "            if value is None or gate_value is None or gate_value < gate.min_value:\n",
                "            if value is None:  # MUTANT: 게이트를 무시한다\n",
            ),
        ),
    ),
    Mutant(
        "load_gate_wiring",
        "부하 축 — config → 엔진 배선 (끊기면 YAML 을 고쳐도 판정이 안 바뀐다)",
        (
            (
                "argus/detection/rules.py",
                "            load_gates={\n"
                "                metric: LoadGate(metric=gate.metric, min_value=gate.min)\n"
                "                for metric, gate in cfg.load_gates.items()\n"
                "            },\n",
                "            load_gates=None,  # MUTANT: 설정을 전달하지 않는다\n",
            ),
        ),
    ),
    Mutant(
        "load_axis_no_fallback",
        "부하 축은 표본 부족 시 전역으로 폴백하지 않는다 (폴백하면 발화 불능으로 되돌아간다)",
        (
            (
                "argus/detection/baseline.py",
                "        baseline = self._by_load.get(metric)\n"
                "        if baseline is None or not baseline.ready:\n"
                "            return None\n"
                "        return baseline.stats()\n",
                "        baseline = self._by_load.get(metric)\n"
                "        if baseline is None or not baseline.ready:\n"
                "            return self.stats(metric)  # MUTANT: 전역으로 폴백\n"
                "        return baseline.stats()\n",
            ),
        ),
    ),
    Mutant(
        "thermal_rule_uses_load_axis",
        "GPU 고온 룰이 부하 축을 참조한다 (σ 로 되돌리면 상한 때문에 영영 안 울린다)",
        (
            (
                "argus/config/rules.yaml",
                '        - {metric: gpu_temp_c, op: ">", value: "load_median + 5"}',
                '        - {metric: gpu_temp_c, op: ">", value: "median + 3 * sigma"}',
            ),
        ),
    ),
    Mutant(
        "load_warm_span",
        "워밍이 가장 긴 축을 덮는다 (30분만 읽으면 부하 축은 매 기동 백지다)",
        (
            (
                "argus/detection/baseline.py",
                "        if self._needs_load_axis():\n            span_s = max(span_s, self.load_window_s)\n",
                "        # MUTANT: 부하 축 창을 무시한다\n",
            ),
        ),
    ),
    # ---- 보존 정리의 청크 삭제 (2026-08-12). 무력화해도 행은 똑같이 지워지고 예외도
    #      안 난다 — 달라지는 것은 전역 락을 얼마나 오래 잡는지뿐이다(조용히 깨진다).
    Mutant(
        "delete_chunking",
        "보존 정리가 삭제를 나눈다 (안 나누면 락 보유가 밀린 양에 비례한다)",
        (
            (
                "argus/storage/retention.py",
                "        chunk = int(getattr(self.settings, \"delete_chunk_rows\", 0) or 0)\n",
                "        chunk = 0  # MUTANT: 나누지 않는다\n",
            ),
        ),
    ),
    Mutant(
        "delete_chunk_wiring",
        "청크 크기 — config → 정리기 배선",
        (
            (
                "argus/config/loader.py",
                "    delete_chunk_rows: int = Field(default=2000, ge=0)",
                "    delete_chunk_rows: int = Field(default=0, ge=0)",
            ),
            (
                "argus/config/defaults.yaml",
                "  delete_chunk_rows: 2000",
                "  delete_chunk_rows: 0",
            ),
        ),
    ),
    Mutant(
        "delete_max_rows_per_tick",
        "틱당 삭제 상한 (없으면 큰 백로그를 한 틱에 다 지우려 한다)",
        (
            (
                "argus/storage/retention.py",
                "            if max_rows and total >= max_rows:\n",
                "            if False:  # MUTANT: 틱당 상한을 무시한다\n",
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
    # ---- 프로그램 사용시간. 셋 다 예외 없이 값만 틀어지는 종류다.
    Mutant(
        "usage_union",
        "사용시간은 이름 단위 구간 합집합 (같은 프로그램 여럿이 겹쳐도 한 번)",
        (
            (
                "argus/storage/rollup.py",
                "        merged = {name: _union(items) for name, items in spans.items()}\n",
                "        merged = {name: sorted(items) for name, items in spans.items()}"
                "  # MUTANT: 겹침을 그대로 더한다\n",
            ),
        ),
        note="합치지 않으면 관측 시간을 넘는 값이 나온다 (실측 chrome 1,073h / 관측 383h)",
    ),
    Mutant(
        "usage_session_clamp",
        "모든 실행 구간은 그 세션 안에서 끝난다",
        (
            (
                "argus/storage/rollup.py",
                "            end = min(end, session_end(began))\n",
                "            pass  # MUTANT: 세션 밖으로 새게 둔다\n",
            ),
        ),
        note="`exit` 하나가 큐 드롭으로 사라지면 몇 초짜리 프로세스가 며칠이 된다",
    ),
    Mutant(
        "usage_retention_watermark",
        "접히기 전의 `process_events` 는 지우지 않는다 (사용시간 영구 손실 경로)",
        (
            (
                "argus/storage/retention.py",
                '            ("process_events", s.events_days * 86400, "program_usage_daily"),\n',
                '            ("process_events", s.events_days * 86400, None),'
                "  # MUTANT: 보호 해제\n",
            ),
        ),
    ),
    Mutant(
        "usage_launch_grace",
        "기동 직후의 `start` 폭주는 실행 횟수가 아니다",
        (
            (
                "argus/storage/rollup.py",
                "_SESSION_START_GRACE_S = 5.0",
                "_SESSION_START_GRACE_S = 0.0",
            ),
        ),
        note="수집기는 첫 스냅샷에서 이미 떠 있던 프로세스 전부를 신규로 본다",
    ),
    # ---- 2026-08-12: 창 크기. 둘 다 **예외 없이 창만 커지는** 종류다 —
    # `--seconds` 스모크는 창이 1255px 로 뜨는 상태에서도 전부 정상이었다.
    Mutant(
        "page_scroll_area",
        "페이지는 스크롤 영역에 담긴다 (아니면 가장 빽빽한 페이지가 창 하한을 정한다)",
        (
            (
                "argus/desktop/app.py",
                "        self._stack.addWidget(area)",
                "        self._stack.addWidget(widget)  # MUTANT: 스크롤 영역 우회",
            ),
        ),
    ),
    Mutant(
        "collapsible_starts_collapsed",
        "접힌 묶음은 최소 높이에서 빠진다 (항상 펼치면 접는 의미가 없다)",
        (
            (
                "argus/desktop/widgets.py",
                "        body.setVisible(expanded)",
                "        body.setVisible(True)  # MUTANT: 항상 펼침",
            ),
        ),
    ),
    # ---- 2026-08-12: 프로그램 설명. 둘 다 예외 없이 조용하다 — 전자는 빈 설명이
    # 나올 뿐이고(그것도 한글 Windows 에서만), 후자는 그냥 느려진다.
    Mutant(
        "proginfo_locale_from_file",
        "exe 의 언어를 파일에서 읽는다 (박아 두면 한글 Windows 에서 빈손이 된다)",
        (
            (
                "argus/collector/proginfo.py",
                'query = f"\\\\StringFileInfo\\\\{language:04x}{codepage:04x}\\\\{key}"',
                'query = f"\\\\StringFileInfo\\\\040904b0\\\\{key}"  # MUTANT: 영어 고정',
            ),
        ),
    ),
    Mutant(
        "proginfo_skips_described",
        "이미 설명이 있는 이름은 다시 열지 않는다 (매 회차 수백 개 파일을 연다)",
        (
            (
                "argus/collector/proginfo.py",
                '"   AND (i.name IS NULL OR (i.description IS NULL AND i.attempts < ?))"',
                '"   AND (i.name IS NULL OR i.attempts < ?)"  # MUTANT: 항상 재조회',
            ),
        ),
    ),
    Mutant(
        "usage_user_only_filter",
        "사용시간 표가 사람이 쓰는 프로그램만 거른다 (아니면 상위가 전부 배경 서비스다)",
        (
            (
                "argus/dashboard/data.py",
                '        where += " AND i.foreground_seen = 1"',
                "        pass  # MUTANT: 필터 없음",
            ),
        ),
    ),
    Mutant(
        "foreground_mark_upserts",
        "포어그라운드 표시는 없는 행도 만든다 (UPDATE 만 하면 설명 없는 게임이 빠진다)",
        (
            (
                "argus/collector/proginfo.py",
                '                " VALUES (?, NULL, NULL, 0, ?, 1)"\n'
                '                " ON CONFLICT(name) DO UPDATE SET foreground_seen = 1",',
                '                " VALUES (?, NULL, NULL, 0, ?, 1)"\n'
                '                " ON CONFLICT(name) DO UPDATE SET foreground_seen = 1"'
                "  # MUTANT 아래 줄에서 무력화\n"
                '                " WHERE 0",',
            ),
        ),
    ),
    # ---- 2026-08-13: 풍선 무음. **셋 다 조용히 되돌아간다** — 알림은 어느 쪽이든
    # 똑같이 뜨고, 소리는 이 개발 PC 에서 원래 안 나서(실측 확인) 눈으로도 귀로도
    # 구분되지 않는다. 판정·반대쪽·배선을 따로 재는 이유가 그것이다.
    Mutant(
        "notify_sound_silent_default",
        "풍선은 기본이 무음이다 (소리가 나면 오탐 한 번의 비용이 훨씬 커진다 — 탐지 규칙 1)",
        (
            (
                "argus/ui/tray.py",
                "        if not self.notify_sound:\n            flags |= _NIIF_NOSOUND\n",
                "        # MUTANT: 무음 플래그 제거\n",
            ),
        ),
    ),
    Mutant(
        "notify_sound_respects_setting",
        "소리를 켜면 실제로 난다 (항상 무음으로 못 박으면 설정이 거짓말이 된다)",
        (
            (
                "argus/ui/tray.py",
                "        if not self.notify_sound:\n",
                "        if True:  # MUTANT: 설정 무시하고 항상 무음\n",
            ),
        ),
    ),
    # ---- 2026-08-13: 일일 리포트. **전부 조용히 되돌아간다** — 리포트는 어느 쪽이든
    # 그럴듯한 숫자를 보여 주고, 사용자에게는 그게 맞는지 확인할 방법이 없다.
    Mutant(
        "daily_report_skips_today",
        "진행 중인 날은 접지 않는다 (부분값이 확정으로 남는다)",
        (
            (
                "argus/report/builder.py",
                "        while cursor < today and len(days) < self.settings.daily_report_days_per_run:\n",
                "        while cursor <= today and len(days) < self.settings.daily_report_days_per_run:  # MUTANT\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_coverage_floor",
        "원본이 잘려 나간 날은 요약을 만들지 않는다 (영구 보존이라 나중에 못 고친다)",
        (
            (
                "argus/report/builder.py",
                "            if coverage < self.settings.daily_report_min_coverage:\n",
                "            if False:  # MUTANT: 잘린 날도 그대로 저장\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_gap_cap",
        "표본 사이의 공백은 사용시간이 아니다 (상한이 없으면 361.6h vs 실제 13.8h)",
        (
            (
                "argus/report/builder.py",
                "                gap = min(seen[i + 1][0] - ts, cap)\n",
                "                gap = seen[i + 1][0] - ts  # MUTANT: 상한 제거\n",
            ),
        ),
    ),
    # 표본 수가 아니라 **간격**을 더한다. 이것이 "같은 시각은 한 번만"과 "수집 주기가
    # 흔들려도 값이 같다"를 동시에 보장한다 — 표본당 고정값으로 바꾸면 둘 다 깨진다.
    # (중복을 미리 걸러내는 코드를 따로 뒀었는데, 값에 영향이 없어 무력화해도 아무
    #  테스트가 울지 않았다. 검증할 수 없는 방어라 빼고 이 mutant 로 대체했다.)
    Mutant(
        "daily_report_counts_gaps_not_samples",
        "표본 수가 아니라 간격을 더한다 (같은 시각의 행이 여럿이면 시간이 부풀어 오른다)",
        (
            (
                "argus/report/builder.py",
                "                gap = min(seen[i + 1][0] - ts, cap)\n",
                "                gap = cap  # MUTANT: 표본당 고정값\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_categories_from_config",
        "분류가 config 에서 온다 (코드에 박히면 YAML 을 고쳐도 안 바뀐다)",
        (
            (
                "argus/report/builder.py",
                "    for category, names in categories.items():\n",
                "    for category, names in {}.items():  # MUTANT: 설정 무시\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_keeps_unmapped",
        "분류에 없는 이름을 버리지 않는다 (남의 PC 는 매핑이 비어 있다)",
        (
            (
                "argus/report/builder.py",
                "                key = categorize(name, self.usage.categories)\n"
                "                by_category[key] = by_category.get(key, 0.0) + seconds\n",
                "                key = categorize(name, self.usage.categories)\n"
                "                if key == OTHER:  # MUTANT: 미분류를 버린다\n"
                "                    continue\n"
                "                by_category[key] = by_category.get(key, 0.0) + seconds\n",
            ),
        ),
    ),
    Mutant(
        "desktop_stop_all_covers_every_tab",
        "등록된 탭을 전부 멈춘다 (남은 QThread 가 프로세스를 죽인다 — 종료 코드만 비0)",
        (
            (
                "argus/desktop/app.py",
                "        for page in self._pages:\n",
                "        for page in self._pages[:2]:  # MUTANT: 일부만 정리\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_wiring",
        "상주가 이 롤업을 등록한다 (안 하면 리포트가 안 생기고 원본도 안 지워진다)",
        (
            (
                "argus/__main__.py",
                "            sup.add(DailyReportRollup(db, settings.rollup, settings.usage))\n",
                "            # MUTANT: 일일 리포트 롤업 등록 제거\n",
            ),
        ),
    ),
    Mutant(
        "daily_report_retention_hold",
        "보존이 이 롤업도 기다린다 (안 기다리면 원본이 먼저 지워진다)",
        (
            (
                "argus/storage/retention.py",
                '            ("process_metrics", s.process_hours * 3600, ("process_5m", "daily_report")),\n',
                '            ("process_metrics", s.process_hours * 3600, ("process_5m",)),  # MUTANT\n',
            ),
        ),
    ),
    Mutant(
        "notify_sound_wiring",
        "general.notify_sound 가 트레이에 닿는다 (안 닿으면 YAML 을 고쳐도 안 바뀐다)",
        (
            (
                "argus/__main__.py",
                "                notify_sound=settings.general.notify_sound,\n",
                "                # MUTANT: notify_sound 배선 제거\n",
            ),
        ),
    ),
    # ---- 2026-08-14 답 대기 알림(라벨 유입). 라벨 UI 는 08-09 에 있었는데 5일 뒤에도
    #      0건이었다 — 고친 것은 "무엇을 물을지"와 "어디서 물을지"라 둘 다 잰다.
    Mutant(
        "pending_answers_pick_unnotified_kinds",
        "미탐은 실측 근거가 있는 종류만 묻는다 (전부 물으면 밀린 것이 그 안에 묻힌다)",
        (
            (
                "argus/dashboard/data.py",
                "    kinds = [str(k).upper() for k in cfg.ask_unnotified_bottlenecks]\n",
                '    kinds = ["THERMAL", "CPU", "MEMORY", "IO", "NONE", "CONTENTION"]'
                "  # MUTANT: 종류를 안 가린다\n",
            ),
        ),
        note=(
            "2026-08-15 에 답 대기를 미탐까지 넓혔다. 근거는 발열 3/3 real 이고, "
            "종류를 안 가리면 최근 14일 미탐 68건이 통째로 밀려온다 — 08-14 에 확인된 "
            "'전부 내밀면 아무도 답하지 않는다'로 되돌아간다"
        ),
    ),
    Mutant(
        "health_poller_wakes_on_stop",
        "창을 닫을 때 상태 폴러가 남은 주기를 기다리지 않는다",
        (
            (
                "argus/desktop/app.py",
                "            self._wake.tryAcquire(1, int(self._interval_s * 1000))\n",
                "            self.msleep(int(self._interval_s * 1000))"
                "  # MUTANT: 못 깨우는 대기\n",
            ),
        ),
        note=(
            "2026-08-16 실측 — 창 종료 3.99초 중 3.01초가 이 폴러였다(5초 주기라 최악 5초). "
            "msleep 은 통째로 잠들어 취소 플래그를 못 본다"
        ),
    ),
    Mutant(
        "realtime_poller_wakes_on_stop",
        "창을 닫을 때 실시간 폴러가 남은 주기를 기다리지 않는다",
        (
            (
                "argus/desktop/pages/realtime.py",
                "            self._wake.tryAcquire(1, int(self.interval_s * 1000))\n",
                "            self.msleep(int(self.interval_s * 1000))"
                "  # MUTANT: 못 깨우는 대기\n",
            ),
        ),
        note="같은 실측의 나머지 0.98초. 폴러 둘을 따로 재야 한쪽만 되돌린 것이 잡힌다",
    ),
    Mutant(
        "chart_breaks_observation_gaps",
        "관측이 없던 구간을 선으로 잇지 않는다",
        (
            (
                "argus/desktop/widgets.py",
                "                xs, ys = break_gaps(timestamps, series)\n",
                "                xs, ys = list(timestamps), list(series)"
                "  # MUTANT: 공백을 안 끊는다\n",
            ),
        ),
        note=(
            "2026-08-16 자기 상태 화면에서 private 이 190→70MB 로 매끄럽게 내려가 보였는데 "
            "그 5시간에 표본이 없었다(22:00 재시작). 읽는 사람은 '서서히 줄었다'고 읽는다"
        ),
    ),
    Mutant(
        "chart_gap_uses_median_interval",
        "공백 기준을 표본 간격의 중앙값으로 잡는다 (평균은 공백에 끌려간다)",
        (
            (
                "argus/desktop/widgets.py",
                "    typical = deltas[len(deltas) // 2]\n",
                "    typical = sum(deltas) / len(deltas)  # MUTANT: 평균\n",
            ),
        ),
        note=(
            "평균은 공백 자신에게 끌려간다 — 5분 주기에 20분 공백이면 평균 480초라 "
            "문턱이 1440초가 되어 그 공백이 정상으로 통과한다. **큰 공백으로는 이 차이가 "
            "안 드러난다**(5시간이면 평균으로도 끊긴다). 1차 측정에서 그래서 안 잡혔다"
        ),
    ),
    Mutant(
        "answer_mark_follows_pending",
        "목록의 답 표시가 답 대기와 같은 규칙을 쓴다",
        (
            (
                "argus/desktop/pages/incidents.py",
                '    return "?" if incident.get("pending_answer") else ""\n',
                '    return "?" if incident.get("notified") else ""'
                "  # MUTANT: 화면이 규칙을 따로 갖는다\n",
            ),
        ),
        note=(
            "답 대기가 미탐 일부를 묻게 되자(2026-08-15) 목록은 '안 물어봄'이라 적고 "
            "답하기 버튼은 그 사건으로 데려가는 상태가 됐다"
        ),
    ),
    Mutant(
        "list_shows_notification_sent",
        "목록에서 알림 발송 여부가 보인다 (등급과 다르다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                '        "notified_mark": "●" if incident.get("notified") else "",\n',
                '        "notified_mark": "",  # MUTANT: 발송 여부를 안 보여준다\n',
            ),
        ),
        note=(
            "`경고` 인데 안 나간 것이 있고(억제·상위 사건에 물림) `정보` 라 안 나간 것도 "
            "있다. 목록에 등급만 있으면 상세를 하나씩 열어 봐야 한다"
        ),
    ),
    Mutant(
        "question_follows_notified",
        "알림이 안 나간 사건에 '이 알림이 쓸모 있었나'를 묻지 않는다",
        (
            (
                "argus/desktop/pages/incidents.py",
                "            _QUESTION_NOTIFIED if incident.get(\"notified\")"
                " else _QUESTION_UNNOTIFIED\n",
                "            _QUESTION_NOTIFIED  # MUTANT: 미탐에도 같은 문장을 쓴다\n",
            ),
        ),
        note="오지도 않은 알림을 떠올리라고 하면 답이 짐작이 된다",
    ),
    Mutant(
        "pending_answers_unnotified_window",
        "미탐에는 더 짧은 창을 준다 (안 나간 사건은 기억 단서가 없다)",
        (
            ("argus/config/defaults.yaml",
             "  ask_unnotified_window_days: 7\n",
             "  ask_unnotified_window_days: 14\n"),
        ),
        note=(
            "발송된 알림은 그때 풍선이라도 떴지만 안 나간 사건은 흔적이 없다. "
            "같은 14일을 주면 9일 이상 지난 발열 18건이 한꺼번에 밀려온다(2026-08-15 실측)"
        ),
    ),
    Mutant(
        "pending_answers_window",
        "답 대기 기간 상한 (기억하지 못하는 알림에 붙인 라벨은 근거가 못 된다)",
        (
            # 2026-08-15 에 값이 config 로 갔다 — 무력화도 거기서 한다.
            ("argus/config/defaults.yaml", "  window_days: 14\n", "  window_days: 3650\n"),
        ),
    ),
    Mutant(
        "pending_count_reaches_the_status_line",
        "답 대기 수가 health() 에 실린다 (안 실리면 사건 탭을 연 사람만 알게 된다)",
        (
            (
                "argus/dashboard/data.py",
                '        "unlabeled": len(unlabeled_notified()),\n',
                "        # MUTANT: 답 대기 수를 맨 윗줄에 안 준다\n",
            ),
        ),
    ),
    Mutant(
        "answer_button_wiring",
        "답하기 버튼이 실제로 뜬다 (판정이 맞아도 안 뜨면 라벨은 계속 0건이다)",
        (
            (
                "argus/desktop/app.py",
                "        self._label_btn.setVisible(prompt is not None)\n",
                "        self._label_btn.setVisible(False)  # MUTANT: 버튼을 안 띄운다\n",
            ),
        ),
    ),
    Mutant(
        "answer_mark_separates_unasked",
        "물은 적 없는 사건은 빈칸 (전부 물음표면 밀린 것이 구분되지 않는다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                '    return "?" if incident.get("notified") else ""\n',
                '    return "?"  # MUTANT: 알림 안 나간 것도 답 대기로\n',
            ),
        ),
    ),
    Mutant(
        "answering_clears_pending_cache",
        "답을 저장하면 답 대기 캐시도 비운다 (안 비우면 답이 안 저장된 것처럼 보인다)",
        (
            (
                "argus/dashboard/data.py",
                "    unlabeled_notified.cache_clear()\n    health.cache_clear()\n",
                "    # MUTANT: 답 대기·맨 윗줄 캐시를 안 비운다\n",
            ),
        ),
    ),
    # ---- 2026-08-14 커넥션 닫기. 정리 최적화 하나가 웜 내보내기 자식을 통째로
    #      실패로 만들었다(02:19, `PRAGMA optimize` 의 database is locked).
    Mutant(
        "close_survives_locked_optimize",
        "정리 최적화 실패가 닫기를 죽이지 않는다 (성공한 작업이 실패로 보고된다)",
        (
            (
                "argus/storage/hot.py",
                "                except sqlite3.OperationalError as exc:",
                "                except sqlite3.ProgrammingError as exc:  # MUTANT: 락은 안 삼킨다",
            ),
        ),
    ),
    Mutant(
        "close_does_not_swallow_everything",
        "삼키는 것은 OperationalError 뿐 (범위를 넓히면 진짜 고장이 조용해진다)",
        (
            (
                "argus/storage/hot.py",
                "                except sqlite3.OperationalError as exc:",
                "                except BaseException as exc:  # MUTANT: 전부 삼킨다",
            ),
        ),
    ),
    Mutant(
        "answer_boxes_mutually_exclusive",
        "정상·비정상 상자는 서로를 끈다 (둘 다 켜지면 무엇을 답했는지 알 수 없다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                "        self._show_label(label)\n"
                "        self._clear_btn.setEnabled(label is not None)\n",
                "        # MUTANT: 방금 켠 것만 남기지 않는다\n",
            ),
        ),
    ),
    Mutant(
        "unknown_is_not_a_false_positive_denominator",
        "모르겠음은 오탐 비율의 분모가 아니다 (답을 모을수록 문제가 작아 보인다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                '        labeled = [r for r in rows if r.get("user_label") in ("normal", "real")]\n',
                '        labeled = [r for r in rows if r.get("user_label")]  # MUTANT\n',
            ),
        ),
    ),
    Mutant(
        "pending_answers_skip_injections",
        "결함 주입 구간은 묻지 않는다 (내가 만든 부하에 대한 답은 실사용 근거가 아니다)",
        (
            (
                "argus/dashboard/data.py",
                '        f" AND NOT {_DURING_INJECTION}"\n',
                "        # MUTANT: 주입 구간도 답하라고 내민다\n",
            ),
        ),
    ),
    Mutant(
        "open_injection_is_not_endless",
        "닫히지 않은 주입을 무한 구간으로 읽지 않는다 (그 뒤 알림이 전부 사라진다)",
        (
            (
                "argus/dashboard/data.py",
                " AND COALESCE(f.ts_end, f.ts_start) >= i.ts_start)",
                " AND COALESCE(f.ts_end, 1e18) >= i.ts_start)  -- MUTANT",
            ),
        ),
    ),
    Mutant(
        "answer_box_signal_is_click_not_toggle",
        "상자 상태를 비추는 것이 저장을 부르지 않는다 (훑기만 해도 labeled_at 이 갱신된다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                '        self._normal_box.clicked.connect(lambda on: self._label("normal" if on else None))\n',
                '        self._normal_box.toggled.connect(lambda on: self._label("normal" if on else None))  # MUTANT\n',
            ),
        ),
    ),
    # ---- 2026-08-14 자동 라벨. 사람 답과 **같은 칸에 섞이지 않는 것**이 요점이라,
    #      판정 로직 둘과 오염 방지 셋을 잰다.
    Mutant(
        "autolabel_never_overwrites_human",
        "기계 답이 사람 답을 덮지 않는다 (덮으면 기계가 매긴 것으로 기계를 고치게 된다)",
        (
            (
                "argus/decide/autolabel.py",
                '    if row["user_label"]:\n        return Verdict(None, "사람이 이미 답했다")\n',
                "    # MUTANT: 사람 답을 덮는다\n",
            ),
        ),
    ),
    Mutant(
        "autolabel_hardware_limit_is_real",
        "열 스로틀은 비정상으로 간다 (실측 라벨 3/3 이 여기였다)",
        (
            (
                "argus/decide/autolabel.py",
                '        return Verdict(LABEL_REAL, f"하드웨어가 실제로 성능을 깎았다 ({bottleneck})")',
                '        return Verdict(LABEL_NORMAL, "MUTANT")',
            ),
        ),
    ),
    Mutant(
        "autolabel_needs_foreground_history",
        "포어그라운드 이력이 없으면 정상으로 넘기지 않는다 (백그라운드는 내가 돌린 작업이 아니다)",
        (
            (
                "argus/decide/autolabel.py",
                "    if not foreground.get(name.lower()):\n"
                '        return Verdict(None, f"{name} 을 직접 띄운 적이 있는지 모른다")\n',
                "    # MUTANT: 아무 프로세스나 내가 띄운 앱으로 친다\n",
            ),
        ),
    ),
    Mutant(
        "autolabel_skips_unnotified",
        "안 나간 알림은 판정하지 않는다 (사건이 알림의 세 배라 타일이 기계 답으로 덮인다)",
        (
            (
                "argus/decide/autolabel.py",
                '        return Verdict(None, "알림이 나가지 않았다")\n',
                "        pass  # MUTANT: 안 나간 알림도 판정한다\n",
            ),
        ),
    ),
    Mutant(
        "autolabel_is_marked_as_machine_in_the_ui",
        "화면이 기계 답을 사람 답과 구분해 보인다 (섞이면 오탐 비율이 누구 판단인지 모른다)",
        (
            (
                "argus/desktop/pages/incidents.py",
                '    if auto == "normal":\n        return "·정상"\n',
                '    if auto == "normal":\n        return "정상"  # MUTANT\n',
            ),
        ),
    ),
    # ---- 회수 스냅샷 (2026-08-17, 두 번째 기계를 붙이면서)
    #
    # 이 경로는 **남의 PC 에서 매일 자동으로 돌고 아무도 화면을 보지 않는다.**
    # 조용히 틀리면 몇 주 뒤 "데이터가 이상한데 왜인지 모르겠다"로만 나타난다.
    Mutant(
        "snapshot_excludes_network_destinations",
        "스냅샷에 네트워크 목적지를 담지 않는다 (기계 밖으로 나가는 유일한 물건이다)",
        (
            (
                "argus/storage/findings.py",
                '    "net_connections": "**네트워크 목적지가 들어 있다.** 설계 규칙 5 — 담지 않는 것이 익명화다",\n',
                "",
            ),
            (
                "argus/storage/findings.py",
                '    "warm_exports",\n)',
                '    "warm_exports",\n    "net_connections",  # MUTANT: 목적지를 담는다\n)',
            ),
        ),
    ),
    Mutant(
        "snapshot_does_not_write_to_source",
        "스냅샷을 뽑을 때 원본에 쓰지 않는다 (상주가 그 DB 를 쓰는 중이다)",
        (
            (
                "argus/storage/findings.py",
                '        conn.execute("BEGIN")',
                '        conn.execute("BEGIN")\n        conn.execute("PRAGMA user_version=999")  # MUTANT',
            ),
        ),
    ),
    Mutant(
        "snapshot_replaces_instead_of_appending",
        "다시 뽑으면 지난 회차를 지운다 (이어붙이면 언제 것인지 모르는 스냅샷이 된다)",
        (
            (
                "argus/storage/findings.py",
                "        if stale.exists():\n            stale.unlink()",
                "        if False:\n            stale.unlink()  # MUTANT",
            ),
        ),
    ),
    Mutant(
        "snapshot_prune_keeps_newest",
        "오래된 스냅샷 정리가 최신 N개를 남긴다 (거꾸로면 방금 뽑은 것을 지운다)",
        (
            (
                "argus/storage/findings.py",
                "        key=lambda p: p.stat().st_mtime,\n        reverse=True,",
                "        key=lambda p: p.stat().st_mtime,\n        reverse=False,  # MUTANT",
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

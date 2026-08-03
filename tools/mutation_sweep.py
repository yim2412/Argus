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
        "지문 자격 — 6시간 미만인 날은 하루로 세지 않는다",
        (("argus/detection/fingerprint.py", "MIN_DAY_HOURS = 6.0", "MIN_DAY_HOURS = 0.0"),),
        note="구현이 아직 없다(PLAN §8 의 1번). xfail(strict) 하나가 이 사실을 고정한다",
        expect_caught=False,
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
        "dashboard_pythonpath",
        "대시보드에 sys.path 를 물려준다 (base 인터프리터엔 streamlit 이 없다)",
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
        "대시보드가 곧바로 죽으면 사용자에게 말한다 (띄운 것과 뜬 것은 다르다)",
        (
            (
                "argus/ui/tray.py",
                '        self.notify("Argus", f"대시보드를 열지 못했습니다 — {reason}", "warning")\n',
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
                "        if not self.settings.notify_enabled or self.notifier is None:\n",
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

"""저장된 사건에 **새 등급 축을 소급 적용해 분포를 본다.** DB 를 수정하지 않는다.

왜 먼저 재는가: 등급 문턱에 근거가 없으면 넣지 말아야 한다(CLAUDE.md — "수치 없이
모델을 추가하지 않는다"). 2026-07-30 에 배수 문턱 5 를 넣지 않고 멈춘 것이 같은 자리다.
사건과 신호가 이미 DB 에 있으므로, 문턱 후보를 바꿔 가며 **무엇이 어떻게 재분류되는지**
그대로 볼 수 있다.

**`incidents.severity` 를 덮어쓰지 않는다.** 덮어쓰면 "고치기 전에 어땠는지"가 사라져
before/after 를 다시 못 잰다.

사용:
    python tools/grade_probe.py                       # 전체 기간
    python tools/grade_probe.py --sweep               # 문턱 후보를 훑는다
    python tools/grade_probe.py --risk-warning 1.5
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argus.config.loader import SeveritySettings, load_settings  # noqa: E402
from argus.decide.severity import combine, leak_risk  # noqa: E402
from argus.detection.base import SEVERITY_ORDER  # noqa: E402
from argus.detection.fingerprint import STAT_FOR, load as load_fingerprints  # noqa: E402
from argus.storage.hot import Database  # noqa: E402

WORSE_FIRST = ("critical", "warning", "info")

# 저장된 지문 대신 즉석에서 다시 만들지. `--rebuild` 가 켠다.
REBUILD = False


def _fmt(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _fresh_fingerprints(db: Database) -> dict:
    """지문을 **즉석에서** 다시 만든다. 저장하지 않는다.

    저장된 지문은 마지막 빌드 시점의 것이라, 제외 규칙을 고친 직후에는 낡은 값이다.
    DB 에 쓰면 상주 인스턴스와 경합하고 이 도구가 읽기 전용이 아니게 된다.
    """
    from argus.detection.fingerprint import build, fault_windows

    settings = load_settings().fingerprint
    prints = build(
        min_days=settings.min_days,
        min_buckets=settings.min_buckets,
        exclude=fault_windows(db),
    )
    return {(fp.name, fp.stat): fp for fp in prints}


def grade_leak_signals(db: Database, settings: SeveritySettings) -> list[dict]:
    """누수 신호마다 (지금 등급, 새 등급, 근거). 위험 축이 실제로 무엇을 가르는지 본다.

    지문 조회는 `procleak` 이 쓰는 것과 **같은 키**여야 한다 — `(이름, stat)` 이고
    stat 은 지표마다 다르다(핸들은 `handles_max`, RSS 는 `rss_p95`). 처음에 `(이름,
    레짐)` 으로 찾아 22건 전부 "지문 없음"이 나왔고, 그건 위험 축이 아무것도 가르지
    못한다는 뜻으로 읽힐 뻔했다.
    """
    prints = _fresh_fingerprints(db) if REBUILD else load_fingerprints(db)

    out = []
    rows = db.query("SELECT * FROM anomaly_signals WHERE detector = 'procleak' ORDER BY ts")
    for row in rows:
        features = json.loads(row["features"] or "{}")
        metric = features.get("metric", "")
        name = (features.get("process") or "").lower()
        stat = STAT_FOR.get(metric)
        found = prints.get((name, stat)) if stat else None
        p99 = float(found.p99) if found is not None else None

        severity, reason = leak_risk(
            last=float(features.get("last") or 0.0),
            fingerprint_p99=p99,
            monotonic=float(features.get("monotonic") or 0.0),
            settings=settings,
        )
        grade = combine((("info"), ""), (severity, reason))
        out.append(
            {
                "ts": row["ts"],
                "process": name,
                "metric": metric,
                "last": features.get("last"),
                "p99": p99,
                "ratio": features.get("ratio"),
                "monotonic": features.get("monotonic"),
                "before": row["severity"],
                "after": grade.severity,
                "reason": grade.reason,
            }
        )
    return out


def _print_signals(graded: list[dict]) -> None:
    print(f"\n누수 신호 {len(graded)}건 — 위험 축 적용")
    print(f"  {'시각':>11}  {'프로세스':<12} {'지표':<8} {'현재값':>9} {'p99':>9} "
          f"{'위치':>7}  {'지금':<8} → {'새 등급':<8} 근거")
    for g in graded:
        position = f"{g['last'] / g['p99'] * 100:.0f}%" if g["p99"] else "—"
        p99 = f"{g['p99']:.0f}" if g["p99"] else "없음"
        changed = "*" if g["before"] != g["after"] else " "
        print(
            f"  {_fmt(g['ts']):>11}  {g['process'][:12]:<12} {g['metric']:<8} "
            f"{g['last']:>9.0f} {p99:>9} {position:>7}  "
            f"{g['before']:<8} →{changed}{g['after']:<8} {g['reason']}"
        )

    print("\n  등급 이동")
    moves = collections.Counter((g["before"], g["after"]) for g in graded)
    for (before, after), count in sorted(moves.items()):
        arrow = "그대로" if before == after else "이동"
        print(f"    {before:<8} → {after:<8} {count:>3}건  ({arrow})")


def _alarm_delta(graded: list[dict]) -> None:
    """**알림 대상이 늘어나는지.** 이게 늘면 등급을 정리하려다 알림을 늘린 것이다."""
    def alarming(severity: str) -> bool:
        return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get("warning", 1)

    before = sum(1 for g in graded if alarming(g["before"]))
    after = sum(1 for g in graded if alarming(g["after"]))
    print(f"\n  알림 대상(warning↑): {before}건 → {after}건  ({after - before:+d})")


def main() -> int:
    parser = argparse.ArgumentParser(description="등급 축 소급 적용 (읽기 전용)")
    parser.add_argument("--risk-warning", type=float, help="위험 축 warning 문턱 (p99 배수)")
    parser.add_argument("--risk-critical", type=float, help="위험 축 critical 문턱")
    parser.add_argument("--sweep", action="store_true", help="문턱 후보를 훑는다")
    parser.add_argument(
        "--rebuild", action="store_true", help="지문을 즉석에서 다시 만든다 (저장 안 함)"
    )
    args = parser.parse_args()

    global REBUILD
    REBUILD = args.rebuild

    base = load_settings().severity
    overrides = {}
    if args.risk_warning is not None:
        overrides["risk_warning_ratio"] = args.risk_warning
    if args.risk_critical is not None:
        overrides["risk_critical_ratio"] = args.risk_critical
    settings = base.model_copy(update=overrides)

    with Database() as db:
        graded = grade_leak_signals(db, settings)
        if not graded:
            print("누수 신호가 없다 — 잴 것이 없다.")
            return 1

        if args.sweep:
            print("문턱 스윕 (위험 축)")
            print(f"  {'warning 문턱':>12} {'critical 문턱':>13}  info/warn/crit   알림대상")
            for warning_at in (0.5, 0.8, 1.0, 1.5, 2.0):
                for critical_at in (2.0, 3.0):
                    if critical_at <= warning_at:
                        continue
                    swept = grade_leak_signals(
                        db,
                        settings.model_copy(
                            update={
                                "risk_warning_ratio": warning_at,
                                "risk_critical_ratio": critical_at,
                            }
                        ),
                    )
                    counts = collections.Counter(g["after"] for g in swept)
                    alarms = sum(
                        1
                        for g in swept
                        if SEVERITY_ORDER.get(g["after"], 0) >= SEVERITY_ORDER["warning"]
                    )
                    print(
                        f"  {warning_at:>12.1f} {critical_at:>13.1f}  "
                        f"{counts['info']:>4}/{counts['warning']:>4}/{counts['critical']:>4}"
                        f"   {alarms:>6}건"
                    )
            return 0

        _print_signals(graded)
        _alarm_delta(graded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

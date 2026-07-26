"""엔트리포인트.

    python -m argus              # 상주 실행 (Ctrl+C 로 종료)
    python -m argus --check      # 기동 점검만 하고 종료
    python -m argus --duration 30

Phase 0 에서는 자기 계측만 돈다. 실제 메트릭 수집기는 Phase 1 에서 붙는다.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from . import __version__
from .config.loader import ConfigError, load_settings
from .logging_setup import get_logger, setup, write_crash
from .machine.calibration import ensure_profile
from .machine.capabilities import load_or_detect
from .paths import ENV_DATA_DIR, data_dir, db_path
from .runtime.budget import BudgetGuard
from .runtime.selftel import BudgetMonitor, SelfTelemetry
from .runtime.supervisor import Supervisor
from .storage.hot import Database

log = get_logger("argus")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="PC 성능 이상 탐지 — 상주 모니터",
    )
    parser.add_argument("--version", action="version", version=f"argus {__version__}")
    parser.add_argument(
        "--data-dir",
        help=f"데이터 저장 위치를 덮어쓴다 (환경변수 {ENV_DATA_DIR} 와 동일)",
    )
    parser.add_argument("--log-level", help="콘솔 로그 레벨 (기본: 설정 파일 값)")
    parser.add_argument(
        "--recalibrate",
        action="store_true",
        help="하드웨어 기준선을 다시 잰다",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="기동 점검(설정·권한·DB·기준선)만 하고 종료한다",
    )
    parser.add_argument(
        "--duration",
        type=float,
        metavar="SECONDS",
        help="지정한 초만큼 돌고 자동 종료한다 (테스트용)",
    )
    return parser.parse_args(argv)


def _print_startup_report(settings, caps, profile, db: Database) -> None:
    """기동 시 한 화면 요약. 남의 PC 에서 무엇이 켜지고 꺼졌는지 바로 보이게 한다."""
    print(f"Argus {__version__}")
    print(f"  데이터   : {data_dir()}")
    print(f"  DB       : {db_path().name}  (스키마 v{db.schema_version()}, {db.size_bytes():,} bytes)")
    print(f"  OS       : {caps.os['system']} {caps.os['release']}  Python {caps.os['python']}")
    print("  계측 소스:")
    for line in caps.summary():
        print(f"  {line}")
    print(
        f"  기준선   : CPU {profile.cpu.get('single_thread_mops')}Mops/s · "
        f"RAM {profile.memory.get('total_gb')}GB · "
        f"디스크 {profile.disk.get('media_type')} "
        f"{profile.disk.get('seq_write_mbps')}MB/s"
    )
    print(
        f"  예산     : CPU {settings.budget.cpu_percent}% · "
        f"RSS {settings.budget.rss_mb}MB"
    )


def run(args: argparse.Namespace) -> int:
    if args.data_dir:
        os.environ[ENV_DATA_DIR] = args.data_dir

    # 설정을 읽어야 로그 레벨을 알 수 있고, 로그를 켜야 설정 오류를 남길 수 있다.
    # 순환을 피하려고 로깅을 먼저 최소 구성한 뒤 레벨만 나중에 맞춘다.
    try:
        settings = load_settings()
    except ConfigError as e:
        # 설정 오류는 사용자가 고쳐야 하는 것이므로 트레이스백 없이 사람 말로만 보여준다.
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    level = args.log_level or settings.general.log_level
    setup(level=level, console=settings.general.console_log)
    log.info("Argus 시작", extra={"version": __version__, "data_dir": str(data_dir())})

    caps = load_or_detect()
    profile = ensure_profile(
        disk_bench_mb=settings.calibration.disk_bench_mb,
        reuse_days=settings.calibration.reuse_days,
        force=args.recalibrate,
    )

    db = Database().open()
    try:
        _print_startup_report(settings, caps, profile, db)

        if args.check:
            print("[OK] 기동 점검 통과")
            return 0

        guard = BudgetGuard(settings.budget)
        sup = Supervisor(multiplier_fn=lambda: guard.multiplier)
        sup.add(BudgetMonitor(guard))
        if settings.self_telemetry.enabled:
            sup.add(SelfTelemetry(db, guard, interval_s=settings.self_telemetry.interval_s))

        sup.install_signal_handlers()
        sup.start()

        if args.duration:
            # 지정 시간 뒤 종료. 타이머 스레드로 걸어 Ctrl+C 도 계속 받게 둔다.
            threading.Timer(args.duration, sup.stop).start()
            print(f"  {args.duration}초 동안 실행합니다. (Ctrl+C 로 조기 종료)")
        else:
            print("  실행 중입니다. Ctrl+C 로 종료합니다.")

        started = time.time()
        sup.wait()
        sup.stop()

        rows = db.query("SELECT COUNT(*) AS c FROM self_telemetry")[0]["c"]
        log.info(
            "Argus 종료",
            extra={"uptime_s": round(time.time() - started, 1), "self_telemetry_rows": rows},
        )
        print(f"  종료 — 가동 {time.time() - started:.1f}초, self_telemetry 누적 {rows}행")
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        # 기동 도중(수퍼바이저 시작 전)에 눌린 경우.
        return 130
    except Exception as e:
        path = write_crash(e, context="main")
        log.exception("치명적 오류")
        print(f"[FAIL] 예기치 못한 오류로 종료했습니다. 크래시 기록: {path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""엔트리포인트.

    python -m argus              # 상주 실행 (Ctrl+C 로 종료)
    python -m argus --check      # 기동 점검만 하고 종료
    python -m argus --duration 30

Phase 0 에서는 자기 계측만 돈다. 실제 메트릭 수집기는 Phase 1 에서 붙는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .branding import set_app_id
from .collector.gpu import GpuCollector
from .collector.network import NetworkCollector
from .collector.process import ProcessCollector
from .collector.system import SystemCollector
from .config.loader import ConfigError, load_settings
from .logging_setup import get_logger, setup, write_crash
from .machine.calibration import ensure_profile
from .machine.capabilities import load_or_detect
from .paths import ENV_DATA_DIR, data_dir, db_path
from .runtime.budget import BudgetGuard
from .runtime.gapmon import GapMonitor, gap_event_row
from .runtime.livecfg import LiveConfig, LiveConfigWatcher
from .runtime.selftel import BudgetMonitor, SelfTelemetry
from .runtime.session import detect_unclean_shutdown
from .runtime.stopfile import StopFileMonitor, clear_stale, request_stop
from .runtime.singleton import AlreadyRunning, InstanceLock
from .runtime.stats import STATS
from .runtime.supervisor import Supervisor
from .storage.hot import Database
from .storage.queue import Sample, SampleQueue
from .ui.tray import TrayIcon

SYSTEM_EVENT_COLUMNS = ("ts", "event", "gap_seconds", "detail")
from .storage.retention import Retention
from .storage.writer import BatchWriter

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
    parser.add_argument(
        "--stop",
        action="store_true",
        help="돌고 있는 상주 인스턴스에 정상 종료를 요청하고 나간다",
    )
    parser.add_argument(
        "--export-warm",
        action="store_true",
        help="끝난 날짜를 웜 스토어로 내보내고 나간다 (상주가 자식 프로세스로 부른다)",
    )
    parser.add_argument(
        "--allow-multi",
        action="store_true",
        help="중복 실행 차단을 끈다 (같은 DB 를 두 프로세스가 쓰게 되므로 진단용)",
    )
    parser.add_argument(
        "--export-findings",
        metavar="PATH",
        help="판정용 테이블만 스냅샷 파일로 뽑고 나간다 (다른 기계의 데이터를 회수할 때)",
    )
    parser.add_argument(
        "--snapshot-keep",
        type=int,
        default=30,
        metavar="N",
        help="스냅샷을 뽑은 뒤 같은 폴더에 남길 개수 (기본 30, 0 이면 정리하지 않는다)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help="화면 대신 이 파일에 결과를 쓴다 (--check·--export-findings). "
        "창 없는 빌드에는 stdout 이 없어 이것이 유일한 통로다",
    )
    return parser.parse_args(argv)


def _emit(lines: list[str], out: str | None) -> None:
    """결과를 화면과 파일 양쪽으로 내보낸다.

    **둘 다 실패해도 프로그램을 죽이지 않는다.** 결과를 보여 주는 일이 하려던
    일 자체를 실패시키면 안 된다 — 특히 창 없는 빌드에서는 화면 쪽이 항상
    조용히 아무 데도 가지 않는다.
    """
    text = "\n".join(lines)
    try:
        print(text)
    except (OSError, ValueError, AttributeError):
        # windowed 빌드의 stdout 은 없거나 닫혀 있다. 파일 쪽이 본체다.
        pass
    if not out:
        return
    try:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("결과 파일 쓰기 실패", extra={"path": out, "error": str(exc)})


def _export_warm(settings) -> int:
    """`--export-warm`. 끝난 날짜를 내보내고 바로 나간다.

    **이 프로세스는 곧 죽으므로 pyarrow 가 남기는 366MB 도 함께 사라진다.** 그것이
    분리한 이유의 전부다. 결과는 stdout 에 JSON 한 줄로 내보내 부모가 로그에 옮긴다 —
    자식이 같은 로그 파일에 직접 쓰면 두 프로세스가 한 파일을 두고 다툰다.
    """
    from .storage.warm import WarmStore

    with Database() as db:
        exported = WarmStore(db, settings.warm).export_pending()
    print(json.dumps({"exported": exported}, ensure_ascii=False))
    return 0


def _export_findings(args) -> int:
    """`--export-findings`. 판정용 스냅샷을 뽑고 바로 나간다.

    **상주가 도는 중에 실행된다.** 다른 기계에서는 이것이 매일 스케줄로 돌고,
    그 기계의 상주는 계속 수집 중이다. 그래서 원본에는 쓰지 않고, 인스턴스 락도
    잡지 않는다(`--export-warm` 과 같은 이유로 락보다 앞에서 처리된다).

    결과를 `--out` 파일로 남기는 것이 중요하다 — 창 없는 빌드에서 이 실행은
    화면에 아무것도 남기지 않으므로, 실패했는지 성공했는지 볼 방법이 그것뿐이다.
    """
    from .storage.findings import export_findings, prune_snapshots, resolve_out_path

    # 폴더를 주면 날짜가 든 이름을 짓는다. 스케줄 작업은 인자가 고정 문자열이라
    # 날짜를 넣을 방법이 이것뿐이고, 파일명이 곧 그 기계의 생존 신호가 된다.
    out_path = resolve_out_path(args.export_findings)
    try:
        result = export_findings(out_path)
    except Exception as exc:  # noqa: BLE001 — 원인을 사람이 읽을 수 있게 남기고 나간다
        log.exception("스냅샷 내보내기 실패")
        _emit([f"[FAIL] 스냅샷 내보내기 실패: {exc}"], args.out)
        return 1

    removed = prune_snapshots(out_path.parent, args.snapshot_keep) if args.snapshot_keep else []

    lines = [
        "[OK] 스냅샷을 내보냈습니다",
        f"  파일   : {result['path']}",
        f"  크기   : {result['size_bytes']:,} bytes",
        f"  행 수  : {result['rows_total']:,}",
        f"  소요   : {result['elapsed_s']}s",
    ]
    if result["missing"]:
        lines.append(f"  없던 표: {', '.join(result['missing'])}")
    if removed:
        lines.append(f"  정리   : 오래된 스냅샷 {len(removed)}개 삭제")
    _emit(lines, args.out)
    return 0


def _startup_report_lines(settings, caps, profile, db: Database) -> list[str]:
    """기동 시 한 화면 요약. 남의 PC 에서 무엇이 켜지고 꺼졌는지 바로 보이게 한다.

    **화면에 찍지 않고 줄 목록을 돌려준다.** 창 없는 빌드(`console=False`)에는
    stdout 이 없어 `print` 가 아무 데도 가지 않기 때문이다 — 남의 PC 에서 점검
    결과를 볼 유일한 통로가 파일이라, 같은 내용을 파일로도 남길 수 있어야 한다.
    """
    lines = [
        f"Argus {__version__}",
        f"  데이터   : {data_dir()}",
        f"  DB       : {db_path().name}  (스키마 v{db.schema_version()}, {db.size_bytes():,} bytes)",
        f"  OS       : {caps.os['system']} {caps.os['release']}  Python {caps.os['python']}",
        "  계측 소스:",
    ]
    lines.extend(f"  {line}" for line in caps.summary())
    lines.append(
        f"  기준선   : CPU {profile.cpu.get('single_thread_mops')}Mops/s · "
        f"RAM {profile.memory.get('total_gb')}GB · "
        f"디스크 {profile.disk.get('media_type')} "
        f"{profile.disk.get('seq_write_mbps')}MB/s"
    )
    lines.append(
        f"  예산     : CPU {settings.budget.cpu_percent}% · "
        f"RSS {settings.budget.rss_mb}MB"
    )
    return lines


def _build_collectors(settings, queue: SampleQueue, caps) -> list:
    """설정과 이 PC 의 능력에 맞춰 수집기를 조립한다.

    쓸 수 없는 것은 애초에 만들지 않는다. NVIDIA GPU 가 없는데 GPU 수집기를 띄워 매 초
    실패시키는 것은 낭비이고, 로그만 더럽힌다.
    """
    collector_settings = settings.collector
    collectors: list = [
        SystemCollector(
            queue,
            interval_s=collector_settings.system_interval_s,
            pdh_enabled=collector_settings.pdh_enabled and caps.pdh.available,
        )
    ]

    if collector_settings.gpu_enabled and caps.nvml.available:
        collectors.append(
            GpuCollector(
                queue,
                interval_s=collector_settings.gpu_interval_s,
                recover_after_failures=collector_settings.gpu_recover_after_failures,
                recover_backoff_s=collector_settings.gpu_recover_backoff_s,
            )
        )

    process_settings = collector_settings.process
    if process_settings.enabled:
        collectors.append(
            ProcessCollector(
                queue,
                collect_interval_s=process_settings.collect_interval_s,
                top_cpu=process_settings.top_cpu,
                top_memory=process_settings.top_memory,
                top_handle_growth=process_settings.top_handle_growth,
                full_store_interval_s=process_settings.full_store_interval_s,
                fallback_interval_s=process_settings.fallback_interval_s,
            )
        )

    network_settings = collector_settings.network
    if network_settings.enabled:
        collectors.append(
            NetworkCollector(
                queue,
                interval_s=network_settings.interval_s,
                max_rows=network_settings.max_rows_per_snapshot,
            )
        )

    return collectors


def _print_shutdown_report(db: Database, started: float) -> None:
    """종료 시 무엇이 얼마나 쌓였는지. 눈으로 확인 가능한 마지막 지점이다."""
    uptime = time.time() - started
    tables = (
        "metrics_raw",
        "gpu_metrics",
        "process_metrics",
        "process_events",
        "net_connections",
        "system_events",
        "self_telemetry",
    )
    print(f"  종료 — 가동 {uptime:.1f}초")
    for table in tables:
        try:
            count = db.query(f"SELECT COUNT(*) AS c FROM {table}")[0]["c"]
        except Exception:
            continue
        print(f"    {table:18} {count:>10,}행")
    snapshot = STATS.snapshot()
    print(f"    {'DB 크기':18} {db.size_bytes():>10,} bytes")
    print(f"    {'버린 행(drop)':18} {snapshot.drop_count:>10,}  (0 이어야 정상)")


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
    # **자식은 "Argus 시작"을 남기지 않는다.** 웜 내보내기는 시간마다 도는데, 그때마다
    # 기동 로그가 쌓이면 로그로 세션 경계를 세는 일이 전부 틀어진다 — 08-12 에 RSS
    # 급증 원인을 찾다가 정확히 그 혼란을 겪었다(기동이 아닌 줄이 기동으로 보였다).
    # 스냅샷 내보내기도 같은 이유로 제외한다 — 다른 기계에서 **매일** 도는 자식이라,
    # 세면 하루 한 번씩 있지도 않은 기동이 생긴다.
    if not (args.export_warm or args.export_findings):
        log.info("Argus 시작", extra={"version": __version__, "data_dir": str(data_dir())})

    # 트레이 창(`TrayIcon`)이 만들어지기 전에 정체를 밝혀 둔다. 창이 생긴 뒤에 부르면
    # 이미 정해진 그룹이 바뀌지 않는다 — 알림 발신자가 파이썬으로 남는다.
    set_app_id()

    # 종료 요청은 신호 파일 하나로 끝난다. 락도 DB 도 필요 없다 — 상주 인스턴스가
    # 그것을 보고 스스로 정상 경로로 내려간다.
    if args.stop:
        path = request_stop()
        print(f"  종료를 요청했습니다: {path}")
        print("  상주 인스턴스가 몇 초 안에 스스로 종료합니다. 없으면 다음 기동이 이 신호를 치웁니다.")
        return 0

    # 웜 내보내기는 **상주와 다른 프로세스에서** 돈다. `import pyarrow` 하나가
    # private 366MB 를 프로세스 수명 내내 붙들기 때문이다(실측 2026-08-12) —
    # 파이썬은 모듈을 프로세스가 죽을 때까지 놓지 않으므로 함수 안 임포트도
    # 일회성이 아니라 상주 비용이 된다. 하루 한 번 쓰는 라이브러리 때문에 관측자가
    # 366MB 를 이고 다니는 것은 설계 규칙 1 과 정면으로 어긋난다.
    #
    # **읽기(duckdb)는 상주에 남는다** — 지문·발열 드리프트가 웜을 읽어야 하고,
    # 그 경로는 pyarrow 를 끌어오지 않는다(실측: Parquet 조회 전체가 36.4MB).
    #
    # 이 분기는 인스턴스 락보다 **앞**이다. 상주가 락을 쥔 채로 부르므로 여기서
    # 락을 잡으려 하면 자식이 매번 물러난다.
    if args.export_warm:
        return _export_warm(settings)

    # 스냅샷 내보내기도 같은 자리다 — 상주가 락을 쥔 채 도는 중에 실행되므로
    # 인스턴스 락보다 **앞**이어야 한다. 원본에는 쓰지 않는다.
    if args.export_findings:
        return _export_findings(args)

    # 같은 DB 를 두 프로세스가 쓰면 수집이 두 배로 들어가고 융합 워터마크·예산 가드가
    # 서로 덮어쓴다. `--check` 는 아무것도 쓰지 않으므로 상주 인스턴스와 함께 돌 수 있다.
    lock: InstanceLock | None = None
    if not (args.check or args.allow_multi):
        try:
            lock = InstanceLock().acquire()
        except AlreadyRunning as e:
            # 오류가 아니다. 부팅 자동 시작과 손으로 띄운 실행이 겹치는 것은 실수가
            # 아니라 흔한 일이므로, 크래시 기록 없이 조용히 물러난다.
            log.info("이미 실행 중이라 종료", extra={"detail": e.detail})
            print(f"  Argus 가 이미 실행 중입니다 — {e.detail}. 이 프로세스는 종료합니다.")
            return 0

    caps = load_or_detect()
    profile = ensure_profile(
        disk_bench_mb=settings.calibration.disk_bench_mb,
        reuse_days=settings.calibration.reuse_days,
        force=args.recalibrate,
    )

    db = Database().open()
    try:
        report = _startup_report_lines(settings, caps, profile, db)

        if args.check:
            report.append("[OK] 기동 점검 통과")
            _emit(report, args.out)
            return 0
        _emit(report, None)

        guard = BudgetGuard(settings.budget)
        queue = SampleQueue(maxsize=settings.storage.queue_max_rows)
        sup = Supervisor(
            multiplier_fn=lambda: guard.multiplier,
            wake_granularity_s=settings.budget.wake_granularity_s,
        )

        # 직전 세션이 소비하지 못하고 남긴 종료 신호를 먼저 치운다. 그대로 두면 뜨자마자
        # 다시 죽어 사용자는 "실행이 안 된다"만 보게 된다.
        clear_stale()

        # 실행 중에 바꿀 수 있는 값. **트레이보다 먼저 만든다** — 트레이 메뉴가
        # 이것을 읽고 쓰고, 창(별도 프로세스)이 바꾼 것도 감시자가 집어 온다.
        live = LiveConfig(defaults={"notify": settings.detection.notify})
        sup.add(LiveConfigWatcher(config=live))

        # 트레이 — 알림 창구이기도 하다. **Fusion 보다 먼저 만들어야** 알림 전달자로
        # 넘겨줄 수 있다. 실패해도 수집은 계속된다(내부에서 삼키고 비활성 상태로 간다).
        tray: TrayIcon | None = None
        if settings.general.tray:
            tray = TrayIcon(
                on_stop=sup.request_stop,
                live=live,
                notify_sound=settings.general.notify_sound,
            )
            sup.add(tray)

        # 런타임 — 스로틀을 받지 않는다(부하가 클 때야말로 제때 돌아야 하는 것들)
        sup.add(BudgetMonitor(guard))
        sup.add(StopFileMonitor(sup.request_stop))
        sup.add(
            BatchWriter(
                db,
                queue,
                flush_interval_ms=settings.storage.flush_interval_ms,
                flush_max_rows=settings.storage.flush_max_rows,
            )
        )
        if settings.self_telemetry.enabled:
            sup.add(
                SelfTelemetry(
                    db,
                    guard,
                    interval_s=settings.self_telemetry.interval_s,
                    active_fn=sup.active_components,
                )
            )

        # 롤업은 보존 정리보다 **먼저** 등록한다. 순서가 동작을 정하지는 않지만
        # (보존이 워터마크로 스스로 막는다), 읽는 사람에게 의존 방향을 보여 준다.
        if settings.rollup.enabled:
            from .storage.rollup import (
                NetworkRollup,
                ProcessRollup,
                ProgramUsageRollup,
                Rollup,
            )

            sup.add(Rollup(db, settings.rollup))
            # 프로세스·네트워크는 5분 단위로 따로 접는다. 워터마크도 따로 두어야
            # 보존 정리가 "자기를 접은 롤업"을 보게 된다.
            sup.add(ProcessRollup(db, settings.rollup, top_n=settings.rollup.process_top_n))
            sup.add(NetworkRollup(db, settings.rollup))
            # 프로그램 사용시간은 하루 단위다. 이 롤업이 `process_events` 의 보존을
            # 붙잡으므로, 빼면 원본이 접히기 전에 지워진다(retention._rules 참조).
            sup.add(ProgramUsageRollup(db, settings.rollup))

            # 일일 리포트("어제 무엇을 했나"). 이상탐지와 별개 트랙이라 탐지가 꺼져
            # 있어도 돈다. **`process_metrics` 의 보존을 붙잡는 두 번째 롤업이다** —
            # 빼면 원본이 하루 만에 지워져 그날 리포트를 영영 만들 수 없다.
            from .report.builder import DailyReportRollup

            sup.add(DailyReportRollup(db, settings.rollup, settings.usage))

        # 프로그램 설명("svchost 가 무엇인가"). 롤업은 아니지만 같은 원본
        # (`process_events`)을 읽고 표시용 값을 채우므로 여기 함께 둔다.
        # 빠져도 수집·탐지는 그대로 돌고 표에 설명만 비어 보인다.
        from .collector.proginfo import ProgramInfoCollector

        sup.add(ProgramInfoCollector(db))

        if settings.warm.enabled:
            from .storage.warm import WarmExporter

            sup.add(WarmExporter(db, settings.warm))
        sup.add(Retention(db, settings.retention))

        # 수집기 — 예산이 빠듯해지면 스로틀 대상이 된다
        collectors = _build_collectors(settings, queue, caps)
        for collector in collectors:
            sup.add(collector)

        # 탐지 — 저장된 관측을 몇 초 늦게 따라 읽는다. 수집 경로에서 직접 조립하지
        # 않는 이유는 평가와 운영이 같은 코드를 타게 하기 위해서다(detection/live.py).
        # 기본값은 기록만 — 알림은 Phase 9 이고, 오탐률이 검증되기 전에는 붙이지 않는다.
        if settings.detection.enabled:
            from .decide.fusion import Fusion, FusionSettings
            from .detection.live import DetectionComponent

            if settings.fingerprint.enabled:
                # 프로세스 지문(Phase 6-B). procleak 의 오탐을 줄이는 데 쓰인다 —
                # "평소에도 핸들을 4천 개씩 쓰는 프로그램"을 이걸로 가린다.
                from .detection.fingerprint import FingerprintBuilder

                sup.add(
                    FingerprintBuilder(
                        db,
                        interval_s=settings.fingerprint.interval_s,
                        min_days=settings.fingerprint.min_days,
                        min_buckets=settings.fingerprint.min_buckets,
                        min_day_hours=settings.fingerprint.min_day_hours,
                    )
                )

            if settings.thermal_drift.enabled:
                # 냉각 열화 — "같은 부하에서 예전보다 뜨거운가". 절대 온도 룰과 달리
                # 하드웨어를 가정하지 않고, 사용자가 **조치할 수 있는** 신호만 낸다
                # (먼지 청소·서멀 재도포). 롤업을 읽으므로 주기가 길다.
                from .detection.thermal import ThermalDriftMonitor

                sup.add(ThermalDriftMonitor(db, settings.thermal_drift))

            sup.add(
                DetectionComponent(
                    db,
                    detector_name=settings.detection.detector,
                    interval_s=settings.detection.interval_s,
                    warm_window_s=settings.detection.baseline_window_s,
                )
            )
            # 신호를 사건으로 접는다. 여기서 Phase 8 귀인이 붙어 "왜"가 채워진다.
            # 알림 발송은 아직 없다 — severity 와 notified 판단까지만.
            #
            # **판정 문턱을 config 에서 실어 보낸다.** 기본값만 쓰면 사용자가
            # `%APPDATA%\Argus\config.yaml` 을 고쳐도 병목 분류가 그대로라 규칙 3 이
            # 껍데기가 된다("튜닝은 코드 수정 없이 YAML 만 고쳐서 되어야 한다").
            sup.add(
                Fusion(
                    db,
                    FusionSettings(
                        bottleneck=settings.bottleneck,
                        incident=settings.incident,
                        autolabel=settings.autolabel,
                        notify_enabled=settings.detection.notify,
                    ),
                    notifier=tray,
                    # 발송 시점마다 다시 물어본다. 위 `notify_enabled` 는 이 창구가
                    # 없을 때(리플레이·재분석)를 위한 값으로 남는다.
                    live=live,
                )
            )

        # 절전 복귀 감지. 수집기를 모두 등록한 뒤에 붙여야 브로드캐스트가 전부에 닿는다.
        if settings.gap_monitor.enabled:
            def handle_gap(gap_s: float, detail: dict) -> None:
                queue.put(
                    Sample("system_events", SYSTEM_EVENT_COLUMNS, gap_event_row(time.time(), gap_s, detail))
                )
                handled = sup.broadcast_time_gap(gap_s)
                log.info("공백 복구 완료", extra={"components": handled})

            sup.add(
                GapMonitor(
                    interval_s=settings.gap_monitor.interval_s,
                    threshold_s=settings.gap_monitor.threshold_s,
                    on_gap=handle_gap,
                )
            )

        # 직전 세션의 사인(死因)을 남긴다. startup 을 넣기 전이어야 "마지막 startup" 이
        # 직전 세션을 가리킨다. 큐가 아니라 직접 쓰는 이유는, 이 판정이 바로 다음 줄의
        # startup 보다 먼저 커밋되어야 순서가 뒤집히지 않기 때문이다.
        try:
            unclean = detect_unclean_shutdown(db.conn)
            if unclean is not None:
                db.conn.execute(
                    f"INSERT INTO system_events ({','.join(SYSTEM_EVENT_COLUMNS)}) VALUES (?,?,?,?)",
                    unclean,
                )
                db.conn.commit()
        except Exception:
            # 사후 판정 실패가 기동을 막아서는 안 된다.
            log.exception("직전 세션 판정 실패")

        queue.put(
            Sample(
                "system_events",
                SYSTEM_EVENT_COLUMNS,
                (time.time(), "startup", None, json.dumps({"version": __version__})),
            )
        )

        print("  수집기   : " + ", ".join(c.name for c in collectors))

        sup.install_signal_handlers()
        sup.start()

        # 기동 알림. **알림 경로가 살아 있는지 확인하는 유일한 방법이다** — 탐지가
        # 조용하면 알림도 없어서, 그 상태로는 "켜져 있는지"를 사용자가 알 수 없다.
        # `notify` 가 꺼져 있으면 사건 알림도 안 가므로 여기서도 띄우지 않는다.
        # **설정 파일이 아니라 실행 중 값을 본다** — 사용자가 지난번에 트레이에서
        # 껐으면 그 의사가 다음 기동에도 유지되어야 한다.
        if tray is not None and live.notify_enabled:
            # 트레이 컴포넌트의 `setup()`(아이콘 등록)이 자기 스레드에서 돌므로 잠깐 기다린다.
            # 못 기다리면 아이콘이 없는 상태라 알림이 조용히 버려진다.
            for _ in range(20):
                if tray._added:  # noqa: SLF001 - 같은 패키지의 기동 협력
                    tray.announce_start()
                    break
                time.sleep(0.1)

        if args.duration:
            # 지정 시간 뒤 종료. 타이머 스레드로 걸어 Ctrl+C 도 계속 받게 둔다.
            threading.Timer(args.duration, sup.stop).start()
            print(f"  {args.duration}초 동안 실행합니다. (Ctrl+C 로 조기 종료)")
        else:
            print("  실행 중입니다. Ctrl+C 로 종료합니다.")

        started = time.time()
        sup.wait()

        # **종료 표시는 큐가 아니라 DB 에 직접 쓴다.**
        #
        # 큐에 넣는 방식은 경합이었다. 종료 이벤트가 서는 순간 writer 스레드도 함께
        # 루프를 빠져나가 잔여분을 비우고 끝내므로, 메인 스레드가 큐에 넣기 전에 flush 가
        # 끝나면 그 샘플은 아무도 쓰지 않는다. 시그널로 끄면 사람이 누르는 타이밍이라
        # 우연히 이길 때가 있었고(`shutdown` 3건 / `startup` 17건), 종료 신호 파일로
        # 끄자 감시자가 즉시 요청하면서 매번 졌다.
        #
        # 그래서 모든 스레드를 정리한 **뒤에** 직접 쓴다. 이 시점엔 writer 가 이미 끝나
        # DB 를 두고 다툴 상대도 없다. 직전 세션 판정이 같은 이유로 직접 쓰고 있다.
        sup.stop()
        try:
            db.conn.execute(
                f"INSERT INTO system_events ({','.join(SYSTEM_EVENT_COLUMNS)}) VALUES (?,?,?,?)",
                (time.time(), "shutdown", None, json.dumps({"uptime_s": round(time.time() - started, 1)})),
            )
            db.conn.commit()
        except Exception:
            # 종료 기록 실패가 종료 자체를 막지는 않는다. 다음 기동이 미종결로 판정할 뿐이다.
            log.exception("종료 이벤트 기록 실패")

        snapshot = STATS.snapshot()
        log.info(
            "Argus 종료",
            extra={
                "uptime_s": round(time.time() - started, 1),
                "drop_count": snapshot.drop_count,
                "db_bytes": db.size_bytes(),
            },
        )
        _print_shutdown_report(db, started)
        return 0
    finally:
        db.close()
        if lock is not None:
            lock.release()


def _ensure_std_streams() -> None:
    """창 없는 빌드(`console=False`)에서 `sys.stdout`/`stderr` 가 `None` 인 것을 막는다.

    **PyInstaller 의 windowed 빌드에는 콘솔이 없어 두 스트림이 `None` 이다.**
    그 상태로 `print()` 를 부르면 `AttributeError: 'NoneType' object has no
    attribute 'write'` 로 죽는다 — 기능이 아니라 *결과를 보여 주려던 코드* 때문에
    프로그램 전체가 내려가는 것이다.

    2026-08-17 에 노트북(창모드 게임 + 마우스 매크로)에 배포하려고 콘솔을 껐다.
    콘솔 창이 뜨면 게임 위에 검은 창이 겹치고, 매일 도는 스냅샷 작업이 하루 한 번씩
    포커스를 흔든다. 창을 없애는 대신 이 방어가 필요해졌다.

    `logging_setup` 이 `sys.stderr` 로 `StreamHandler` 를 만드는 자리도 같이 지킨다.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                # devnull 조차 못 여는 환경이라면 더 할 수 있는 것이 없다.
                # 여기서 죽지는 않는다 — 아래 `_emit` 가 예외를 삼킨다.
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()
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

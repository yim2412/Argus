# COVERAGE — 2026-09-25 전면 감사 (1차)

> 어느 파일을 어느 영역으로 **실제로** 읽었는지. 없으면 "감사했다"를 반증할 수 없다.
> `tools/audit/check_ledger.py` 가 추적 파일 목록과 대조한다(누락·유령).
> **정적 스캔·설정 퍼징·골든은 전 파일에 기계적으로 적용됐다** — 아래 '깊이'는 사람이 읽은 것만 적는다.
> `detection/`·`decide/`·`explain/`·`storage/` 는 독립 리뷰가 따로 읽는다(결과는 보고서에).

| 파일 | 읽은 영역 · 깊이 |
|---|---|
| `argus/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/__main__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/branding.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/base.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/gpu.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/network.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/pdh.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/process.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/procsource.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/proginfo.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/collector/system.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/config/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/config/loader.py` | 설정 배선·하위호환 — 로더 함수 전부, `ensure_user_config` (F-007·026) |
| `argus/dashboard/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/dashboard/data.py` | 조용한 실패·리소스 누수 — `health`·함수 목록, 연결 열고 닫기 전부 (PASS: 요청마다 `finally: close`) (F-014) |
| `argus/dashboard/theme.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/decide/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/decide/autolabel.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/decide/budget.py` | 알림 판정·시각 경계 — `NotificationBudget` 전체 (F-022) |
| `argus/decide/fusion.py` | 설정 배선·조용한 실패·설명 정확도 — `_refine_bounds`·`_peak_and_baselines`·`analyze_incident`·`_trigger_rules`·`run_once`·`_close`·`_merge` 전부 (F-006·016·017·019·020·022) |
| `argus/decide/severity.py` | 위생 — 머리말·함수 목록, 제품 사용처 grep (F-023) |
| `argus/decide/suppression.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/app.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/incidents.py` | 보안 — 상세 리치텍스트 렌더 (PASS: Windows 파일명에 `<>` 불가) |
| `argus/desktop/pages/processes.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/realtime.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/report.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/selfstate.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/settings.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/timeline.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/pages/usage.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/desktop/widgets.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/base.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/baseline.py` | 위생·탐지 신뢰성 — 머리말, `BaselineSet` 의 `reset`·`ready`·`readiness` (F-018) |
| `argus/detection/expr.py` | 조용한 실패 — `_eval` 이항·단항·비교 (F-021) |
| `argus/detection/fingerprint.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/live.py` | 조용한 실패·탐지 신뢰성 — `setup`·`tick`·`on_time_gap` (F-018) |
| `argus/detection/procleak.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/registry.py` | 설정 배선 — 전체 |
| `argus/detection/replay_source.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/rules.py` | 조용한 실패·설정 배선 — `load_rules`·`build`·`RuleEngine.__init__`·`reset`·`evaluate` 앞부분·`machine_variables` (F-001·003·004·018·027) |
| `argus/detection/thermal.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/detection/trace.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/__main__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/attribution.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/baselines.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/replay.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/eval/scoring.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/explain/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/explain/attribution.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/explain/bottleneck.py` | 설명 정확도 — `classify`·`_choose` 앞부분 (F-016) |
| `argus/explain/changepoint.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/explain/report.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/logging_setup.py` | 조용한 실패 — `write_crash` |
| `argus/machine/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/machine/calibration.py` | 인코딩 — subprocess 호출 전부 (PASS: `encoding="utf-8"`·`errors="replace"` 명시, 정적 스캔) |
| `argus/machine/capabilities.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/paths.py` | 빌드 — `resource_path` (PASS: spec datas 와 일치) |
| `argus/report/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/report/builder.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/report/data.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/budget.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/gapmon.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/heapcensus.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/livecfg.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/selftel.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/session.py` | 리소스 누수 — 메모리 DB 연결 한 곳 (PASS: 테스트 전용 `:memory:`) |
| `argus/runtime/singleton.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/stats.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/stopfile.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/runtime/supervisor.py` | 조용한 실패 — 컴포넌트 수명 전체 (F-014) |
| `argus/storage/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/storage/findings.py` | 개인정보·리소스 누수 — 머리말·표 목록, 연결 닫기 (PASS) |
| `argus/storage/history.py` | 시각 경계·리소스 누수 — 날짜 키 3엔진 대조 (PASS, 실측), 연결 닫기 4곳 (PASS) |
| `argus/storage/hot.py` | 하위호환 — `open`·`close`·`_migrate` (F-012·013) |
| `argus/storage/locktrace.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/storage/queue.py` | 수집 규칙 2 — `put`·`put_many`·`drain` (리뷰 #11 오탐 판정) |
| `argus/storage/replay_db.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/storage/retention.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/storage/rollup.py` | 시각 경계 — 날짜 계산 줄만 grep |
| `argus/storage/warm.py` | 시각 경계·인코딩·데이터 보존 — 날짜 계산 줄, 내보내기 자식 호출(PASS: 자식은 `logging_setup` 으로 UTF-8 을 못박고 부모도 UTF-8 로 읽는다), `raw_watermark` (F-015) |
| `argus/storage/writer.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/ui/__init__.py` | 기계 검사만 (정적 스캔·골든·퍼징) — 사람이 읽지 않음 |
| `argus/ui/tray.py` | UI 조용한 실패·인코딩 — `setup`·`notify`·`_open_dashboard`·`_watch_dashboard` (PASS: 창 stderr 파이프 교착 가설 기각 — `communicate` 의 읽기 스레드가 타임아웃 뒤에도 계속 읽는다, `probes/p028`. FAIL: CP949 에서 창 실패 이유가 깨진다 F-029) |

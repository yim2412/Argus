# 발견 대장 — 2026-09-25 전면 감사 (1차)

> 기준 커밋 `a69ec85`. 절차는 `~/.claude/commands/audit.md`. **분석 중에는 코드를 고치지 않는다.**
> 형식은 `tools/audit/check_ledger.py` 가 파싱한다 — 머리줄 `### F-NNN · 영역: … · 상태: …` 와
> 필드 줄(`- 키: 값`)을 바꾸지 않는다.
>
> - 근거 등급: `실측`(프로브·명령으로 재현) / `인용`(코드 인용만) / `추정`
> - **재현 없는 발견은 `높음` 이상 금지.** 독립 리뷰 발견은 내가 재현하기 전엔 최대 `중간`.
> - 이력: `[신규]` / `[기존]`(계획서·CHANGELOG·DONE 에 이미 있음). 정지 규칙은 `[신규]` 만 센다.
> - 상태: `미처리` · `수정중` · `수정됨` · `기각` · `판단 필요`

## ▶ 재개 지점 (2026-09-25 17:00 — **합의 받음, 3단계(수정 판) 진행 중**)

**지금 단계: 2단계(분석) 끝 → 두 번째 멈춤.** 코드는 한 줄도 안 고쳤다(감사 도구 `~/.claude/tools/pool.js` 는
고쳤다 — 측정이 결과를 버리고 있었다, `ef4511f`). 합의를 받으면 아래 순서대로 3단계(수정 판)를 무인으로 간다.
끝난 것: 0단계 설계 · 1단계 안전망(`8f3f7d8`) · 대장 F-001~F-029 · 변이 수집(스윕 F-024 · if-py F-025, 양성 대조로
하네스 확인) · 독립 리뷰 14건 판정 · 남은 영역(트레이 UI · 리소스 누수 · 인코딩 CP949 흉내).

#### 수정 순서 (선행 조건 · 비용 · 축 기여 — 심각도순이 아니다)

| 순 | 항목 | 심각도 | 왜 이 자리 |
|---|---|---|---|
| 1 | F-002 | 높음 | **선행 조건.** 수정 판의 변이 검증이 `mutation_sweep` 인데 앵커 11개가 끊겨 전체 실행이 죽는다 |
| 2 | F-028 | 중간 | **선행 조건.** 5·6·7 번이 실시간·융합 경로를 고친다 — 그 경로를 재는 테스트가 먼저 있어야 한다 |
| 3 | F-015 | 높음 | 비가역 데이터 손실(원본 영구 삭제). 비용 작음 |
| 4 | F-012 | 높음 | 배포 후 업데이트 한 번에 영구 기동 불가. 비용 작음 |
| 5 | F-016 | 높음 | 제품 산출물("왜 느렸나")이 틀린다. 비용 중간 + 골든 갱신 |
| 6 | F-019 → F-017 → F-020 → F-022 | 중간·중간·낮음·낮음 | 융합 묶음(같은 파일). F-022 로 `lag_s` 를 config 로 빼면 F-017 튜닝이 된다 |
| 7 | F-018 · F-027 | 중간·낮음 | 탐지 신뢰성 묶음(`rules.py` 의 reset·캐시) |
| 8 | F-014 | 중간 | 조용한 실패 축 전반의 가시성(컴포넌트 건강). 7 까지의 수정이 "실패하면 보이게" 되는 자리 |
| 9 | F-029 | 중간 | 배포 대상 전용 인코딩. 비용 작음 |
| 10 | F-001 → F-003 · F-004 · F-021 | 높음·중간·중간·낮음 | **판단 필요(F-001)** 뒤. 사용자 룰을 읽기로 하면 셋이 같이 따라온다 |
| 11 | F-026 → F-007 → F-013 | 중간·중간·낮음 | **판단 필요(F-026·F-013)** 뒤. 설정·하위호환 묶음 |
| 12 | F-024 · F-025 | 중간 | 단언 보강 — 앞 수정들이 겹치는 줄을 먼저 흡수하고 남은 것만 |
| 13 | F-011 · F-005 · F-006 · F-008 · F-009 · F-010 · F-023 | 중간~낮음 | 배포물 고지·문구·위생 |

상위 5개 중 둘(F-002·F-016)은 국소 수정이 아니다 — 무서운 것을 피하지 않았다.

#### 판단 — 사용자 결정 (2026-09-25 17:00, 넷 다 추천안)

| 항목 | 결정 |
|---|---|
| F-001 | **지킨다** — 사용자 룰 파일을 읽는다. F-003·F-004·F-021 이 같이 따라온다 |
| F-026 | **빈 템플릿 + 버전** — 앞으로는 키를 전부 주석 처리한 템플릿(설명서 역할 유지)과 버전 필드. 이미 퍼진 사본(이 PC·노트북, 아직 배포 전)은 기본값과 같은 키를 걷어 낸다(백업 후) |
| F-013 | **경고만** — 수집은 계속하고 로그 + 창/트레이에 드러낸다 |
| F-025 | **스모크는 지금처럼** — 손으로 돌린다. 단언 보강은 제품 판정 15줄만 |

순서표는 그대로 간다. 진행 상황은 각 항목의 `상태:` 가 말한다(`수정중` 이 있으면 그게 끊긴 자리).

#### 독립 리뷰 — 결과 (14건 전부 판정)

에이전트 1개(detection·decide·explain·storage·config 범위, 내 발견 비공개).

| # | 에이전트 등급 | 판정 | 대장 | 근거 |
|---|---|---|---|---|
| 1 | 높음 | 재현 | F-015 높음 | 프로브(삭제까지) |
| 2 | 높음 | 재현 · **내림** | F-017 중간 | 프로브 + 실DB 323건 중 2건 |
| 3 | 높음 | 재현 | F-016 높음 | 프로브 |
| 4 | 높음 | 재현(모습이 다름: 오탐이 아니라 미탐) · **내림** | F-018 중간 | 프로브 |
| 5 | 중간 | 인용 · **내림** | F-020 낮음 | 실DB 29건 중 0건 |
| 6 | 중간 | 인용 | F-026 중간 | 코드 |
| 7 | 중간 | 재현 | F-019 중간 | 프로브 |
| 8 | 중간 | 재현(단위) · **내림** | F-021 낮음 | 지금은 도달 불가(F-001) |
| 9·12·13 | — | 겹침 | F-003·F-005·F-006 | 내가 먼저 찾음 |
| 10 | 낮음 | 인용 | F-022 낮음 | 융합 상수와 묶음 |
| 11 | 낮음 | **오탐** | — | 큐 용량 20,000 > 한 번에 넣는 묶음 |
| 14 | 낮음 | 인용 | F-027 낮음 | 코드 |

**겹침 3 · 에이전트만 10(그중 `높음` 2: F-015·F-016) · 오탐 1.** 에이전트만 찾은 `높음` 이 있으므로
다음 감사에도 쓴다(audit.md 기준). 에이전트의 `높음` 4건 중 2건은 실데이터로 재면 `중간` 이었다 — 등급은
믿지 말고 발견만 받는다.

#### 이번 세션에 한 실수 (보고서에 넣는다)
- 처음에 "상주가 안 떠 있다"고 보고 — 명령줄에 `argus` 가 있는지로 찾았는데 상주는 `tools\soak_entry.py` 로 떠 있었다.
- 백테스트 확인 중 `af73bf7^` 트리의 `autolabel_backfill.py` 를 **격리 없이** 실행 → 실제 DB 를 열었다.
  첫 `judge()` 에서 `TypeError` 로 죽어 판정 쓰기 전에 끝났다. 이후 도구는 격리 폴더에서만 돌린다.
- `mutate.js` 를 두 개 동시에 돌려 전역 잠금을 덮어썼다(전역 CLAUDE.md 에 경고 추가).

---

### F-001 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/detection/rules.py:268` — `source = path if path is not None else resource_path("config/rules.yaml")`
- 요약: 동봉 `rules.yaml` 머리말은 "사용자 룰은 `%APPDATA%\Argus\rules.yaml` 에 두면 이 파일을 대체한다"고 약속하는데, 사용자 경로를 읽는 코드가 어디에도 없다. 사용자가 룰을 고쳐도 **아무 일도 안 일어나고 아무 신호도 없다.**
- 근거: 실측 — 격리 데이터 폴더에 룰 1개짜리 파일을 두고 `registry.build("rules")` → 동봉본 10개 룰이 쓰임
- 재현: `docs/audit/probes/p001_user_rules_ignored.py` (현재 FAIL)
- 반증조건: 다른 경로(환경변수·설정 키)로 사용자 룰 파일을 넘기는 코드가 있으면 기각
- 이력: [신규] — 계획서·CHANGELOG·DONE 에 없음
- 심각도: 높음
- 수정비용: 작음(경로 해석 1곳 + 테스트). 단 **판단 필요**: 약속을 지킬지(사용자 파일 읽기) 약속을 지울지(머리말 수정). 전자면 F-003 이 같이 따라온다
- 대상: `argus/detection/rules.py`, `argus/config/rules.yaml`, `argus/paths.py`

### F-002 · 영역: 측정 도구 신뢰성 · 상태: 수정중
- 위치: `tools/mutation_sweep.py` — `MUTANTS` 중 11개
- 요약: 무력화할 원문을 소스에서 찾지 못하는 변이가 160개 중 11개. 전체 실행은 11번째(`rule_for`)에서 `[중단]` 으로 죽는다 — 그 뒤 149개도 전체 실행으로는 한 번도 안 돈다. 끊긴 것: `rule_for` · `rule_cooldown`(탐지 규칙 1) · `notify_failure_isolation` · `rollup_watermark` · `per_program_fallback` · `window_fits_screen` · `usage_retention_watermark` · `daily_report_retention_hold` · `answer_mark_separates_unasked` · `autolabel_never_overwrites_human` · `autolabel_skips_unnotified`. **이 11개 규칙은 지금 아무도 재지 않는다.**
- 근거: 실측 — 사본에서 전체 실행 → `[중단] rule_for: … 원문이 0회 발견됐다`, exit 1. 앵커 전수 대조 11/160
- 재현: `docs/audit/probes/p002_sweep_stale_anchors.py`
- 반증조건: 해당 규칙이 다른 변이 키로 재어지고 있으면 그 키만큼 기각
- 이력: [신규]
- 심각도: 높음
- 수정비용: 중간(11개 앵커를 현재 코드에 다시 맞추고, 각각 잡히는지 확인). 도구에 "시작 전 앵커 전수 검사"를 넣으면 다음 번엔 11번째가 아니라 0번째에 전부 보인다
- 대상: `tools/mutation_sweep.py`

### F-003 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/detection/rules.py:276` — `for index, entry in enumerate(data.get("rules") or []):`
- 요약: 룰 파일 구조가 틀리면(룰이 문자열·`rules` 가 매핑·최상위가 리스트·YAML 문법 오류) `RuleError` 가 아니라 `AttributeError`/`ParserError` 가 난다. 그러면 `build()` 가 "detection 설정을 읽지 못해 기본값을 쓴다"는 **틀린 경고**를 남긴 뒤 `RuleEngine()` 에서 같은 예외가 다시 나고, `live.setup` 이 로그 한 줄만 남기고 **룰 탐지 전체를 끈다.** 사용자에게 보이는 신호는 없다(설계 규칙 4).
- 근거: 실측(`tools/audit/config_fuzz.py` crash 4건) + 인용(`rules.py:340` except → `RuleEngine()`, `live.py:70` except → continue)
- 재현: `.venv\Scripts\python.exe tools\audit\config_fuzz.py` → `crash:AttributeError rules(구조)` ×3, `crash:ParserError` ×1
- 반증조건: 지금은 동봉본만 읽으므로(F-001) 사용자가 이 경로를 밟을 수 없다 → **F-001 을 "약속 삭제"로 정하면 이건 낮음으로 내린다**
- 이력: [신규] (CHANGELOG:2735 "룰 파일 오타 하나로 … 나머지는 계속 돈다"는 격리만 다뤘고, 알림·메시지는 다루지 않았다)
- 심각도: 중간
- 수정비용: 작음
- 대상: `argus/detection/rules.py`

### F-004 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/detection/rules.py` — `Condition(**c)` (메트릭 이름을 검증하지 않음)
- 요약: 존재하지 않는 메트릭 이름을 쓴 룰이 **오류 없이 로드되고 영원히 발화하지 않는다.** 룰 이름을 오타 내면 "예외도 로그도 없이 룰만 죽는다" — 822c1fa 가 기록한 바로 그 모양.
- 근거: 실측 — `config_fuzz.py` `silent rules(구조) [알수없는 메트릭]`
- 재현: 위와 같음
- 반증조건: 런타임에 알 수 없는 메트릭을 한 번이라도 경고하는 코드가 있으면 낮음으로
- 이력: [신규]
- 심각도: 중간 (사용자 룰이 읽히지 않는 지금은 개발자 실수 경로뿐. F-001 결정에 따라 올라간다)
- 수정비용: 작음(로드 시 알려진 메트릭 집합과 대조)
- 대상: `argus/detection/rules.py`

### F-005 · 영역: UI · 상태: 미처리
- 위치: `argus/config/rules.yaml` — `CPU 과부하` 룰 `explain: "CPU {cpu_total}% 로 45초 이상 지속 …"` / `for: 30s`
- 요약: 사용자에게 가는 알림 문장이 "45초 이상 지속"이라 말하는데 실제 발화 조건은 30초다. 주석(“45초로 잡았더니 … 놓쳤다”)대로 `for` 만 내리고 문장을 안 고쳤다.
- 근거: 실측 — 전 룰의 `explain` 속 시간과 `for` 대조: 10개 중 1개 불일치
- 재현: `docs/audit/probes/p005_explain_vs_for.py`
- 반증조건: 없음(문자열 그대로)
- 이력: [신규]
- 심각도: 중간 (사용자가 보는 문장이 사실과 다르다 — 설명이 산출물이라는 탐지 규칙 2)
- 수정비용: 작음 + 재발 방지로 정적 검사 1개
- 대상: `argus/config/rules.yaml`, `tools/audit/static_scan.py` 또는 테스트

### F-006 · 영역: 설정 배선 · 상태: 미처리
- 위치: `argus/decide/fusion.py:381`, `:434` — `baselines = BaselineSet(window_s=1800.0, min_samples=60)`
- 요약: 사건 경계·최악 시점 계산의 베이스라인 창/표본 수가 코드에 박혀 있다. `detection.baseline_window_s`·`min_samples` 를 YAML 에서 바꿔도 **설명 쪽은 안 바뀐다**(규칙 3). 창 앞 조회도 `ts_start - 1800.0` 으로 같이 박혀 있다.
- 근거: 인용
- 재현: (수정 판에서 배선 테스트로 — 기본값이 아닌 값으로)
- 반증조건: 설명 쪽 창을 탐지 쪽과 **일부러** 다르게 둔 기록이 있으면 "판단 필요"로
- 이력: [신규]
- 심각도: 중간
- 수정비용: 작음
- 대상: `argus/decide/fusion.py`, `argus/config/loader.py`

### F-007 · 영역: 설정 배선 · 상태: 미처리
- 위치: `argus/config/loader.py` — 모든 설정 모델 (pydantic 기본 `extra="ignore"`)
- 요약: 사용자 `settings.yaml` 의 **오타 키가 조용히 무시된다.** 22개 절 전부. 사용자는 값을 고쳤는데 아무 일도 안 일어난다.
- 근거: 실측 — `config_fuzz.py` silent 22건
- 재현: `.venv\Scripts\python.exe tools\audit\config_fuzz.py`
- 반증조건: 로드 뒤 모르는 키를 경고하는 코드가 있으면 기각
- 이력: [기존] — CHANGELOG:2136 "pydantic 은 모르는 키를 조용히 무시". 개발자 쪽(`defaults.yaml`)만 테스트로 막았고 사용자 파일은 미처리
- 심각도: 중간
- 수정비용: 작음(거부가 아니라 **경고** — 옛 설정 파일의 은퇴한 키가 기동을 막으면 안 된다: 하위호환)
- 대상: `argus/config/loader.py`

### F-008 · 영역: 도구 배선 · 상태: 미처리
- 위치: 안전망 전체 (`tools/audit/run_gates.py` `tools-import`·`tools-help`)
- 요약: `tools/` 의 시그니처 드리프트는 **호출 시점**에 터진다(af73bf7: `judge()` 에 `observer` 필수 → 실행 즉시 `TypeError`). 임포트·`--help` 게이트는 그걸 못 잡는다. 백테스트에서 놓침.
- 근거: 실측 — af73bf7^ 트리에서 게이트 초록, 도구 실행은 `TypeError: judge() missing 1 required keyword-only argument: 'observer'`
- 재현: `.venv\Scripts\python.exe tools\audit\backtest.py af73bf7`
- 반증조건: —
- 이력: [기존] 사건(CLAUDE.md "tools/ 는 테스트 밖") · 안전망 공백은 [신규]
- 심각도: 중간
- 수정비용: 중간 — 도구마다 **합성 DB 위의 미리보기(dry-run)** 를 게이트에 넣는다. 쓰기가 있는 도구는 미리보기 경로가 있어야 한다
- 대상: `tools/audit/run_gates.py`, 해당 도구들

### F-009 · 영역: 위생 · 상태: 미처리
- 위치: `tools/make_icon.py` · `tools/readiness.py` · `tools/soak_entry.py` · `tools/pyc_audit.py`
- 요약: argparse 가 없어 `--help` 를 무시하고 **본 동작을 한다.** 감사 중 `make_icon.py --help` 가 `argus/assets/argus.ico` 를 다시 썼다(결정론이라 diff 는 없었다). `soak_entry` 는 상주 진입점이라 `--help` 가 상주를 띄울 수 있다(확인 안 함 — 이번엔 인자 파서가 argus 쪽이라 usage 가 떴다).
- 근거: 실측(make_icon) / 인용(나머지)
- 재현: `grep -L ArgumentParser tools/*.py`
- 반증조건: —
- 이력: [신규]
- 심각도: 낮음
- 수정비용: 작음
- 대상: 위 4개 도구

### F-010 · 영역: 위생 · 상태: 미처리
- 위치: `CLAUDE.md` 구조 표 · 탐지 규칙 4
- 요약: ① 구조 표에 `argus/report/` 패키지가 없다(파일 → 역할 표가 "어디를 고칠지"의 근거라 빠지면 안 보인다). ② 탐지 규칙 4 "초기 2시간은 수집만"은 PLAN:441 에서 **재시작 후 부트스트랩 폐지**로 바뀌었고 실제 문턱은 표본 수(`min_samples: 60`)다. CLAUDE.md 가 낡았다.
- 근거: 인용
- 재현: —
- 반증조건: —
- 이력: ② [기존] PLAN:441 결정, 문서 반영만 빠짐 · ① [신규]
- 심각도: 낮음
- 수정비용: 작음
- 대상: `CLAUDE.md`

### F-011 · 영역: 라이선스·귀속 · 상태: 미처리
- 위치: `packaging/argus.spec:33`, `packaging/argus_ui.spec:24` — `datas = [` (LICENSE·고지 없음)
- 요약: 배포물(`dist/argus`, `dist/argus-ui`)에 자체 `LICENSE` 도, 제3자 고지도 없다. PyInstaller 가 dist-info 4개(duckdb·numpy·markupsafe·pydantic)의 라이선스만 우연히 실었다. **PySide6/Qt 는 LGPL-3** 이라 고지와 교체 가능성(onedir 는 충족) 안내가 필요하고, pyarrow(Apache-2.0)는 NOTICE 를 요구한다.
- 근거: 실측 — `find dist -iname "*licen*"` 결과가 위 4개 dist-info 밖에 없음(2026-08-17 빌드)
- 재현: `find dist -maxdepth 3 -iname "*licen*"`
- 반증조건: 설치 단계(Phase 14)에서 따로 넣는 계획이 있으면 "판단 필요"로
- 이력: [신규]
- 심각도: 중간 (배포가 전제인 프로젝트)
- 수정비용: 작음 — `THIRD_PARTY_NOTICES.txt` 생성 스크립트 + `datas` 한 줄 + 배포 스모크에 존재 검사
- 대상: `packaging/*.spec`, `packaging/make_deploy.ps1`, 새 고지 파일

### F-012 · 영역: 하위호환 · 상태: 미처리
- 위치: `argus/storage/hot.py:141` — `self.conn.executescript(sql)` / `:147` `self.conn.rollback()`
- 요약: `executescript()` 는 실행 전에 COMMIT 하고 스크립트를 자동 커밋으로 돌린다 — 실패 시 `rollback()` 이 되돌릴 게 없다. 여러 문장짜리 마이그레이션이 중간에 실패하면 앞 문장이 남고 `user_version` 은 안 올라가, **다음 기동부터 `duplicate column` 으로 영원히 DB 를 못 연다**(마이그레이션을 고쳐 재배포해도). 실제 파일 중 ALTER 가 2개 이상인 것: 005·012·018·020. 도중 실패의 현실적 방아쇠는 락 대기 초과(이 PC 에서 25초 쓰기 관측 기록 있음)·디스크 가득 참.
- 근거: 실측(메커니즘) — 합성 마이그레이션으로 재현. 실제 사용자 PC 에서의 발생은 추정
- 재현: `docs/audit/probes/p012_partial_migration.py` (현재 FAIL: 실패 뒤 컬럼 `['a','b']`, 재기동 `duplicate column name: b`)
- 반증조건: —
- 이력: [신규]
- 심각도: 높음 (배포 후 업데이트에서 사용자 PC 의 상주가 벽돌이 된다 — 수집·저장 규칙 4 가 막으려던 바로 그 일)
- 수정비용: 작음 — 문장 단위 `execute` 를 명시적 `BEGIN`/`COMMIT` 안에서(SQLite DDL 은 트랜잭션 가능) + 프로브를 테스트로
- 대상: `argus/storage/hot.py`, 테스트

### F-013 · 영역: 하위호환 · 상태: 미처리
- 위치: `argus/storage/hot.py:129` — `pending = [(v, p) for v, p in migration_files() if v > current]`
- 요약: DB 의 `user_version` 이 코드가 아는 최대 버전보다 **높을 때**(새 버전을 쓰다 옛 exe 로 되돌린 경우) 아무 검사 없이 연다. 옛 코드가 새 스키마에 쓰면 조용히 어긋날 수 있다.
- 근거: 인용
- 재현: —
- 반증조건: 모든 스키마 변경이 가산적(ADD COLUMN·새 표)이라 옛 코드가 안전하게 쓸 수 있다고 보장되면 낮음 유지·기각
- 이력: [신규]
- 심각도: 낮음 (자동 업데이트 채널이 아직 없다 — "나중에" 표)
- 수정비용: 작음(경고 로그 + 창 표시) — 막을지 경고할지는 **판단 필요**
- 대상: `argus/storage/hot.py`

### F-014 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/runtime/supervisor.py:122` (setup 실패 → `return`, 컴포넌트 영구 중단) · `argus/dashboard/data.py:408` `health()`
- 요약: 컴포넌트가 setup 에서 실패해 영구히 멈추거나 tick 이 계속 실패해도 흔적은 **로그와 `crash_*.json` 뿐**이고, 둘 다 읽는 코드가 없다. `health()` 는 수집 정지(`sample_ts`)만 가른다 — **융합·탐지·롤업·알림이 죽으면 "사건 없음"으로 보여 정상과 구별되지 않는다.** 백테스트가 놓친 13건 중 3건(a8fd5fb 지문 스레드 몇 주 · e5a547f 트레이 열기 · 7b32e3e explorer 재시작)이 이 유형이다 — 매번 그 자리만 막았고 유형 전체를 드러내는 장치는 없다.
- 근거: 인용 (크래시 파일 소비자 grep 0건, health 반환 필드 4개)
- 재현: (수정 판에서 프로브: setup 이 실패하는 컴포넌트 → 창이 읽는 계층에서 보이는가)
- 반증조건: 다른 경로(자기계측 표 등)가 컴포넌트 생존을 기록하고 창이 그걸 보여 주면 기각
- 이력: 사건들은 [기존], 유형 전체를 드러내는 장치가 없다는 것은 [신규]
- 심각도: 중간 (재현 전이라 상한)
- 수정비용: 중간 — 수퍼바이저가 컴포넌트별 마지막 성공 tick·연속 실패 수를 `meta`/자기계측에 남기고 `health()` 가 "멈춘 구성요소"를 한 줄로
- 대상: `argus/runtime/supervisor.py`, `argus/runtime/selftel.py`, `argus/dashboard/data.py`, `argus/desktop/app.py`

### F-015 · 영역: 데이터 보존 · 상태: 수정됨
- 위치: `argus/storage/warm.py:397` — `"SELECT MAX(date_key) AS d FROM warm_exports WHERE kind = ?"`
- 요약: `raw_watermark()` 가 종류마다 **가장 늦게 내보낸 날짜(MAX)** 를 쓴다. `export_pending` 은 하루가 실패하면 로그만 남기고 다음 날짜로 가므로, 중간 날짜가 비어도 워터마크는 그 뒤까지 간다. `Retention` 은 그 워터마크까지 초 단위 원본을 지운다 → **한 번도 웜으로 나가지 않은 날의 원본이 복구 불가능하게 삭제된다.** 현실적 방아쇠: 백신의 파일 잠금으로 `temp.replace(target)` 실패, 디스크 가득 참.
- 근거: 실측 — 격리 폴더 3일치, 09-02 raw_metrics 내보내기만 실패 주입 → 09-02 원본 SQLite 0행·웜 없음. 대조(주입 없음)에서는 09-02 가 웜으로 나감(프로브가 유효함을 확인)
- 재현: `docs/audit/probes/p015_warm_gap_raw_loss.py` (현재 FAIL)
- 반증조건: 실패한 날짜를 다음 회차 전에 반드시 재시도하는 경로가 있으면 기각 — 없음(재시도 전에 보존 정리가 먼저 돌 수 있다)
- 이력: [신규] — 독립 리뷰가 찾음(#1), 내가 삭제까지 재현. 설계 의도("내보내기가 다 끝난 뒤에 세운다", warm.py:565)는 **순서**만 막고 **빈칸**은 안 막았다
- 심각도: 높음
- 수정비용: 작음 — 워터마크를 "빈칸 없이 이어진 마지막 날짜"로 + 프로브를 테스트로
- 대상: `argus/storage/warm.py`, 테스트
- 결과: `raw_watermark` 가 종류마다 "가장 늦게 나간 날" 앞의 **안 나간 날**(`exportable_dates`)을 보고 그 날 시작에서 멈춘다. 0행인 날은 `export_date` 가 기록하므로 빈칸으로 남지 않는다(워터마크가 영구히 묶이지 않는다). 실패한 날은 다음 회차에 다시 시도되고 그때 워터마크가 다시 전진한다. 프로브 FAIL → **PASS**. 테스트 `test_watermark_stops_before_a_day_that_failed_to_export` — "막지 않았으면 MAX 가 08-03" 대조를 먼저 단언, 고치기 전 빨강 확인. 되돌리는 변이(`if gaps:` → `if False:`) 빨강 확인, `mutation_sweep` 에 `warm_watermark_stops_at_gap` 등록. 웜 테스트 31 통과. **상주 재시작 필요**(수정 판 끝에 모아서)

### F-016 · 영역: 설명 정확도 · 상태: 미처리
- 위치: `argus/decide/fusion.py:443` — `peak_row = max(rows, key=lambda r: r["cpu_total"] or 0.0)`
- 요약: 사건의 "최악 시점"을 `cpu_total` 이 가장 큰 행 **하나**로 고르고, 병목 판정은 그 행만 본다. 디스크가 막힌 순간과 CPU 가 튄 순간이 다르면 디스크가 멀쩡한 행이 판정된다. 사건을 연 디스크 룰의 방아쇠 우선(`trigger_metrics`)도 못 살린다 — 그 행에서는 IO 점수 자체가 없어서 `_choose` 가 고를 후보에 없다. **"왜 느렸나"가 이 제품의 산출물이다(탐지 규칙 2).**
- 근거: 실측 — 격리 폴더, 60초 디스크 정체(응답 200ms·큐 6) 중 1초만 CPU 95% → 병목 `CPU`. 대조(튐 없음) → `IO`
- 재현: `docs/audit/probes/pr03_peak_by_cpu_only.py` (현재 FAIL)
- 반증조건: 병목 판정이 행 하나가 아니라 구간 전체(또는 방아쇠 지표의 최악 행)를 보는 경로가 있으면 기각 — `analyze_incident` 는 `peak` 하나만 `classify` 에 넘긴다
- 이력: [신규] — 독립 리뷰 #3, 내가 재현
- 심각도: 높음
- 수정비용: 중간 — 방아쇠 지표가 있으면 그 지표의 최악 행을 쓰거나, 지표마다 구간 최악값을 모은 합성 행으로 판정. 골든(설명 문장)이 바뀌므로 갱신 필요
- 대상: `argus/decide/fusion.py`, 테스트, 골든

### F-017 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/decide/fusion.py:583` — `"WHERE run_id IS NULL AND ts > ? AND ts <= ? ORDER BY ts",`
- 요약: 융합은 **탐지 시각**이 워터마크 뒤인 신호만 읽고 워터마크를 `now - lag_s` 로 옮긴다. 신호가 탐지 후 `lag_s` 보다 늦게 기록되면(락 정체) 영영 안 읽힌다. 제품은 `lag_s` 를 넘기지 않아 **코드 기본 15초**다(→ F-022). 락 정체 실측: 10초 초과 하루 1~3건, 최악 115초.
- 근거: 실측 — 격리 폴더, 탐지 20초 뒤 기록된 신호 → 사건 0개. 대조(제때) → 1개. 실DB(읽기 전용): 실시간 신호 323건 중 사건에 안 붙은 것 **2건**(08-01·08-04), 이후 7주 0건 — 드물다
- 재현: `docs/audit/probes/pr02_late_signal_skipped.py` (현재 FAIL)
- 반증조건: 워터마크 뒤에 늦게 들어온 신호를 다시 훑는 경로가 있으면 기각 — 없음
- 이력: [신규] — 독립 리뷰 #2(에이전트 `높음`). 실데이터 빈도로 내림
- 심각도: 중간
- 수정비용: 작음 — 신호에 기록 시각(삽입 순번)을 두고 그걸로 워터마크를 잡거나, 워터마크를 "아직 사건에 안 붙은 신호"로 바꾼다
- 대상: `argus/decide/fusion.py`, (스키마 변경이면) 마이그레이션, 테스트

### F-018 · 영역: 탐지 신뢰성 · 상태: 미처리
- 위치: `argus/detection/rules.py:399` — `        self.baselines.reset()`
- 요약: 절전 복귀(`on_time_gap`)가 탐지기 `reset()` 을 부르고, 룰 엔진은 **베이스라인 전체**(전역·프로그램별·부하 축 6시간)를 비운다. docstring 의 의도는 "지속 조건 시계를 버린다"인데 평소값까지 버리고, 기동 때와 달리 `warm` 으로 다시 채우지 않는다. 베이스라인은 이상값도 배우므로, 창이 60개뿐인 복귀 직후에는 이상이 ~60초 만에 "평소"가 되어 `for` 를 못 채운다 → **복귀 직후 시작한 이상은 안 잡힌다.** 노트북(배포 대상)은 매일 잠든다. 이 PC 는 최근 5주 공백 0건이라 여기서는 안 보인다.
- 근거: 실측 — 제품 룰(`registry.build("rules")`), 평소 30분(메모리 45%) → reset → 한가한 1분 → 메모리 90% 3분: 발화 없음. 대조: reset 없음 → 발화 / reset 뒤 한가한 10분 → 발화
- 재현: `docs/audit/probes/pr04_resume_rebaseline_miss.py` (현재 FAIL)
- 반증조건: 복귀 뒤 베이스라인을 DB 에서 다시 채우는 경로가 있으면 기각 — `on_time_gap` 은 `skip_to_now` 만 한다
- 이력: [신규] — 독립 리뷰 #4(에이전트는 "베이스라인을 버린다"까지, 미탐 여부는 내가 잼). 처음엔 오탐을 재려 했으나 같은 흡수 때문에 성립하지 않았다. 리뷰가 함께 말한 "`on_time_gap` 이 락 없이 다른 스레드에서 돈다"는 **재지 않음**
- 심각도: 중간
- 수정비용: 작음 — reset 에서 베이스라인은 남기고 지속 시계·쿨다운만 버린다(또는 복귀 뒤 `warm` 재호출)
- 대상: `argus/detection/rules.py`, `argus/detection/live.py`, 테스트

### F-019 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/decide/fusion.py:618` — `        self._set_watermark(end)`
- 요약: 워터마크는 신호를 다 처리한 **뒤에만** 옮겨진다. 도중에 `_close`(→ `analyze_incident`)가 예외를 던지면 다음 틱이 같은 신호를 다시 읽고 같은 자리에서 또 죽는다 — 그 사건의 데이터가 계속 예외를 부르면 **융합이 영구 정지하고 이후 모든 사건·알림이 사라진다.** 슈퍼바이저 로그에만 남고 화면에는 없다(→ F-014 와 같은 축).
- 근거: 실측 — 격리 폴더, 사건 A 닫기에만 예외 주입 → 틱 3번 모두 예외, 뒤 신호 B 는 사건이 안 됨. 대조(예외 없음) → 사건 2개. 실로그(3개 파일)에는 융합 실패 기록 없음 — 잠재 결함
- 재현: `docs/audit/probes/pr07_close_error_stalls_fusion.py` (현재 FAIL)
- 반증조건: 사건 단위로 예외를 가두는 경로가 있으면 기각 — 없음
- 이력: [신규] — 독립 리뷰 #7, 내가 재현
- 심각도: 중간
- 수정비용: 작음 — 사건 하나의 닫기를 try 로 가두고 "분석 실패"로 닫은 뒤 진행(사건은 남기고 설명만 비운다 — `peak is None` 경로와 같은 모양)
- 대상: `argus/decide/fusion.py`, 테스트

### F-020 · 영역: 알림 판정 · 상태: 미처리
- 위치: `argus/decide/fusion.py:742` — `            severity = _escalate(severity)`
- 요약: 탐지기가 둘 이상인 사건은 `_merge` 가 불릴 **때마다** 저장된 심각도를 한 단계씩 올린다. 합의는 한 번인데 신호마다 누적된다(info 신호 여럿 → critical). 저장된 심각도는 알림 예산이 그대로 읽는다(`budget.py:64`).
- 근거: 인용 + 실DB(읽기 전용): 사건 285건 중 탐지기 2개 이상 29건, 신호 최고 등급보다 2단계 이상 오른 사건 **0건** — 쿨다운 때문에 한 사건에 신호가 적어 아직 안 나타났다
- 재현: 없음(인용)
- 반증조건: 합의 승격이 사건당 한 번만 일어나면 기각 — 매 병합마다 일어난다
- 이력: [신규] — 독립 리뷰 #5
- 심각도: 낮음
- 수정비용: 작음 — "탐지기 수가 1→2 가 되는 순간"에만 승격
- 대상: `argus/decide/fusion.py`, 테스트

### F-021 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/detection/expr.py:109` — `            return op(float(left), float(right))`
- 요약: `**` 가 허용되어 있고, 음수의 분수 거듭제곱은 복소수가 된다. 비교에서 `TypeError` 가 나는데 룰 평가는 `ExprError` 만 잡아 그 틱의 **룰 전체**가 건너뛰어진다. 지금은 도달 불가 — 동봉 룰에는 `**` 가 없고 사용자 룰은 F-001 때문에 읽히지 않는다. **F-001 을 "사용자 룰을 읽는다"로 고치면 같이 고친다.**
- 근거: 실측(단위) — `expr.evaluate("x > (y ** 0.5)", {"x":1, "y":-4})` → `TypeError`
- 재현: 위 한 줄
- 반증조건: 복소수 결과를 `ExprError` 로 바꾸는 경로가 있으면 기각 — 없음
- 이력: [신규] — 독립 리뷰 #8
- 심각도: 낮음
- 수정비용: 작음
- 대상: `argus/detection/expr.py`, 테스트

### F-022 · 영역: 설정 배선 · 상태: 미처리
- 위치: `argus/decide/fusion.py:62` — `    lag_s: float = 15.0`
- 요약: 설계 규칙 3("임계값·튜닝 상수는 config 한 곳에만") 위반 묶음. 융합의 `lag_s`(15)·`gap_s`(120), 알림 예산의 `per_day`(8)·`min_severity` 가 코드 상수이고 `__main__.py` 가 넘기지 않는다 — YAML 로 튜닝할 수 없다. 덤으로 알림 예산의 "하루"가 `now % 86400`(UTC 자정)이라 한국 시간 **오전 9시**에 초기화된다(`budget.py:45` — `day_start = now - (now % 86400)`).
- 근거: 인용 — `__main__.py:541` 의 `FusionSettings(...)` 인자, `fusion.py:532` 의 `NotificationBudget()`
- 재현: 없음(인용)
- 반증조건: 이 값들을 config 에서 읽는 경로가 있으면 기각 — 없음
- 이력: [신규] — 독립 리뷰 #10(예산·UTC), 융합 쪽은 내가 F-017 을 재다 찾음
- 심각도: 낮음
- 수정비용: 작음 — config 절 추가 + 배선 테스트(**기본값이 아닌 값으로**)
- 대상: `argus/config/defaults.yaml`, `argus/config/loader.py`, `argus/__main__.py`, `argus/decide/budget.py`, 테스트

### F-023 · 영역: 위생 · 상태: 미처리
- 위치: `CLAUDE.md:27` — `` `severity` 가 등급을 두 축(현재 손실·위험)으로 매긴다 ``
- 요약: 현재 손실 축은 2026-08-09 에 기각됐다(`docs/DONE.md` §10). 제품은 위험 축(`leak_risk`)만 쓰고, `combine`·`clock_loss_impact` 는 `tools/grade_probe.py` 에서만 불린다. 구조표가 기각된 설계를 현재형으로 말한다.
- 근거: 인용 — `grep` 결과 제품 코드의 `decide.severity` 사용처는 `procleak.py:30`(`leak_risk`) 하나
- 재현: 없음(인용)
- 반증조건: 제품이 `combine` 을 부르면 기각 — 안 부른다
- 이력: [신규](문서 불일치) — 기각 결정 자체는 [기존]
- 심각도: 낮음
- 수정비용: 작음 — 구조표 한 줄
- 대상: `CLAUDE.md`

### F-024 · 영역: 테스트 사각 · 상태: 미처리
- 위치: `tools/mutation_sweep.py` — `MUTANTS` 중 3개
- 요약: 규칙 무력화 전수(149개 측정, 원문 끊김 11개는 F-002) 중 **3개가 무력화돼도 테스트가 전부 초록**: `foreground_mark_upserts` · `shutdown_test_suppresses_notifications` · `usage_user_only_filter`. 그 규칙을 지키는 단언이 없다(또는 `expect_caught` 표시가 틀렸다).
- 근거: 실측 — `.audit_runs/2026-09-25/`(git 무시) `sweep_jobs.py collect` → 측정 149/149 · 잡힘 146 · 안 잡힘 3
- 재현: `.venv\Scripts\python.exe tools\mutation_sweep.py --only <키>`
- 반증조건: 셋 다 의도된 동치(`expect_caught: False`)면 기각 — 표시부터 본다
- 이력: [신규] — 01:42 기준 2개였고 나머지 54개에서 1개(`usage_user_only_filter`) 추가
- 심각도: 중간
- 수정비용: 작음(키마다 단언 하나)
- 대상: 해당 규칙의 테스트 파일

### F-025 · 영역: 테스트 사각 · 상태: 미처리
- 위치: `argus/detection/` · `argus/decide/` — if-py 변이 생존 줄 (목록은 `.audit_runs/2026-09-25/` 수집 출력)
- 요약: 조건 뒤집기 283개 중 핵심 테스트로 87개 생존 → 전체 테스트(644개) 재확인 **87개 전부 생존**. 줄별 판정: **스모크 블록 안 51**(`python -m …` 스모크가 지킨다 — pytest 몫 아님) · **표시·로그 문구 전용 9**(`rules.py:54`·`287`·`504`·`510`·`512`, `fusion.py:177`·`463`, `trace.py:71`·`73`) · **제품이 안 부르는 코드 3**(`severity.py:113`·`117`·`135` — 기각된 현재 손실 축, F-023) · **제품 판정인데 단언 없음 24**. 24개 중 실시간 경로 9줄은 F-028 로 따로 올렸다. 나머지 15: `fusion.py:180`(방아쇠 지표 수집 — 뒤집으면 `trigger_metrics` 가 빈다, F-016 과 같은 경로) · `fingerprint.py:123`(허용 통계량 검사 — 뒤집으면 정상 호출이 전부 예외인데 초록 = 함수가 테스트 밖) · `rules.py:134·136·138·140`(비교 연산자 `>=`·`<`·`<=`·`==` — `<` 는 동봉 「단일 코어 병목」이 쓴다. 뒤집으면 `!=` 로 떨어져 거의 항상 참) · `rules.py:174`(`swap_total_mb`) · `rules.py:242`(`any` 조합) · `thermal.py:157·161`(**재발화 억제 — 탐지 규칙 1 의 자리**) · `procleak.py:215·391·394`(추적 표본 가드) · `expr.py:146`(조건식) · `base.py:191`(첫 관측 시각)
- 근거: 실측 — `ifpy_recheck.py collect` → 변이 87 · survived 87 · 판정 없음 0 · 대조 survived. **"전부 생존"은 극단값이라 도구를 먼저 의심했다**: 슬롯에서 `argus` 가 사본에서 불러와지는 것을 확인했고, 반드시 잡혀야 할 변이(`fusion.py:600` 사건 분리 조건)를 같은 방식으로 돌려 **caught**(`test_fusion.py` 즉시 실패)를 확인 — 하네스는 유효하다. 앞 단계가 196개를 이미 걸렀으니 남은 87개가 다 사는 것은 자연스럽다
- 재현: `.venv\Scripts\python.exe tools\audit\ifpy_recheck.py collect --out .audit_runs\2026-09-25`
- 반증조건: 줄별 판정에서 동치로 분류되면 그 줄은 기각
- 이력: [신규]
- 심각도: 중간
- 수정비용: 중간 — 단언 15개(모듈별로 묶어). 스모크 블록 51개는 "스모크를 CI 에 넣을지"의 문제로 따로 판단. 하네스에 **양성 대조(잡혀야 할 변이)** 를 기본으로 넣는다 — 이번엔 손으로 했다
- 대상: `tests/` (detection·decide), `tools/audit/ifpy_recheck.py`

### F-028 · 영역: 테스트 사각 · 상태: 수정됨
- 위치: `argus/detection/live.py:126` — `        if rows:`
- 요약: **실시간 탐지 경로가 pytest 에서 재어지지 않는다.** 뒤집어도 전체 테스트가 초록인 줄: `live.py:126`(뒤집으면 **신호가 한 건도 DB 에 안 쓰인다**) · `live.py:81`(기동 예열 — 30분 공백 방지) · `live.py:112`(탐지 결과 거르기) · `live.py:138`(복귀 시 꼬리 이동) · `live.py:93`·`99`(탐지기·꼬리 없음 / 빈 묶음) · `replay_source.py:48`(뒤집으면 꼬리 읽기가 **아무것도 안 읽는다**) · `replay_source.py:61`·`65`. CLAUDE.md 는 "실시간과 리플레이는 같은 경로"라 하지만, 같은 것은 탐지기이고 **실시간 쪽 배선(`DetectionComponent`·`LiveTail`)은 테스트 밖**이다. 상주의 핵심 경로가 조용히 끊겨도 초록이다.
- 근거: 실측 — if-py 변이 전체 테스트 재확인에서 위 7줄 생존
- 재현: `.venv\Scripts\python.exe tools\audit\ifpy_recheck.py collect --out .audit_runs\2026-09-25` 의 `[survived] argus/detection/live.py:*`·`replay_source.py:*`
- 반증조건: 이 경로를 도는 통합 테스트가 있으면 기각 — 변이가 살아남았으니 없다
- 이력: [신규]
- 심각도: 중간
- 수정비용: 작음~중간 — 격리 DB 에 관측 몇 초를 넣고 `DetectionComponent.tick()` 을 돌려 `anomaly_signals` 행을 세는 테스트 하나가 7줄 대부분을 덮는다(예열·복귀는 한두 개 더)
- 대상: `tests/`
- 결과: `tests/test_live_path.py` 5개 — 가짜 시계 + 격리 DB 로 기동(예열) → 꼬리 읽기 → 판정 → `anomaly_signals` 쓰기를 그대로 돈다. 대조 테스트(예열이 없으면 이상이 흡수돼 신호 0)가 예열 배선까지 재고 있음을 먼저 단언한다. **변이 9/9 잡힘**(각 줄을 뒤집어 이 파일만 돌림). 처음엔 8/9 였다 — 복귀 테스트의 `pytest.approx(T + 305)` 가 기본 상대 오차(1e-6)라 17억 초대 시각에서 ±1,790초를 같다고 봐 `skip_to_now` 를 빼도 초록이었다. `abs=1.0` 으로 고쳐 잡힘. 같은 함정이 기존 테스트에 있는지 훑었다 — `test_retention_fault_window.py` 는 시각이 100만 초대라 ±1초로 안전. 전체 649 통과

### F-029 · 영역: 인코딩 · 상태: 미처리
- 위치: `argus/ui/tray.py:573` — `        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()`
- 요약: 창이 곧바로 죽으면 트레이가 자식 stderr 마지막 줄을 풍선으로 보여 주는데, 바이트를 **UTF-8 로 못박아** 읽는다. 창 프로세스(`argus.desktop.app`)는 `logging_setup.setup()` 을 부르지 않아 stderr 가 실행 PC 의 로캘(배포 대상 CP949)로 나온다 → **한글 이유가 깨져 보인다.** 현실적 방아쇠: 사용자가 `settings.yaml` 을 잘못 고쳐 창이 `ConfigError("설정 값이 잘못됐습니다 …")` 로 죽는 순간 — 이유를 읽어야 할 바로 그때다. 이 PC 는 UTF-8 로캘이라 여기서는 안 보인다(전역 인코딩 절).
- 근거: 실측 — 자식 stdio 를 `PYTHONIOENCODING=cp949` 로 되돌려 제품의 `_watch_dashboard` 에 넘김 → "RuntimeError: â ���� ……". 대조(자식 UTF-8) → 원문 그대로
- 재현: `docs/audit/probes/p029_window_error_cp949.py` (현재 FAIL)
- 반증조건: 창 자식의 stderr 인코딩을 부모가 정하는 경로가 있으면 기각 — `_open_dashboard` 의 `env` 에 `PYTHONIOENCODING` 이 없다
- 이력: [신규] — 프로젝트 CLAUDE.md 수집·저장 규칙 6 의 "해당 자리"(warm·calibration)에 이 자리는 없다
- 심각도: 중간
- 수정비용: 작음 — `_open_dashboard` 가 자식 `env` 에 `PYTHONIOENCODING=utf-8` 을 넣는다(부모가 정한다 — 전역 인코딩 규칙 3). CLAUDE.md 규칙 6 의 "해당 자리"에 추가
- 대상: `argus/ui/tray.py`, `CLAUDE.md`, 테스트

### F-026 · 영역: 하위호환 · 상태: 미처리
- 위치: `argus/config/loader.py:726` — `        shutil.copyfile(source, target)`
- 요약: 첫 실행에 `defaults.yaml` **전체**를 사용자 `settings.yaml` 로 복사하고, 이후 사용자 파일을 기본값 위에 덮는다. 사본의 모든 키가 사용자 값이 되므로 **업데이트로 바뀐 기본값(튜닝한 문턱 등)이 기존 설치에 영영 안 먹는다** — 오류도 없다. 설정 파일에 버전 필드가 없어(수집·저장 규칙 4 위반) 어느 판에서 만든 사본인지도 모른다. `rules.yaml` 의 `version: 1` 은 읽는 코드가 없다.
- 근거: 인용 — `ensure_user_config` · `load_settings` 의 `_deep_merge(merged, 사용자)`. 이 PC 의 `settings.yaml` 은 109줄(동봉본 639줄)이라 옛 판 사본으로 보인다 — 이미 갈라져 있다
- 재현: 없음(인용 — 메커니즘이 두 줄이라 프로브가 코드를 되풀이할 뿐이다)
- 반증조건: 사본을 기본값과 다른 키만 남기도록 정리하거나, 버전으로 옮겨 쓰는 경로가 있으면 기각 — 없음
- 이력: [신규] — 독립 리뷰 #6. CHANGELOG 의 "settings.yaml 을 프로그램이 덮어쓰지 않는다"는 주석 보존 결정이지 이 문제를 다룬 것이 아니다
- 심각도: 중간 — **배포가 전제**라 두 번째 릴리스부터 모든 사용자에게 해당
- 수정비용: 중간 — **판단 필요**: (a) 사본 대신 주석만 있는 빈 템플릿을 만든다 (b) 사본에 버전을 넣고 옮겨 쓰기(마이그레이션) (c) 둘 다. 이미 퍼진 사본의 처리도 정해야 한다
- 대상: `argus/config/loader.py`, `argus/config/defaults.yaml`, 테스트

### F-027 · 영역: 조용한 실패 · 상태: 미처리
- 위치: `argus/detection/rules.py:145` — `@lru_cache(maxsize=1)`
- 요약: `machine_variables()` 가 프로파일을 못 읽으면 `{}` 를 돌려주는데, 그 **실패 결과도 수명 내내 캐시**된다. 첫 실행 캘리브레이션이 실패하면(자식 PowerShell 출력 — 인코딩 규칙 6 의 그 자리) `cores`·`ram_gb` 를 쓰는 룰(컨텍스트 스위치 급증 등)이 **재시작 전까지 평가되지 않는다.** docstring 은 "조용히 통과시키지 않는다"고 하지만 룰이 빠진 사실은 아무 데도 안 드러난다.
- 근거: 인용 — `except Exception: return {}` 가 `lru_cache` 안에 있다
- 재현: 없음(인용)
- 반증조건: 빈 결과를 캐시하지 않거나 주기적으로 다시 읽으면 기각 — 안 한다
- 이력: [신규] — 독립 리뷰 #14
- 심각도: 낮음
- 수정비용: 작음 — 빈 결과는 캐시하지 않는다 + 빠진 룰을 `capabilities`/화면에 드러낸다(설계 규칙 4)
- 대상: `argus/detection/rules.py`, 테스트

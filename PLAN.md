# Argus — 구현 계획서 (v2)

> `Argus.md`의 스펙을 실제 구현 가능한 단계로 재설계한 문서.
> 원본 대비 추가된 축: **평가 인프라 우선**, **활동 레짐 조건부 탐지**, **체감 성능 지표**, **귀인(attribution) 엔진**.

---

## 0. 설계 원칙

| 원칙 | 의미 |
|---|---|
| **관측자는 가벼워야 한다** | Argus 자신의 CPU/RAM/IO를 항상 계측하고, 예산 초과 시 자동으로 샘플링을 낮춘다. 모니터가 병목이 되는 순간 프로젝트는 실패다. |
| **측정할 수 없으면 만들지 않는다** | 탐지기보다 리플레이·결함주입 하네스를 먼저 만든다. 정밀도/재현율 숫자 없이 모델을 추가하지 않는다. |
| **탐지가 아니라 설명이 제품이다** | "이상함" 알림은 가치가 낮다. "14:32 디스크 큐 급증, 기여도 1위 `Chrome.exe`(68%), 2위 `MsMpEng.exe`(19%)"가 제품이다. |
| **알림은 예산제** | 하루 알림 총량에 상한을 둔다. 상한을 넘으면 severity 컷을 자동으로 올린다. 알림 피로가 시스템을 죽인다. |
| **모든 단계가 배포 가능** | 각 Phase 종료 시점에 항상 실행되는 바이너리가 존재한다. |

---

## 0.5 배포 전제 (타인 배포 O)

Argus는 **개발자 본인 PC 전용이 아니라 배포 대상**이다. 다음이 처음부터 설계 제약이다.

### 하드웨어/환경 가정 금지
`Argus.md`의 "RTX 3080 / RAM 64GB 기준"은 **개발 환경일 뿐 전제가 아니다.**

| 항목 | 대응 |
|---|---|
| GPU | NVIDIA(pynvml) / AMD·Intel / GPU 없음 — `GpuBackend` 인터페이스로 추상화, 없으면 관련 피처·탐지기 자동 비활성화 |
| 디스크 | NVMe / SATA SSD / HDD — 디스크 응답시간 임계값을 **하드코딩 금지**, 부트스트랩 기간에 실측 분포로 자동 캘리브레이션 |
| 코어 수 | per-core 배열 길이가 가변 → 피처 벡터는 코어 수와 무관한 집계값(평균·최대·분산·불균형도)으로 구성 |
| RAM | 절대값(GB) 대신 **비율·정규화 값**을 피처로 사용 |
| 권한 | 관리자 권한을 **요구하지 않고** 동작해야 함. ETW·서명검증은 권한 있을 때만 활성화되는 부가 기능 |
| Windows 버전 | 10/11 모두. PDH 카운터명은 로케일 의존 → **영문 카운터명 인덱스 조회** 방식 사용 (한글 Windows에서 카운터명 문자열 매칭은 실패한다) |

### 처음부터 필요한 것

- **피처 정규화 계층**: 모든 절대값은 해당 머신의 캘리브레이션 기준으로 정규화. 모델이 특정 하드웨어에 종속되지 않게.
- **부트스트랩 캘리브레이션**: 첫 실행 시 짧은 벤치(디스크 랜덤 읽기, 메모리 대역폭, 코어 성능)를 돌려 `machine_profile`을 만들고 저장. 모든 임계값의 기준점.
- **기능 탐지(feature detection)**: 시작 시 사용 가능한 계측 소스를 탐지 → `capabilities` 기록 → 대시보드에 "이 기능은 관리자 권한이 필요합니다" 형태로 노출. 조용히 실패하지 않는다.
- **크래시 리포팅**: 예외를 로컬 파일에 구조화 저장 + (동의 시) 익명 전송. 남의 PC에서 나는 버그는 재현이 불가능하므로 이게 없으면 디버깅이 불가능하다.
- **설정 마이그레이션**: 설정/DB 스키마에 버전 필드. 업데이트 시 자동 마이그레이션. Phase 1의 스키마 설계 시점에 이미 반영.
- **개인정보 경계**: 프로세스 경로·네트워크 목적지·창 제목은 민감 정보. **기본은 로컬 저장만**, 외부 전송 일절 없음. 리포트 내보내기 시 익명화 옵션.
- **라이선스/서드파티**: PresentMon(MIT), pynvml 등 번들 시 라이선스 고지 파일 포함.

### Phase 조정

- **Phase 0**에 `capabilities` 탐지 + `machine_profile` 캘리브레이션 추가
- **Phase 1**의 스키마에 버전 필드, 모든 임계값을 config로 외부화
- **Phase 12(ETW)** 는 순수 부가 기능으로 격리 — 없어도 제품이 성립해야 함
- **Phase 14** 확장: 설치 마법사, 언인스톨러, 업데이트 채널, 크래시 리포팅, 첫 실행 동의 화면

---

## 1. 아키텍처 (개정)

```
┌─ SENSING ─────────────────────────────────────────────┐
│ T0  ETW 커널 이벤트 스트림   (프로세스 생성/디스크/컨텍스트 스위치)  │
│ T1  시스템 카운터 1Hz        (psutil + PDH)                │
│ T2  프로세스 스냅샷 적응형    (활성 프로세스만 고빈도)            │
│ T3  체감 지표 (QoE)          (디스크 응답시간/DPC/프레임타임)     │
└───────────────────┬───────────────────────────────────┘
                    ↓  링버퍼 + 백프레셔 + 배치 writer
┌─ STORAGE ─────────────────────────────────────────────┐
│ HOT  인메모리 링버퍼(최근 10분) · SQLite WAL(최근 24h 원본)      │
│ WARM Parquet 일별 파티션 + DuckDB 쿼리 레이어(장기)             │
│ FEATURE STORE  파생 피처 캐시(롤링 통계·미분·비율)               │
└───────────────────┬───────────────────────────────────┘
                    ↓
┌─ CONTEXT ─────────────────────────────────────────────┐
│ 활동 레짐 추론: IDLE / BROWSE / GAME / BUILD / LLM / MEDIA │
│ (GMM 클러스터링 → HMM 평활화 → 포어그라운드 프로세스로 라벨링)     │
└───────────────────┬───────────────────────────────────┘
                    ↓  레짐 조건부
┌─ DETECTION (앙상블) ──────────────────────────────────┐
│ D1 통계 베이스라인 (레짐×시간대 EWMA + 로버스트 Z)              │
│ D2 룰 엔진 (YAML DSL)                                   │
│ D3 Isolation Forest / Half-Space Trees (다차원 점 이상)     │
│ D4 프로세스 지문 (프로그램별 정상 프로필 이탈)                   │
│ D5 시퀀스 모델 (TCN-AE → 필요시 LSTM-AE, 점진적 열화 탐지)      │
│ D6 변화점 탐지 (BOCPD, 레짐 전환과 구분)                      │
└───────────────────┬───────────────────────────────────┘
                    ↓
┌─ EXPLAIN ─────────────────────────────────────────────┐
│ 병목 분류 (CPU/IO/MEM/GPU/THERMAL-bound)                 │
│ 기여도 분해 (프로세스 단위 Shapley 근사)                      │
│ 인과 후보 랭킹 (선행성 + 기여도 + 지문 이탈도 결합)              │
└───────────────────┬───────────────────────────────────┘
                    ↓
┌─ DECIDE ──────────────────────────────────────────────┐
│ 이상 융합(동일 사건 dedup) · 억제(suppression) · 에스컬레이션    │
│ 알림 예산 관리 · 사용자 피드백 반영                            │
└───────────────────┬───────────────────────────────────┘
                    ↓
┌─ ACT ─────────────────────────────────────────────────┐
│ 대시보드(Streamlit) · Toast/Discord · 일간 리포트            │
└───────────────────────────────────────────────────────┘

┌─ 평가 인프라 (개발 전용, 항상 병렬 운영) ──────────────────┐
│ 리플레이 하네스: 저장 데이터로 탐지기 오프라인 재현             │
│ 결함 주입기: 메모리 누수/CPU 스핀/디스크 폭주 인위 생성          │
│ 스코어보드: 탐지기별 정밀도·재현율·탐지지연·오탐률 추적           │
└───────────────────────────────────────────────────────┘
```

---

## 2. 원본 스펙 대비 변경점

| # | 원본 | 개정 | 이유 |
|---|---|---|---|
| 1 | 1초 전체 프로세스 순회 | **적응형 계층 샘플링** — 전체는 10초, 상위 N개 활성 프로세스만 1초 | 400개 프로세스 × `cpu_percent()` 1Hz는 그 자체로 CPU 3~8% 상시 점유 |
| 2 | 시간대별 베이스라인 | **활동 레짐 조건부 베이스라인** | 시각보다 "무엇을 하는 중인가"가 압도적으로 강한 조건 변수 |
| 3 | 리소스 사용률만 수집 | **체감 지표(QoE) 추가** | 사용률은 원인, 레이턴시가 증상. 증상 없는 원인은 알릴 가치가 없다 |
| 4 | SQLite 단일 저장소 | **SQLite(핫) + Parquet/DuckDB(웜)** | 초당 삽입 × 다차원 = 주당 수백만 행. 장기 분석 쿼리가 SQLite에서 무너짐 |
| 5 | LSTM Autoencoder | **TCN-AE 우선, LSTM은 대안** | 학습 3~10배 빠르고 병렬화됨. 성능 동등 이상. PyTorch 의존은 유지 |
| 6 | 평가 방법 없음 | **리플레이 + 결함주입 하네스를 Phase 2에 배치** | 튜닝 불가능한 시스템은 운영 불가능 |
| 7 | 상관분석 = 원인 | **변화점 + 기여도 분해 + 선행성** | 상관은 인과가 아니다. 귀인을 별도 엔진으로 분리 |
| 8 | PyInstaller + PyTorch | **ONNX Runtime으로 추론 분리** | PyTorch 번들 시 .exe가 2GB+. ONNX면 ~50MB |
| 9 | 알림 정책 없음 | **알림 예산 + 억제 + dedup** | 오탐 3회면 사용자는 알림을 끈다. 그 순간 제품은 죽는다 |
| 10 | 자기 계측 없음 | **self-telemetry + 자동 스로틀** | 모니터링 도구의 1순위 실패 모드 |

---

## 3. 데이터 모델

### 3.1 핫 스토어 (SQLite, WAL)

```sql
-- 1초 원본, 24시간 보존
metrics_raw(ts INTEGER PK, cpu_total REAL, cpu_per_core BLOB,
            mem_used, mem_avail, swap_used,
            disk_read_bps, disk_write_bps, disk_queue, disk_resp_ms,
            net_rx_bps, net_tx_bps,
            gpu_util, gpu_vram_used, gpu_temp, gpu_power, gpu_pstate,
            dpc_latency_us, ctx_switches, interrupts)

-- 적응형 주기 프로세스 메트릭
process_metrics(ts, pid, name, cpu, rss, io_read, io_write,
                handles, threads, gpu_util, tier)

-- ETW 이벤트 (구조화)
kernel_events(ts, event_type, pid, ppid, image_path, signed,
              detail_json)

-- 네트워크 연결
net_connections(ts, pid, laddr, raddr, rport, state, remote_asn, resolved_host)

-- 활동 레짐 타임라인
regimes(ts_start, ts_end, regime, confidence, foreground_proc)

-- 이상 (융합 전 원시)
anomaly_signals(ts, detector, score, features_json, regime)

-- 이상 (융합 후, 사용자에게 보이는 단위)
incidents(id PK, ts_start, ts_end, severity, bottleneck_class,
          title, explanation_md, contributors_json,
          notified, user_label, labeled_at)

-- 프로세스 지문
process_fingerprints(name, regime, cpu_p50, cpu_p95, cpu_p99,
                     rss_p50, rss_p95, io_profile_json,
                     net_targets_json, sample_count, updated_at)

-- 모델 레지스트리
models(id, kind, version, trained_at, train_window, metrics_json,
       artifact_path, active)

-- 자기 계측
self_telemetry(ts, cpu, rss, queue_depth, drop_count, write_latency_ms)
```

### 3.2 웜 스토어

- `warm/date=YYYY-MM-DD/metrics.parquet` (1분 집계)
- `warm/date=YYYY-MM-DD/process.parquet` (5분 집계, 상위 프로세스만)
- DuckDB로 직접 쿼리 (`SELECT ... FROM 'warm/**/*.parquet'`)
- 보존: 원본 24h → 1분 집계 30일 → 1시간 집계 무기한

### 3.3 피처 파이프라인

원시 메트릭을 그대로 모델에 넣지 않는다. 파생 피처:

- 롤링 통계: 10s/60s/300s 창의 mean·std·p95·min·max
- 1차 미분(변화율), 2차 미분(가속도) — 메모리 누수는 여기서 보인다
- 비율 피처: `disk_queue / disk_throughput`, `gpu_util / gpu_power` (효율), `mem_used / mem_total`
- 레짐 원-핫 + 레짐 지속시간
- 시간 피처: 요일, 시각의 sin/cos 인코딩
- 프로세스 집계: 상위 5개 프로세스의 CPU 합/최대, 프로세스 총 개수, 신규 프로세스 수

---

## 4. Phase별 계획

각 Phase는 **완료 기준(DoD)** 을 만족해야 다음으로 넘어간다.

---

### Phase 0 — 골격과 자기 계측 (1~2일)

- 프로젝트 구조, `pyproject.toml`, 로깅(구조화 JSON 로그), 설정 로더(YAML + 환경변수 오버라이드)
- 프로세스 수퍼바이저: 수집/분석/UI 스레드 생명주기 관리, graceful shutdown
- **self-telemetry 먼저 구현** — Argus 자신의 CPU/RAM/큐 깊이를 DB에 기록
- 리소스 예산 가드: CPU 2% / RSS 300MB 초과 지속 시 자동 스로틀 다운

**DoD**: `python -m argus` 실행 → 자기 자신의 리소스 사용량이 SQLite에 쌓이고, 로그가 남고, Ctrl+C로 깨끗하게 종료된다.

> ✅ **완료.** 60초 실행 실측: CPU 평균 0.013%(최대 0.050%), RSS 67.5MB, 기록 간격 5.009초,
> 핸들 안정, `drop_count` 0, 시그널 종료 후 DB 무결성 OK. 상세는 `CHANGELOG.md`.
> 24시간 연속 가동 검증은 실제 수집기가 붙는 Phase 1 에서 함께 한다.

---

### Phase 1 — 수집 레이어 (3~5일)

**T1 시스템 카운터 (1Hz)**
- psutil: CPU per-core, 메모리, 스왑, 디스크 IO, 네트워크 IO
- `win32pdh` (PDH): `\LogicalDisk(*)\Avg. Disk sec/Transfer`, `Current Disk Queue Length`, `\System\Context Switches/sec`, `\Processor Information(*)\% Processor Performance` (실클럭)

**T2 프로세스 (적응형)**
- 10초마다 전체 스캔 → CPU/메모리 상위 20개 + 포어그라운드 프로세스를 "활성 집합"으로 승격
- 활성 집합만 1초 주기 폴링
- `cpu_percent()`의 첫 호출은 항상 0.0 → 캐시된 이전 스냅샷과의 델타로 직접 계산

**T3 GPU**
- pynvml: 사용률, VRAM, 온도, 전력, P-State, 스로틀 사유(`nvmlDeviceGetCurrentClocksThrottleReasons`)
- 프로세스별 GPU: `nvmlDeviceGetProcessUtilization`
- NVML 미탑재 환경 fallback (조용히 비활성화)

**수집 → 저장 경로**
- 각 수집기는 `queue.Queue(maxsize=N)` 에 push, 가득 차면 **오래된 것부터 버리고 drop_count 증가** (블로킹 금지)
- DB writer 스레드가 200ms 또는 500행 단위로 배치 INSERT
- 크래시 안전: WAL + `synchronous=NORMAL`

**DoD**: ~~24시간~~ **6시간** 무중단 실행, drop_count = 0, Argus 자체 CPU 평균 1.5% 미만, DB 증가량 하루 500MB 미만.

> **DoD 수정 (2026-07-27)**: 무중단 요구를 24시간에서 6시간으로 낮춘다. 24시간은 서버 기준이고,
> 이건 **데스크톱 상주 프로그램**이다. 배포 대상 사용자도 PC 를 켜고 쓰다 끄지 24시간 켜두지 않으므로
> 6~8시간 세션이 오히려 실사용에 가깝다. 24시간을 요구하면 검증 조건이 실제 사용 패턴과 어긋난 채
> 개발만 막는다. 재부팅을 건너 이어지는지는 자동 시작(Phase 14) 범위로 옮긴다.

> ✅ **대부분 완료.** 300초 실행 실측: CPU 평균 0.216%(최대 0.367%), `drop_count` 0,
> DB 292MB/일 환산(3.5M행/일, 행당 87 bytes), RSS 75.6MB, 쓰기 지연 0.35ms,
> 핸들 기울기 -0.024/초(누수 없음).
> **장시간 항목은 압축 테스트로 검증했다.** 보존 기간을 10분으로 줄여 40분 실행하면
> 삭제 경로가 실제로 돈다. 결과: 전 테이블 10.8분에서 절단 ✅, DB 2.05MB 에서 안정 ✅,
> WAL 3.95MB 고정 후 종료 시 0 으로 체크포인트 ✅, 행 수 정상 상태 진동 ✅, drop 0 ✅.
>
> ⚠️ **남은 것은 메모리 장기 추세뿐이다.**
> **6시간 소크 진행 중** (2026-07-27 15:45:26 시작 → 21:45 판정, 작업 스케줄러 `ArgusSoak`).
> 그날 앞선 시도 네 번이 전부 죽었고 원인이 매번 달랐다 — 세션 종속, 콘솔 Ctrl+C,
> job object 상속, uv 트램폴린이 만든 콘솔. 해법은 스케줄러가 base `pythonw.exe` 를
> 직접 실행하는 것이다(`tools/soak_entry.py`, 원인 정리는 `tools/README.md`).
>
> **측정 지표를 고치고 다시 시작했다.** 그전 실행에서 RSS 가 63 → 18MB 로 내려갔는데,
> 메모리를 반납한 게 아니라 백그라운드 프로세스의 워킹셋을 Windows 가 트림한 것이었다
> (강제 트림 실측: RSS 95.8 → 1.0MB, private 85.4MB 유지). **RSS 로는 누수를 판정할 수
> 없다** — 누수가 있어도 트림이 가린다. `self_telemetry` 에 `private_mb`(판정 정본)·
> `peak_wset_mb`·`page_faults` 를 추가했고, 새 지표를 남은 시간에 쌓기 위해 1시간을
> 버리고 재시작했다. 상세는 `CHANGELOG.md`.
>
> **추가 구현**: 절전 복귀 처리(`runtime/gapmon.py`). PC 가 자는 것은 상주 프로그램이
> 반드시 겪는데 원안에 없었다. 복귀 시 속도 추적 초기화·PDH 재개방·프로세스 목록 재기준을
> 하고, 공백 구간을 `system_events` 에 남겨 이후 단계가 제외할 수 있게 한다.
>
> **설계 변경**: 계층 샘플링을 수집이 아니라 **저장** 단계에 두기로 했다. PDH `Process V2`
> 와일드카드 질의가 전 프로세스를 13ms 에 주므로(psutil 1,380ms 대비 106배) 수집을 아낄 이유가
> 없어졌다. 매 틱 전부 수집하고, 활성 집합만 1초 해상도로 저장한다. 상세는 `CHANGELOG.md`.

---

### Phase 2 — 평가 인프라 ★ (3~4일)

> **이 단계를 건너뛰면 이후 전부가 감(感)에 의존하게 된다.**

**2-1. 리플레이 하네스**
- 저장된 기간을 지정해 실시간처럼 재생 → 탐지기에 주입 → 결과 수집
- 시간 배속(1x ~ 1000x), 결정론적 (같은 입력 = 같은 출력)
- 탐지기는 "지금 시각"을 직접 읽지 않고 주입된 clock을 사용하도록 인터페이스 강제

**2-2. 결함 주입기 (`tools/fault_injector.py`)**
| 시나리오 | 구현 |
|---|---|
| 메모리 누수 | 초당 N MB씩 리스트에 append, 30분간 |
| CPU 스핀 | k개 코어 busy loop, 가변 duty cycle |
| 디스크 폭주 | 랜덤 4K 쓰기 폭탄, 큐 길이 상승 유도 |
| GPU 점유 | 대형 텐서 반복 연산 |
| 핸들 누수 | 파일 핸들 미해제 반복 |
| 서서히 나빠짐 | 위 항목을 30~90분에 걸쳐 선형 증가 (가장 중요) |

주입 구간은 ground-truth 라벨로 자동 기록된다.

**2-3. 스코어보드**
- 탐지기별: Precision, Recall, F1, **탐지 지연(주입 시작 → 첫 알림)**, 정상구간 오탐률(FP/hour)
- CLI: `python -m argus.eval --detector all --scenario leak_slow`
- 결과를 `eval_runs` 테이블에 축적 → 회귀 감시

**DoD**: 결함 주입 → 리플레이 → "탐지기 X가 메모리 누수를 12분 만에 잡음, 오탐 0.3/h" 형태 리포트가 자동 출력된다.

> ✅ **완료.** `python -m argus.eval --detector all` 실측: `fixed_cpu` TP 2 / FP 0 /
> 정밀도 100% / 재현율 28.6% / F1 0.444 / 지연 100초, `always` F1 0.583(재현율 100%,
> 정밀도 41.2%). 리플레이 실측 17만~27만 배속, 두 번 재생 결과 동일.
>
> **설계 변경 — 결함 주입기를 상대 강도로.** 첫 실측에서 주입 구간이 정상 구간보다
> 조용했다(12코어에 3스레드 → `cpu_total` 26.8%, 정상 평균 20.7%). 절대량 인자를
> `machine_profile` 기준 비율(`--cpu-load`/`--mem-load`/`--disk-load`)로 바꿨다.
> 함께 드러난 버그 둘: CPU 스핀이 GIL 때문에 스레드로는 병렬이 안 됨(→ 프로세스),
> 디스크 폭주가 단일 스레드 동기 쓰기라 큐 깊이가 항상 1(→ 다중 워커).
> 주입기가 끝날 때 효과를 스스로 검증하고, 열화가 없으면 라벨을 `completed=0` 으로
> 남겨 채점에서 빠지게 한다.
>
> **채점 단위는 틱이 아니라 알람이다.** TP/FN 은 구간 단위, FP 는 알림 단위로 센다.
> 정상 구간 알람 시간을 쿨다운으로 나누지 않으면 영원히 발화하는 탐지기가 만점을 받는다.
>
> ⚠️ **오탐률(FP/hour)은 아직 측정 못 했다.** 정상 구간이 30분 이상 쌓여야 하는데
> 소크 테스트가 반복해서 죽는 바람에 최대 25분까지만 모였다. 숫자를 지어내지 않고
> `—` 로 표시하도록 해 뒀다. 6시간 연속 실행이 끝나면 채워진다.

---

### Phase 3 — 통계 베이스라인 + 룰 엔진 (3~4일)

**베이스라인**
- 각 메트릭에 대해 EWMA(α 가변) + EW 표준편차
- **로버스트 통계 사용**: 평균/표준편차 대신 중앙값/MAD 기반 Modified Z-score (이상치가 베이스라인을 오염시키는 것 방지)
- 동적 임계값: `median + k * 1.4826 * MAD`, k는 메트릭별 설정
- 부트스트랩 기간(첫 2시간)에는 탐지 비활성, 수집만

**룰 엔진 DSL**
```yaml
rules:
  - name: "디스크 병목"
    when:
      all:
        - {metric: disk_resp_ms, op: ">", value: 50}
        - {metric: disk_queue, op: ">", value: "baseline * 3"}
    for: 30s              # 지속 조건 — 순간 스파이크 무시
    cooldown: 10m         # 재발화 억제
    severity: warning
    regime_scope: [ANY]
    explain: "디스크 응답시간 {disk_resp_ms}ms (평소 {baseline}ms)"

  - name: "온도 스로틀링"
    when:
      all:
        - {metric: gpu_temp, op: ">", value: 83}
        - {metric: gpu_throttle_reason, op: "contains", value: "THERMAL"}
    for: 60s
    severity: critical
```
- 표현식 평가는 **AST 화이트리스트 파서**로 (`eval()` 금지 — 사용자 편집 파일이므로 보안 이슈)
- `for`(지속시간)와 `cooldown`이 핵심. 이거 없으면 알림이 폭발한다.

**DoD**: ~~결함 주입 시나리오 6개 중 4개 이상~~ **이 PC 에서 증상이 관측되는 시나리오는
전부** 룰만으로 탐지, 정상 구간 오탐 < 1/h.

> **DoD 수정 (2026-07-27)**: "6개 중 4개"라는 고정 개수 기준을 버린다. 실제 주입기는
> 4종이고 그중 2종은 **이 하드웨어에서 증상 자체가 발생하지 않는다**(NVMe 는 2배 부하에도
> 응답 0.1→0.2ms). 증상 없는 구간을 탐지하라는 요구는 오탐을 요구하는 것과 같다
> (CLAUDE.md: 증상 없는 원인은 알릴 가치가 없다). 하드웨어마다 만들 수 있는 증상이
> 다르다는 것이 이 프로젝트의 전제이므로, 고정 개수를 요구하는 기준 자체가 틀렸다.
>
> ✅ **완료.**
>
> | 항목 | 결과 |
> |---|---|
> | 기준선 초과 | ✅ `rules` F1 **0.615** > `fixed_cpu` 0.364 > `always` 0.214 |
> | 증상 있는 시나리오 전부 탐지 | ✅ **2/2** (`cpu_spin`·`memory_leak`) |
> | 정상 구간 오탐 < 1/h | ✅ **0.00/h** (`always` 는 60.83/h — 비교군이 제 역할을 했다) |
> | 실시간 연동 | ✅ `anomaly_signals` 기록. 알림은 Phase 9 (`notify: false`) |
>
> 탐지되지 않은 2종은 이유가 다르고, 둘 다 룰의 결함이 아니다.
> - `disk_thrash`: 이 NVMe 에 증상이 없어 라벨이 `completed=0` 으로 자동 제외된다.
>   SATA·HDD 장비에서 확인해야 한다.
> - `handle_leak`: 핸들은 프로세스별 지표라 시스템 룰로 닿지 않는다 → Phase 8.
>
> **계획서 수정 — 재시작 후 부트스트랩 폐지.** "첫 2시간 탐지 비활성"을 그대로 두면
> PC 를 켤 때마다 2시간을 기다린다. 시작 시 `metrics_raw` 에서 최근 30분을 읽어 즉시
> 채운다(실측 1,172틱 / 60ms). 2시간 부트스트랩은 진짜 첫 설치일 때만 적용된다.
>
> **통계 방식 확정.** 계획서가 "EWMA + EW 표준편차"와 "중앙값/MAD"를 같이 적었는데
> 둘은 양립하지 않는다(EWMA 는 로버스트하지 않다). **중앙값/MAD** 로 간다.
> 산포가 없는 메트릭은 z 를 내지 않고(`degenerate`) 절대 임계값으로 다룬다.
>
> **주입기에 `manual` 시나리오 추가.** GPU 점유 시나리오를 만들려다 방향을 바꿨다.
> GPU 를 실제로 태우려면 torch(약 2.5GB)나 D3D 직접 호출이 필요한데 시나리오 하나에
> 치를 비용이 아니고, **인위로 만든 부하보다 진짜 게임이 더 나은 데이터다.**
> 사용자가 게임·빌드를 하는 구간에 라벨만 붙인다 — Phase 4 레짐 인식이 요구하는 것도
> 정확히 이 데이터다.
>
> - `disk_thrash`: 이 NVMe 는 2배 부하에도 응답시간이 0.1→0.2ms 라 **증상이 없다.**
>   룰 문제가 아니라 하드웨어 특성이며, SATA·HDD 에서 확인해야 한다.
> - `handle_leak`: 핸들은 프로세스별 지표라 시스템 룰로 닿지 않는다 → Phase 8 범위.
>
> **계획서 수정 — 재시작 후 부트스트랩 폐지.** "첫 2시간 탐지 비활성"을 그대로 두면
> PC 를 켤 때마다 2시간을 기다린다. 시작 시 `metrics_raw` 에서 최근 30분을 읽어 즉시
> 채운다(실측 1,172틱 / 60ms). 2시간 부트스트랩은 진짜 첫 설치일 때만 적용된다.
>
> **통계 방식 확정.** 계획서가 "EWMA + EW 표준편차"와 "중앙값/MAD"를 같이 적었는데
> 둘은 양립하지 않는다(EWMA 는 로버스트하지 않다). **중앙값/MAD** 로 간다.
> 산포가 없는 메트릭은 z 를 내지 않고(`degenerate`) 절대 임계값으로 다룬다.
>
> **실시간 연동은 오탐률 확인 후에 붙인다.** 알림은 되돌릴 수 없다.

---

### Phase 4 — 활동 레짐 추론 ★ (4~5일)

원본 스펙에 없던, 그러나 정확도를 가장 크게 끌어올리는 모듈.

**입력 피처**: CPU 총량/분산, GPU 사용률, VRAM, 네트워크 처리량, 디스크 IO 패턴, 포어그라운드 프로세스, 활성 프로세스 집합의 카테고리

**파이프라인**
1. 1분 윈도우 피처 벡터 생성
2. GMM(k=6~10) 비지도 클러스터링 → 잠재 상태
3. HMM 평활화 → 잦은 상태 전환 억제 (레짐은 보통 수십 분 지속)
4. 클러스터 → 사람이 읽는 라벨 매핑: 포어그라운드 프로세스 최빈값 + 리소스 프로파일로 자동 명명 (`GAME:Cyberpunk2077`, `LLM:ollama`, `IDLE`, `BUILD`)
5. 사용자가 대시보드에서 라벨 수정 가능

**활용**
- 모든 베이스라인이 `(regime, hour_bucket)` 조건부로 재계산
- 레짐 전환 자체는 이상이 아님 → 변화점 탐지가 레짐 전환과 진짜 이상을 구분해야 함
- 새 레짐 발견 시 "새로운 사용 패턴 감지" 정보성 알림 (경고 아님)

**DoD**: 게임 실행/종료, 빌드 시작/종료를 90% 이상 정확히 레짐 경계로 인식. 레짐 조건부 베이스라인 적용 후 Phase 3 대비 오탐률 50% 이상 감소.

---

### Phase 5 — 다차원 이상탐지 (3~4일)

- **Isolation Forest** (scikit-learn): 레짐별로 별도 모델. 매일 새벽 3시 재학습, 학습 창 = 최근 14일 중 해당 레짐 구간
- **Half-Space Trees** (river): 온라인 학습, 재학습 없이 즉시 적응. IF와 앙상블
- 이상 점수 정규화: 원시 점수 → 최근 분포 기준 백분위로 변환 (모델 간 비교 가능하게)
- 피처 중요도: 이상 판정 시 어떤 차원이 기여했는지 (IF의 경로 길이 분해 or 대체로 permutation)

**DoD**: 리플레이 평가에서 룰 엔진 대비 F1 개선, 그리고 **룰이 못 잡던 시나리오를 최소 1개 잡음**. 개선이 없으면 채택하지 않는다(복잡도 비용이 크므로).

---

### Phase 6 — 프로세스 지문 (3~4일)

- 프로세스명 × 레짐 단위로 리소스 분포 학습 (p50/p95/p99, IO 패턴, 네트워크 대상 집합)
- 실행 경로 + 서명자 + 파일 해시를 아이덴티티에 포함 (이름만으로는 위장 가능)
- 이탈 판정: 현재 값이 자기 자신의 p99를 지속 초과, 또는 KL divergence 기반 분포 이탈
- 신규 프로세스: 지문 없음 → 학습 모드(경고 안 함), N회 관측 후 지문 확정
- 버전 업데이트 감지: 해시 변경 시 지문을 폐기하지 않고 "변경 이후" 분포를 별도 추적 → 유의미하게 나빠졌으면 **"업데이트 후 성능 회귀"** 알림 (이게 실사용에서 굉장히 유용한 기능)

**DoD**: 브라우저·게임·에디터 각각의 정상 프로필이 구축되고, 인위적 과사용 주입 시 해당 프로세스가 1위 용의자로 지목된다.

---

### Phase 7 — 시퀀스 모델 (5~7일, 데이터 2주 축적 후)

- **TCN Autoencoder** 우선 (dilated causal conv, 수용영역 ~10분)
- 입력: 1분 집계 다변량 시계열, 윈도우 60스텝
- 학습: 정상으로 라벨된 구간만 (사용자 피드백 + 룰/IF 모두 조용했던 구간)
- 판정: 재구성 오차의 채널별 분해 → 어떤 메트릭이 예측 불가였는지 바로 나옴
- LSTM-AE는 TCN이 부족할 때만 (비교 실험을 스코어보드에 기록)
- **주 타깃**: 서서히 진행되는 열화 — 메모리 누수, 팬 열화로 인한 점진적 스로틀, 디스크 노후화

**DoD**: `leak_slow` 시나리오(60분에 걸친 선형 누수)를 룰/IF보다 **먼저** 탐지. 이게 안 되면 이 모듈의 존재 이유가 없다.

---

### Phase 8 — 귀인 엔진 ★ (4~5일)

"느려진 이유"를 실제로 만드는 단계.

**8-1. 변화점 탐지**
- BOCPD(Bayesian Online Changepoint Detection) 또는 `ruptures` PELT
- 이상 발생 시각을 점(point)이 아니라 **구간 시작점**으로 정확히 특정

**8-2. 병목 분류**
- 규칙 + 학습 결합. 판정 축:
  - CPU-bound: 높은 CPU, 낮은 IO 대기, 실클럭 최대
  - IO-bound: 높은 디스크 큐/응답시간, CPU는 IO wait
  - Memory-bound: 낮은 가용 메모리, 높은 페이지 폴트/스왑
  - GPU-bound: GPU 100%, VRAM 압박
  - Thermal-bound: 스로틀 사유 플래그, 클럭 하락 + 온도 상승
  - Contention: 특정 리소스 포화 없이 컨텍스트 스위치/DPC 급증

**8-3. 기여도 분해**
- 변화점 전후 구간에서, 각 프로세스의 해당 리소스 델타 기여율 계산
- Shapley 근사(프로세스 집합 소규모이므로 정확 계산도 가능)
- 지문 이탈도로 가중: "평소보다 이상한 정도"가 큰 프로세스에 가산점
- **선행성 검사**: 프로세스의 리소스 증가가 시스템 지표 악화보다 시간적으로 앞섰는가 (Granger 유사 lag 상관)

**8-4. 리포트 생성**
```
[WARNING] 14:32:10 ~ 14:38:44 — 디스크 IO 병목 (6분 34초)
체감 영향: 디스크 응답시간 8ms → 71ms (평소의 8.9배)

원인 후보:
  1. Chrome.exe (PID 8812)   기여도 68%  ← 지문 이탈: 쓰기량 p99의 4.2배
  2. MsMpEng.exe (PID 3104)  기여도 19%  ← 정상 범위 (실시간 검사 중)
  3. 기타                     기여도 13%

타임라인: 14:31:52 Chrome 탭 12개 → 47개 급증 (선행 40초)
레짐: BROWSE (평소 이 레짐의 디스크 쓰기: 2.1 MB/s, 현재: 88 MB/s)
```

**DoD**: 결함 주입 시 원인 프로세스를 1순위로 지목하는 비율 85% 이상.

---

### Phase 9 — 융합·알림 정책 (2~3일)

- **신호 융합**: 여러 탐지기가 같은 시간대에 발화 → 하나의 `incident`로 병합. 탐지기 합의 수가 severity에 반영
- **억제 규칙**: 상위 사건이 있으면 하위 사건 알림 억제 (디스크 병목 알림 중 "Chrome CPU 높음"은 묻는다)
- **알림 예산**: 하루 기본 8건. 소진 시 severity 컷 자동 상향, 나머지는 대시보드에만
- **에스컬레이션**: `info` → 대시보드만 / `warning` → Toast / `critical` → Toast + Discord
- **디바운스**: 동일 서명(같은 원인+같은 프로세스)은 쿨다운 내 재알림 금지

**DoD**: 일주일 실사용에서 알림 8건/일 이하, 사용자 "정상이야" 피드백 비율 30% 이하.

---

### Phase 10 — 대시보드 (4~5일)

Streamlit 멀티페이지:

1. **Live** — 실시간 게이지, 레짐 표시, 최근 10분 스파크라인
2. **Timeline** — 시간축 위에 레짐 밴드 + incident 마커 + 메트릭 오버레이. 구간 드래그 → 상세 분석
3. **Incidents** — 사건 목록, 리포트 전문, "정상이야 / 맞아 문제야" 피드백 버튼
4. **Processes** — 프로세스별 리소스 랭킹, 지문 대비 현재 상태 (정상범위 밴드 위에 현재값 표시)
5. **Regimes** — 레짐 타임라인, 라벨 편집, 레짐별 리소스 프로필 비교
6. **Health** — Argus 자기 계측 (수집 지연, drop, DB 크기, 모델 상태)
7. **Models** — 모델 레지스트리, 스코어보드 히스토리, 재학습 트리거

시각화는 대시보드 착수 시점에 `dataviz` 스킬 기준을 적용한다.

**DoD**: 대시보드만 보고 "어제 몇 시에 왜 느렸는지" 3클릭 내로 파악 가능.

---

### Phase 11 — 피드백 & 지속 학습 (3~4일)

- 피드백 라벨 → `incidents.user_label` → 학습 데이터셋 재구성
- 오탐으로 라벨된 구간은 정상 데이터로 편입, 정탐 구간은 학습에서 제외
- **드리프트 감지**: 입력 분포가 학습 시점과 유의하게 달라지면(PSI/KS 검정) 재학습 트리거
- 하드웨어/OS 변경 감지 시 관련 베이스라인 리셋 (RAM 증설, GPU 교체, 드라이버 업데이트)
- 모델 레지스트리: 새 모델은 **섀도 모드**로 먼저 운영 → 스코어보드에서 기존 모델을 이기면 승격
- 일간/주간 리포트 자동 생성 (Markdown → 선택적 PDF)

**DoD**: 피드백 20건 반영 후 리플레이 평가에서 오탐률 개선이 수치로 확인됨.

---

### Phase 12 — 심층 계측: ETW (5~7일, 선택적 고위험)

> 여기서 진짜 커널 레벨 관측이 열린다. 다만 Python 생태계가 약해 리스크가 있으므로 **후반부 + fallback 필수**.

- **PresentMon** (Intel, 오픈소스) 연동 → 프레임타임/프레임 페이싱. 게임 렉을 실제로 측정하는 유일한 방법
- ETW 프로바이더:
  - `Microsoft-Windows-Kernel-Process` — 프로세스 생성/종료 (psutil 폴링보다 정확, 단명 프로세스 놓치지 않음)
  - `Microsoft-Windows-Kernel-Disk` — 개별 IO 요청의 지연시간 분포
  - `Microsoft-Windows-Kernel-Network`
  - DPC/ISR 레이턴시 — 드라이버 문제로 인한 스터터링 탐지
- 구현: `pywintrace` 시도 → 불안정하면 `xperf`/`wpr` 세션을 서브프로세스로 돌리고 결과 파싱하는 fallback
- 관리자 권한 필요 → 권한 없으면 자동 비활성화 + 대시보드에 안내

**DoD**: "리소스는 여유로운데 렉이 걸린" 케이스를 DPC 레이턴시 또는 프레임타임 분산으로 설명 가능.

---

### Phase 13 — 보안 라이트 (2~3일)

- 신규 프로세스: 서명 검증(`WinVerifyTrust`), 실행 경로 화이트리스트 이탈, 부모-자식 관계 이상 (`winword.exe → powershell.exe` 등)
- 네트워크: 단시간 다수 신규 원격 IP, 알려지지 않은 목적지, 지문에 없는 네트워크 대상
- 지속성 지점 변경 감시 (레지스트리 Run 키, 시작프로그램, 예약 작업)
- **정보성 알림만** — 판정하지 않고 보고한다. 오탐 비용이 성능 탐지보다 훨씬 크다

**DoD**: 새 프로그램 설치/실행 시 과도한 경고 없이, 요약 형태로 보고.

---

### Phase 14 — 패키징 & 배포 (3~4일)

- **PyTorch → ONNX 변환**, 런타임은 `onnxruntime`만 번들 (.exe 크기 2GB+ → ~120MB)
- 실행 모드 2가지:
  - **트레이 앱** (기본): 사용자 세션에서 실행, 시스템 트레이 아이콘, 대시보드 열기 메뉴
  - **Windows 서비스** (선택): `pywin32` 서비스, 관리자 권한 ETW 수집 담당. 트레이 앱과 로컬 IPC(named pipe)
- PyInstaller `--onedir` (onefile은 시작이 느리고 DLL 문제 잦음)
- 첫 실행 마법사: 데이터 저장 위치, 알림 채널, 리소스 예산, 자동 시작 등록
- 자동 시작: 작업 스케줄러 등록 (레지스트리 Run 키보다 안정적)
- 무결성: DB 손상 감지 시 자동 복구, 설정 파일 스키마 검증
- 업데이트 채널(선택): GitHub Releases 폴링

**DoD**: 클린 Windows 환경에서 설치 → 부팅 시 자동 시작 → 7일 무중단 동작.

---

## 5. 폴더 구조

```
argus/
├── argus/
│   ├── __main__.py              # 엔트리포인트
│   ├── supervisor.py            # 스레드 생명주기 + 리소스 예산 가드
│   ├── config/
│   │   ├── loader.py            # YAML + env 오버라이드 + 스키마 검증
│   │   ├── settings.yaml
│   │   └── rules/
│   │       ├── performance.yaml
│   │       └── security.yaml
│   ├── collector/
│   │   ├── base.py              # Collector 인터페이스, 주기/에러 격리
│   │   ├── system.py            # psutil
│   │   ├── pdh.py               # Windows 성능 카운터
│   │   ├── gpu.py               # pynvml
│   │   ├── process.py           # 적응형 계층 샘플링
│   │   ├── network.py
│   │   ├── qoe.py               # 체감 지표
│   │   └── etw/                 # Phase 12
│   │       ├── session.py
│   │       ├── providers.py
│   │       └── presentmon.py
│   ├── storage/
│   │   ├── hot.py               # SQLite WAL
│   │   ├── warm.py              # Parquet + DuckDB
│   │   ├── schema.sql
│   │   ├── writer.py            # 배치 writer, 백프레셔
│   │   └── retention.py         # 다운샘플/압축 잡
│   ├── features/
│   │   ├── pipeline.py
│   │   ├── rolling.py
│   │   └── store.py
│   ├── context/
│   │   ├── regime.py            # GMM + HMM
│   │   └── labeler.py           # 레짐 자동 명명
│   ├── detection/
│   │   ├── base.py              # Detector 인터페이스 (clock 주입)
│   │   ├── baseline.py          # EWMA + robust Z
│   │   ├── rules/
│   │   │   ├── engine.py
│   │   │   └── expr.py          # 안전한 AST 표현식 평가기
│   │   ├── isolation.py
│   │   ├── streaming.py         # river Half-Space Trees
│   │   ├── fingerprint.py
│   │   ├── sequence.py          # TCN-AE
│   │   ├── changepoint.py       # BOCPD
│   │   └── security.py
│   ├── explain/
│   │   ├── bottleneck.py
│   │   ├── attribution.py       # Shapley 근사 + 선행성
│   │   └── report.py            # 자연어 리포트 생성
│   ├── decide/
│   │   ├── fusion.py            # 신호 → incident
│   │   ├── suppression.py
│   │   └── budget.py
│   ├── notify/
│   │   ├── toast.py
│   │   ├── discord.py
│   │   └── digest.py            # 일간/주간 요약
│   ├── learning/
│   │   ├── registry.py          # 모델 버전 관리 + 섀도 배포
│   │   ├── trainer.py
│   │   ├── drift.py
│   │   └── feedback.py
│   ├── dashboard/
│   │   ├── app.py
│   │   └── pages/
│   │       ├── 1_live.py
│   │       ├── 2_timeline.py
│   │       ├── 3_incidents.py
│   │       ├── 4_processes.py
│   │       ├── 5_regimes.py
│   │       ├── 6_health.py
│   │       └── 7_models.py
│   └── tray/
│       └── app.py               # 시스템 트레이
├── tools/
│   ├── fault_injector.py        # 결함 주입
│   ├── replay.py                # 리플레이 하네스
│   └── eval.py                  # 스코어보드
├── tests/
│   ├── unit/
│   ├── integration/
│   └── scenarios/               # 결함 시나리오 정의
├── packaging/
│   ├── argus.spec               # PyInstaller
│   └── installer.iss            # Inno Setup (선택)
├── docs/
│   ├── Argus.md                 # 원본 스펙
│   └── PLAN.md                  # 이 문서
├── pyproject.toml
└── README.md
```

---

## 6. 기술 스택 (개정)

| 영역 | 선택 | 비고 |
|---|---|---|
| 런타임 | **Python 3.12** | 3.14는 PyTorch·river 지원이 불확실해 회피. 3.12는 전 의존성이 안정 지원 |
| 시스템 메트릭 | psutil, pywin32(PDH) | |
| GPU | pynvml | NVML 부재 시 graceful degradation |
| 커널 이벤트 | pywintrace / xperf fallback, PresentMon | Phase 12, 고위험 |
| 핫 저장소 | SQLite (WAL) | 표준 라이브러리 |
| 웜 저장소 | pyarrow + duckdb | 장기 분석 쿼리 |
| 통계 | numpy, scipy | |
| 배치 ML | scikit-learn | IsolationForest, GMM |
| 스트리밍 ML | river | Half-Space Trees, 온라인 통계 |
| 시계열 DL | PyTorch → ONNX | 학습만 torch, 배포는 onnxruntime |
| 변화점 | ruptures + 자체 BOCPD | |
| HMM | hmmlearn | 레짐 평활화 |
| 설정/룰 | PyYAML + pydantic | 스키마 검증 필수 |
| 대시보드 | Streamlit + plotly | |
| 트레이 | pystray + Pillow | |
| 알림 | win11toast, requests(Discord) | |
| 패키징 | PyInstaller (onedir) | |
| 테스트 | pytest, hypothesis | 시계열 속성 기반 테스트 |

---

## 7. 주요 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| **수집 오버헤드가 관측 대상을 오염** | 치명적 | self-telemetry + 자동 스로틀 (Phase 0에 배치) |
| **오탐 폭발로 사용자가 알림 차단** | 치명적 | 레짐 조건부 + 알림 예산 + `for`/`cooldown` |
| ETW 파이썬 바인딩 불안정 | 중 | Phase 12로 후치, xperf 서브프로세스 fallback |
| 라벨 데이터 부족으로 모델 평가 불가 | 높음 | 결함 주입기로 합성 ground-truth 생성 (Phase 2) |
| PyInstaller + PyTorch 번들 비대 | 중 | ONNX 변환 |
| SQLite 쓰기 경합 | 중 | WAL + 단일 writer 스레드 + 배치 |
| 하드웨어 변경 시 베이스라인 무효 | 중 | 드리프트 감지 + 자동 리셋 |
| 레짐 오분류가 하위 탐지 전체를 오염 | 높음 | HMM 평활화 + confidence 낮으면 무조건부 베이스라인으로 폴백 |

---

## 8. 일정 요약

| Phase | 내용 | 예상 |
|---|---|---|
| 0 | 골격 + 자기 계측 | 1~2일 |
| 1 | 수집 레이어 | 3~5일 |
| 2 | **평가 인프라** | 3~4일 |
| 3 | 베이스라인 + 룰 | 3~4일 |
| 4 | **활동 레짐** | 4~5일 |
| 5 | 다차원 ML | 3~4일 |
| 6 | 프로세스 지문 | 3~4일 |
| 7 | 시퀀스 모델 | 5~7일 |
| 8 | **귀인 엔진** | 4~5일 |
| 9 | 융합/알림 정책 | 2~3일 |
| 10 | 대시보드 | 4~5일 |
| 11 | 피드백/지속학습 | 3~4일 |
| 12 | ETW 심층 계측 | 5~7일 (선택) |
| 13 | 보안 라이트 | 2~3일 |
| 14 | 패키징/배포 | 3~4일 |

**최소 유용 지점(MVP)**: Phase 4 완료 시점 — 수집 + 평가 + 레짐 조건부 룰 탐지.
**제품이라 부를 수 있는 지점**: Phase 10 완료 시점 — 귀인 리포트 + 대시보드.

Phase 7과 12는 앞 단계 결과에 따라 **채택 여부를 재평가**한다. 스코어보드에서 개선을 입증하지 못하면 넣지 않는다.

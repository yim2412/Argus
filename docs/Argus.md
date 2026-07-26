# PC 성능 이상 탐지 시스템 (Performance Anomaly Detector)

> ⚠️ **최초 스펙 문서 (원본, 보존용).** 현행 설계는 [`../PLAN.md`](../PLAN.md) 이며, 충돌 시 `PLAN.md` 가 우선한다.
>
> 이 문서와 다르게 결정된 주요 항목:
> - "RTX 3080 / RAM 64GB 기준"은 **전제가 아니라 개발 환경일 뿐** — 배포 대상이므로 하드웨어를 가정하지 않는다
> - 1초 전체 프로세스 순회 → **적응형 계층 샘플링** (오버헤드)
> - 시간대별 베이스라인 → **활동 레짐 조건부 베이스라인**
> - LSTM Autoencoder → **TCN Autoencoder 우선** (LSTM은 대안)
> - SQLite 단일 저장소 → **SQLite(핫) + Parquet/DuckDB(웜)**
> - PyTorch 번들 → **ONNX Runtime** (exe 크기)
> - 추가된 것: 평가 인프라(결함주입·리플레이), 체감 성능 지표(QoE), 귀인 엔진, 알림 예산

## 프로젝트 개요

개인 PC/서버의 리소스 사용 패턴을 실시간으로 학습하고, 통계 + 머신러닝 기반으로
"평소와 다른" 성능 이상을 탐지하는 상주형 모니터링 프로그램. 성능 병목(리소스 낭비/
병목) 탐지에 우선순위를 두고, 경량 보안 탐지 기능을 서브로 포함한다.

**최종 목표**: Windows에서 상주 실행되는 .exe 프로그램 (PyInstaller 패키징)

**대상 하드웨어**: RTX 3080, RAM 64GB, Windows 환경 기준으로 설계

---

## 아키텍처

```
[수집 레이어] → [저장소: SQLite] → [탐지 엔진] → [대시보드 / 알림]
   Collector      시계열 DB          다층 이상탐지      Streamlit + Webhook
```

- 수집과 분석은 별도 백그라운드 스레드로 분리 (메인 리소스 부담 최소화)
- 수집 주기: 1초 실시간 스냅샷 + 1분 집계 (장기 트렌드용 다운샘플링)

---

## 기술 스택

| 영역 | 기술 |
|---|---|
| 시스템 메트릭 | psutil |
| GPU 메트릭 | pynvml (NVIDIA) |
| 저장소 | SQLite (WAL 모드) |
| 통계/ML | scikit-learn (Isolation Forest), PyTorch (LSTM Autoencoder) |
| 대시보드 | Streamlit |
| 알림 | Windows Toast, Discord Webhook |
| 패키징 | PyInstaller |
| 설정/룰 | YAML |

---

## 모듈별 상세 스펙

### 1. 수집 레이어 (`collector/`)

**목표**: 다차원 리소스 메트릭을 지속적으로 수집

- 시스템: CPU per-core 사용률, 메모리(사용/가용/스왑), 디스크 I/O(읽기/쓰기 속도, 큐 길이), 네트워크 처리량(송/수신)
- GPU: RTX 3080 사용률, VRAM 사용량, 온도, 전력 소비 (pynvml)
- 프로세스별: PID별 CPU/메모리/핸들 수/스레드 수, 신규 프로세스 생성 이벤트 로그
- 네트워크 연결: 열린 포트, 활성 연결의 원격 IP/도메인

**산출물**: `collector.py` — 백그라운드 스레드로 1초마다 스냅샷을 큐에 적재 → DB writer가 배치 삽입

---

### 2. 저장소 (`storage/`)

- SQLite, WAL 모드 (동시 읽기/쓰기 지원)
- 스키마 예시:
  - `metrics_raw` (timestamp, cpu_per_core, mem_used, disk_io, net_io, gpu_util, gpu_vram, gpu_temp)
  - `process_metrics` (timestamp, pid, name, cpu, mem, io_read, io_write)
  - `anomalies` (timestamp, type, severity, source_module, description, resolved_by_user)
  - `process_fingerprints` (process_name, baseline_cpu_range, baseline_mem_range, baseline_network_targets, updated_at)
- 데이터 보존 정책: 최근 1시간 원본(초 단위) → 1분 집계로 다운샘플 → 7일 이후 1시간 집계로 재압축

---

### 3. 탐지 엔진 (`detection/`) — 핵심 모듈

**3-1. 베이스라인 학습기** (`baseline.py`)
- 요일 × 시간대별 정상 리소스 사용 패턴 학습 (예: 평일 오전 vs 새벽 게임 시간대 구분)
- EWMA(지수가중이동평균) + 동적 임계값 산출

**3-2. Isolation Forest 이상탐지** (`isolation_detector.py`)
- CPU/RAM/디스크/GPU/네트워크를 다차원 벡터로 구성해 전역 이상치 탐지
- scikit-learn `IsolationForest`, 주기적 재학습 (예: 매일 새벽)

**3-3. LSTM Autoencoder** (`sequence_detector.py`)
- 리소스 사용 시계열 "흐름" 패턴 학습 → 재구성 오차 기반 이상 탐지
- 점진적 성능 저하(메모리 누수, 서서히 증가하는 CPU 점유 등) 조기 탐지에 특화
- 최소 며칠치 데이터 축적 후 학습 시작 (개발 순서상 후순위)

**3-4. 병목 분류기** (`bottleneck_classifier.py`)
- 이상 탐지 시 원인을 CPU-bound / IO-bound / Memory-bound / GPU-bound로 자동 분류
- 각 리소스 지표의 상대적 기여도 기반 규칙 + 간단한 분류 모델

**3-5. 프로세스 지문 시스템** (`fingerprint.py`)
- 프로그램별(게임, 로컬 LLM, 브라우저 등) 정상 리소스 프로필 구축
- 평소 대비 과도한 리소스 사용 시 플래깅
- `process_fingerprints` 테이블에 지속 업데이트

**3-6. 상관분석 엔진** (`correlation_engine.py`)
- YAML로 정의된 규칙 기반 패턴 매칭
- 예시 규칙:
  ```yaml
  - name: "디스크 병목 의심"
    conditions:
      - metric: disk_queue_length
        op: ">"
        value: baseline * 3
      - metric: disk_response_time
        op: ">"
        threshold: 50ms
    severity: warning
  ```
- 사용자가 직접 규칙 추가 가능하도록 설계

**3-7. 경량 보안 탐지** (`security_lite.py`, 서브 기능)
- 비정상 프로세스 생성 패턴 (알려지지 않은 실행 경로, 서명되지 않은 바이너리)
- 의심스러운 외부 연결 (짧은 시간 내 다수 신규 IP 연결 등)
- 성능 탐지 대비 우선순위 낮음 — Phase 후반에 구현

---

### 4. 대시보드 (`dashboard/`)

- Streamlit 기반 실시간 화면
  - 리소스 히트맵 (시간대별 CPU/GPU/메모리)
  - 이상 탐지 타임라인 (색상으로 severity 구분)
  - "느려진 이유" 자동 리포트 (예: "14:32 디스크 I/O 급증, 원인 추정: Chrome")
  - 프로세스별 리소스 사용 랭킹 + 트렌드 그래프
  - 프로세스 지문 뷰어 (정상 범위 대비 현재 상태)

---

### 5. 알림 & 피드백 루프 (`notifier/`, `feedback/`)

- Windows Toast 알림 / Discord Webhook 연동
- 사용자 피드백 UI: "이건 정상이야" 버튼 → 해당 이상 케이스를 정상으로 재라벨링 → 모델 재학습 큐에 반영
- 일간/주간 요약 리포트 자동 생성 (마크다운 또는 PDF)

---

## 개발 순서 (권장)

1. **수집기 + DB 스키마** — 기반 구축, 실행 시 데이터가 쌓이는 것부터 확인
2. **베이스라인 통계 + 룰 기반 탐지** — 빠른 MVP, 눈으로 결과 확인 가능
3. **Isolation Forest 통합** — 다차원 이상탐지 추가
4. **프로세스 지문 시스템** — 프로그램별 정상 프로필 구축
5. **LSTM Autoencoder** — 데이터 축적 후 시계열 이상탐지 (가장 복잡, 후순위)
6. **상관분석 엔진 + 대시보드** — 규칙 기반 원인 분석 + 시각화
7. **알림 + 피드백 루프 + .exe 패키징** — 최종 배포 형태로 마무리

각 단계가 끝날 때마다 "돌아가는 프로그램"이 존재하도록 순서를 잡았습니다.

---

## 클로드 코드 시작 프롬프트 예시

```
이 스펙 문서를 기반으로 프로젝트 초기 구조를 잡아줘.
1단계(수집기 + DB 스키마)부터 시작해서, 실행하면 실제로 메트릭이
SQLite에 쌓이는 것까지 구현해줘. Python 3.11 기준, psutil과 pynvml
사용. 프로젝트 폴더 구조도 잡아줘.
```

---

## 참고: 프로젝트 폴더 구조 (제안)

```
pc-anomaly-detector/
├── collector/
│   ├── system_collector.py
│   ├── gpu_collector.py
│   └── process_collector.py
├── storage/
│   ├── db.py
│   └── schema.sql
├── detection/
│   ├── baseline.py
│   ├── isolation_detector.py
│   ├── sequence_detector.py
│   ├── bottleneck_classifier.py
│   ├── fingerprint.py
│   ├── correlation_engine.py
│   ├── security_lite.py
│   └── rules/
│       └── correlation_rules.yaml
├── dashboard/
│   └── app.py
├── notifier/
│   └── notifier.py
├── feedback/
│   └── feedback_loop.py
├── config/
│   └── settings.yaml
├── main.py
├── requirements.txt
└── README.md
```

# Argus

Windows PC 성능 이상 탐지 프로그램. 리소스 사용 패턴을 학습해 **"평소와 다른" 상태를 감지하고,
느려진 원인 프로세스를 지목**한다.

> **상태: Phase 3 완료** — 실제 메트릭이 SQLite에 쌓이고, **룰 엔진이 이상을 탐지합니다.**
> 리플레이 평가에서 F1 0.615 / 정밀도 100% / 오탐 0.00건per시간 (기준선 0.364·0.214 대비).
> **알림은 아직 보내지 않습니다** — `anomaly_signals` 에 기록만 하며, 알림 정책은 Phase 9입니다.
> 진행 상황은 [`CHANGELOG.md`](CHANGELOG.md) 참조.
>
> 실측(300초 기준): 자체 CPU 평균 0.22% · RSS 76MB · DB 292MB/일 · 유실 0건
> 리플레이 17만~27만 배속 (6시간 구간을 0.1초에 재생)

---

## 무엇을 하는가

일반적인 모니터링 도구(작업 관리자, HWMonitor 등)는 **지금 값이 얼마인지**를 보여준다.
Argus는 **그 값이 이 PC 기준으로 이상한지**를 판단하고, 이상하다면 **왜 그런지** 설명한다.

```
[WARNING] 14:32:10 ~ 14:38:44 — 디스크 IO 병목 (6분 34초)
체감 영향: 디스크 응답시간 8ms → 71ms (평소의 8.9배)

원인 후보:
  1. Chrome.exe (PID 8812)   기여도 68%  ← 지문 이탈: 쓰기량 p99의 4.2배
  2. MsMpEng.exe (PID 3104)  기여도 19%  ← 정상 범위 (실시간 검사 중)

타임라인: 14:31:52 Chrome 탭 12개 → 47개 급증 (선행 40초)
레짐: BROWSE (평소 이 레짐의 디스크 쓰기: 2.1 MB/s, 현재: 88 MB/s)
```

### 설계상 특징

- **활동 레짐 인식** — "새벽 2시"보다 "지금 게임 중인가 / 빌드 중인가"가 훨씬 강한 기준이다.
  리소스 패턴에서 활동 상태를 추론하고, 모든 판단을 그 조건 아래에서 한다.
- **체감 성능 측정** — CPU 사용률은 느림의 *원인*이지 느림 자체가 아니다.
  디스크 응답시간·DPC 레이턴시·프레임타임을 함께 봐서 "리소스는 여유로운데 렉걸림"을 잡는다.
- **원인 귀인** — 상관관계가 아니라, 변화점 탐지 + 프로세스별 기여도 분해 + 시간적 선행성으로
  용의자를 랭킹한다.
- **점진적 열화 탐지** — 메모리 누수, 팬 열화로 인한 스로틀링처럼 며칠에 걸쳐 서서히 나빠지는 것.
- **하드웨어 무가정** — 첫 실행 시 캘리브레이션으로 이 PC의 기준선을 만든다.
  임계값이 코드에 박혀 있지 않아 HDD든 NVMe든 각자의 정상 범위가 적용된다.

---

## 실행

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

.venv\Scripts\python.exe -m argus --check          # 기동 점검 (권한·GPU·DB·기준선)
.venv\Scripts\python.exe -m argus                  # 상주 실행 (Ctrl+C 종료)
.venv\Scripts\python.exe -m argus --duration 60    # 60초만 실행
```

첫 실행 시 `%APPDATA%\Argus\` 에 DB·설정·로그가 만들어지고, 약 3초간 하드웨어 기준선을 측정합니다.
개발 중 실사용 데이터를 건드리지 않으려면 `ARGUS_DATA_DIR` 로 위치를 옮기세요.

### 수집 중인 것

| 테이블 | 주기 | 내용 |
|---|---|---|
| `metrics_raw` | 1초 | CPU per-core·메모리·디스크 IO/**응답시간**·네트워크·컨텍스트 스위치·**실효 클럭** |
| `gpu_metrics` | 1초 | 사용률·VRAM·온도·전력·클럭·**스로틀 사유** (NVIDIA) |
| `process_metrics` | 1초 / 30초 | 활성 집합은 1초, 전체 프로세스는 30초 해상도 |
| `process_events` | 이벤트 | 프로세스 생성·종료 (부모·실행 경로 포함) |
| `net_connections` | 30초 | 활성 연결 (DNS 역조회 없음) |
| `self_telemetry` | 5초 | Argus 자신의 CPU·메모리·핸들·큐·유실 |

굵게 표시한 것이 일반 모니터링 도구에 없는 "증상" 지표입니다. 사용률은 원인이고 응답시간이 증상인데,
증상 없는 원인은 알릴 가치가 없습니다.

### 검증

```powershell
.venv\Scripts\python.exe -m argus.collector.process   # 모듈별 스모크 ([OK]/[FAIL])
.venv\Scripts\python.exe -m pytest tests -q           # 정상 종료·DB 무결성
```

각 모듈은 단독 실행하면 자기 점검 결과를 출력합니다.

### 탐지기 채점 (Phase 2)

이상 탐지에는 정답이 없어서, 결함을 인위로 주입해 **정답 구간을 만듭니다.**

```powershell
python tools\fault_injector.py cpu_spin --duration 120 --ramp   # 결함 주입 (정답 라벨 생성)
.venv\Scripts\python.exe -m argus.eval --detector all --save    # 리플레이 + 채점
```

저장된 데이터를 17만 배속으로 재생하므로 "며칠 돌려봐야 아는" 것을 몇 초에 확인합니다.
탐지기별 정밀도·재현율·F1·탐지 지연·오탐률이 나오고, 이력은 `eval_runs` 에 쌓여
회귀 감시에 쓰입니다. 자세한 내용은 [`tools/README.md`](tools/README.md).

---

## 문서

| 문서 | 내용 |
|---|---|
| [`PLAN.md`](PLAN.md) | 아키텍처, 데이터 모델, Phase별 개발 계획 (**현행 기준**) |
| [`CHANGELOG.md`](CHANGELOG.md) | 변경 이력과 Phase별 실측 결과 |
| [`CLAUDE.md`](CLAUDE.md) | 프로젝트 규칙 — 설계 원칙, 수집·탐지 규칙, 작업 방식 |
| [`docs/Argus.md`](docs/Argus.md) | 최초 스펙 문서 (원본, 충돌 시 `PLAN.md` 우선) |

---

## 기술 스택 (예정)

Python 3.11 · psutil · pywin32(PDH) · pynvml · SQLite(WAL) · DuckDB/Parquet ·
scikit-learn · river · PyTorch→ONNX Runtime · Streamlit · PyInstaller

---

## 요구사항 (예정)

- Windows 10 / 11
- 관리자 권한 **불필요** (ETW 기반 심층 계측만 선택적으로 필요)
- GPU 없어도 동작 (NVIDIA면 GPU 메트릭 추가 수집)

---

## 개인정보

수집한 모든 데이터(프로세스명·실행 경로·네트워크 목적지 포함)는 **로컬에만 저장**되며
외부로 전송되지 않는다. 저장 위치는 `%APPDATA%\Argus\`.

---

## 라이선스

MIT — [`LICENSE`](LICENSE)

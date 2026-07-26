# Argus

Windows PC 성능 이상 탐지 프로그램. 리소스 사용 패턴을 학습해 **"평소와 다른" 상태를 감지하고,
느려진 원인 프로세스를 지목**한다.

> **상태: Phase 0 완료** — 실행은 되지만 아직 **자기 자신만 관측합니다.**
> 시스템 메트릭 수집은 Phase 1, 이상 탐지는 Phase 3부터입니다. 진행 상황은 [`CHANGELOG.md`](CHANGELOG.md) 참조.

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

### 검증

```powershell
.venv\Scripts\python.exe -m argus.storage.hot      # 모듈별 스모크 ([OK]/[FAIL])
.venv\Scripts\python.exe -m pytest tests -q        # 정상 종료·DB 무결성
```

각 모듈은 단독 실행하면 자기 점검 결과를 출력합니다.

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

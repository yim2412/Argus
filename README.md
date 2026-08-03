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

**같은 데이터 폴더에는 하나만 뜹니다.** 이미 실행 중일 때 다시 실행하면 오류 없이
그대로 종료합니다(자동 시작과 수동 실행이 겹치는 것은 흔한 일이라 실패로 다루지 않습니다).
`ARGUS_DATA_DIR` 로 분리한 인스턴스는 서로 막지 않습니다.

### 네이티브 창 (개발 중)

```powershell
.venv\Scripts\python.exe -m argus.desktop.app
.venv\Scripts\python.exe -m argus.desktop.app --seconds 12   # 검증: 그린 표본 수를 보고
```

대시보드를 브라우저가 아니라 데스크톱 앱(PySide6)으로 옮기는 중입니다. **다섯 페이지
(실시간 · 프로세스 · 타임라인 · 사건 · 자기 상태)가 모두 동작합니다.** Streamlit 판은
비교·되돌리기를 위해 당분간 함께 둡니다.

**상주와 별도 프로세스입니다** — 창이 죽어도 수집은 계속됩니다. 개발 중 창 위치는
`ARGUS_UI_SCREEN`(0-기반 모니터 번호)으로 지정합니다. 배포 exe 에는 영향이 없습니다.

### exe 로 묶기

```powershell
.venv\Scripts\pyinstaller.exe packaging\argus.spec --noconfirm      # 상주
dist\argus\argus.exe --check

.venv\Scripts\pyinstaller.exe packaging\argus_ui.spec --noconfirm   # 네이티브 창
dist\argus-ui\argus-ui.exe --seconds 12
```

실측: 상주 **195MB** · 창 **298MB**. 창 쪽이 큰 것은 Qt 런타임 때문이고,
`argus_ui.spec` 의 `excludes` 가 WebEngine·3D·Multimedia 를 걷어내 설치본 643MB 에서
그만큼 줄인 결과입니다.

`--onedir` 입니다(onefile 은 시작이 느리고 DLL 문제가 잦습니다). 실측 **빌드 37~48초 ·
`dist` 195MB · exe 9.9MB**. 격리 실행으로 검증하려면 `ARGUS_DATA_DIR` 을 지정하세요.

**아직 상주 본체만 묶습니다.** 대시보드(Streamlit)는 자체 자원 파일과 동적 import 가
많아 별도 문제이고, 그것 때문에 전체 빌드가 막히면 무엇이 진짜 장애물인지 알 수 없게
됩니다. 트레이·알림 발송도 미구현이라 지금 exe 는 **콘솔 창이 뜬 채 수집만 합니다.**

spec 안에서는 **상대경로를 쓰지 마세요.** spec 의 상대경로는 spec 파일 위치가 아니라
빌드를 실행한 작업 디렉터리 기준으로 풀립니다. 첫 빌드에서 `pathex=[".."]` 가 프로젝트의
부모를 가리켜 `argus` 패키지가 통째로 빠졌고, **빌드는 성공한 뒤 실행에서만** 죽었습니다.

### 로그온 시 자동 시작

상주 모니터는 켜져 있어야 데이터가 쌓입니다. 켜는 것을 잊으면 그날치가 비고, 베이스라인·
지문처럼 축적이 필요한 기능이 그만큼 늦어집니다. 작업 스케줄러에 등록해 두세요.

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Start
```

관리자 권한은 필요 없습니다. 로그온 1분 뒤에 시작하고, 창은 뜨지 않습니다.

```powershell
schtasks /query /tn "Argus" /fo LIST     # 상태
.venv\Scripts\python.exe -m argus --stop # 중지 (권장 — 정상 종료)
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall
```

**중지는 `--stop` 으로 합니다.** `%APPDATA%\Argus\STOP` 파일을 만들면 상주 인스턴스가
몇 초 안에 그것을 보고 스스로 정리하고 내려갑니다(파일을 직접 만들어도 동일합니다).
`schtasks /end` 는 강제 종료라 DB 는 안전하지만 **정상 종료가 `unclean_shutdown` 으로
기록되어** 사후 진단이 크래시와 구분되지 않습니다. Windows 에는 콘솔 없이 도는
프로세스에 종료를 곱게 전할 방법이 없어서 파일을 신호로 씁니다.

### 수집 중인 것

| 테이블 | 주기 | 내용 |
|---|---|---|
| `metrics_raw` | 1초 | CPU per-core·메모리·디스크 IO/**응답시간**·네트워크·컨텍스트 스위치·**실효 클럭** |
| `gpu_metrics` | 1초 | 사용률·VRAM·온도·전력·클럭·**스로틀 사유** (NVIDIA) |
| `process_metrics` | 1초 / 30초 | 활성 집합은 1초, 전체 프로세스는 30초 해상도 |
| `process_events` | 이벤트 | 프로세스 생성·종료 (부모·실행 경로 포함) |
| `net_connections` | 30초 | 활성 연결 (DNS 역조회 없음) |
| `self_telemetry` | 5초 | Argus 자신의 CPU·메모리(RSS/**private**)·핸들·큐·유실·**실행 중 컴포넌트** |
| `metrics_1m` | 1분 | 위를 접은 장기 보존용 집계 — 평균·최대·p95·**표준편차**·코어 불균형·포어그라운드 |

굵게 표시한 것이 일반 모니터링 도구에 없는 "증상" 지표입니다. 사용률은 원인이고 응답시간이 증상인데,
증상 없는 원인은 알릴 가치가 없습니다.

### 저장 구조

```
metrics_raw(초, 24시간)     → metrics_1m(1분)  → warm/date=YYYY-MM-DD/metrics.parquet
process_metrics(초, 24시간) → process_5m(5분)  → warm/date=YYYY-MM-DD/process.parquet
```

초 단위 원본은 하루 292MB 라 오래 둘 수 없고, 레짐·ML 단계는 며칠~2주치를 요구합니다.
접으면 **지표 하루 434KB(압축비 59:1), 프로세스 하루 705KB** 라 몇 년을 둬도 무해합니다.

프로세스는 프로그램 이름 단위로 `p50/p95/p99` 를 남깁니다 — 이게 Phase 6 지문의 입력입니다.
**시각별로 먼저 합친 뒤 분위수를 냅니다**: 크롬 탭 30개를 그냥 모으면 "개별 프로세스의
p95"가 되어 "크롬 전체"와 30배 어긋납니다.

**삭제는 롤업 워터마크를 넘지 못합니다.** 접히기 전에 지워진 원본은 어디에도 남지 않기
때문입니다. 롤업이 멈추면 삭제도 멈추고 DB 가 커지는데, 그게 맞는 선택입니다 — 디스크가
차는 건 눈에 보이지만 지워진 2주치는 되돌릴 수 없습니다.

### 대시보드 (Phase 10)

```powershell
.venv\Scripts\python.exe -m argus.dashboard      # http://localhost:8501
```

| 페이지 | 내용 |
|---|---|
| **실시간** | 지금 이 순간과 최근 10분 |
| **타임라인** | 1분 집계 시간축 · 결함 주입 구간(정답)과 탐지 신호가 겹쳐 보인다 |
| **프로세스** | 프로세스별 사용량 랭킹, 포어그라운드, 개별 추이 |
| **자기 상태** | Argus 자신의 상태 — 관측자가 병목이 되고 있지 않은지 |
| **사건** | 무슨 일이 있었고 **왜** 그랬는가 · 피드백 버튼 |

준비 중: 레짐(Phase 4-B) · 모델(학습 후). 데이터가 없는 페이지를 미리 만들어
두지 않는 이유는 "아직 없음"과 "고장남"이 구분되지 않기 때문입니다.

> 사이드바 메뉴 이름은 `pages/` 의 **파일명**에서 나옵니다. 그래서 한국어 UI 를 위해
> 파일명 자체가 한국어입니다 — 파일 안의 제목만 바꾸면 메뉴는 영어로 남습니다.

대시보드는 **읽기 전용 연결**로 붙고 조회 결과를 캐시합니다. "PC 가 느린 이유"를 찾는
프로그램의 화면이 CPU 를 먹으면 자기가 만든 이상을 자기가 관측하게 됩니다.

### 검증

```powershell
.venv\Scripts\python.exe -m argus.collector.process   # 모듈별 스모크 ([OK]/[FAIL])
.venv\Scripts\python.exe -m argus.storage.history     # 핫+웜 병합 조회 (중복 계상 검출)
.venv\Scripts\python.exe -m pytest tests -q           # 정상 종료·DB 무결성·대시보드 렌더
```

각 모듈은 단독 실행하면 자기 점검 결과를 출력합니다.

### 다음 작업 판정

데이터가 쌓여야 시작할 수 있는 작업들이 있습니다. **날짜를 세지 않고 표본을 셉니다** —
2시간 켜 둔 날과 12시간 켜 둔 날은 같은 "1일"이 아닙니다.

```powershell
.venv\Scripts\python.exe tools\readiness.py
```

가상환경으로 실행해야 합니다. 롤업은 이틀이 지나면 Parquet(웜 스토어)으로 옮겨가고
SQLite 에서는 지워지므로, 판정하려면 두 계층을 합쳐 읽어야 하고 거기에 DuckDB 가 쓰입니다.

### 탐지기 채점 (Phase 2)

이상 탐지에는 정답이 없어서, 결함을 인위로 주입해 **정답 구간을 만듭니다.**

```powershell
python tools\fault_injector.py cpu_spin --duration 120 --ramp   # 결함 주입 (정답 라벨 생성)
.venv\Scripts\python.exe -m argus.eval --detector all --save    # 리플레이 + 채점
```

핸들 누수는 개방 속도를 낮춰야 실제 누수에 가까운 모양이 나옵니다. 기본 20/s 는 2분이면
상한에 닿아 뒤가 평평해집니다.

```powershell
python tools\fault_injector.py handle_leak --duration 720 --handle-rate 6
```

### 탐지기

| 이름 | 보는 것 |
|---|---|
| `rules` | 시스템 전역 메트릭 위의 룰 엔진 (`rules.yaml`). 지속 조건·쿨다운 강제 |
| `procleak` | **프로세스별** 자원이 줄지 않고 계속 자라는 것 (핸들·메모리 누수) |
| (지문) | 프로그램별 "평소 범위"를 학습해 `procleak` 의 오탐을 가립니다 |
| `always` · `fixed_*` | 비교 기준선. 새 탐지기가 이걸 못 이기면 채택하지 않습니다 |

`procleak` 이 따로 있는 이유는 핸들 누수가 **전역 메트릭에 드러나지 않기** 때문입니다.
한 프로세스가 핸들 8천 개를 쥐어도 CPU·메모리·디스크는 평온합니다. 같은 구간 채점에서
`rules` 의 F1 이 0.000, `procleak` 이 0.400 이었습니다.

지문은 "평소에도 핸들을 4천 개씩 쓰는 프로그램"을 가려 줍니다. 녹화 프로그램이 핸들을
1,539개까지 늘린 것은 그 프로그램에겐 평소 범위(p99 13,323)라 알리지 않고, 같은 프로그램이
메모리를 2,164MB 까지 늘린 것은 평소(p99 915MB)를 넘으므로 알립니다. **지문이 없는 프로그램은
아무것도 막지 않습니다** — 누수는 대개 새로 뜬 프로세스에서 생기기 때문입니다.

```powershell
.venv\Scripts\python.exe -m argus.detection.fingerprint   # 지문 생성·확인
```

### 귀인 채점 (Phase 8)

```powershell
.venv\Scripts\python.exe -m argus.eval --attribution
```

"이상 감지"가 아니라 **"무엇 때문인지"** 를 채점합니다 — 결함 주입 구간에서 원인
프로세스를 1순위로 지목했는가. 실측 **88.9%(8/9)**, 3위 이내 100%.

```
CPU 병목 — 14:41:35 ~ 14:45:59 (4분 23초)
체감 영향: CPU 21% → 96% (평소의 8.6σ)

원인 후보:
  1. python (프로세스 12개)   기여도 82%  (40.2%, +29.3%)  ← 거의 동시
  2. discord (프로세스 6개)   기여도  5%  (3.7%, +1.8%)
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

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

**알림은 소리 없이 뜹니다.** 상주의 알림은 사용자가 부른 것이 아니라 끼어드는 것이라,
소리까지 나면 오탐 한 번의 비용이 훨씬 커집니다. 소리를 원하면 `%APPDATA%\Argus\settings.yaml`
의 `general.notify_sound` 를 `true` 로 두세요. **다른 앱의 알림음은 Windows 설정 소관입니다**
(설정 → 시스템 → 알림 → 해당 앱 → "알림 소리 재생").

### 창

```powershell
.venv\Scripts\python.exe -m argus.desktop.app
.venv\Scripts\python.exe -m argus.desktop.app --seconds 12   # 검증: 그린 표본 수를 보고
```

화면은 브라우저가 아니라 데스크톱 앱(PySide6)입니다. 창은 지난번에 닫은 크기로 열리고
(처음에는 HD 1280×720),
탭은 두 묶음입니다 — **보기**(실시간 · 프로세스 · 사건 · 사용시간 · 일일 리포트)와
**진단**(타임라인 · 자기 상태 · 설정). 트레이 메뉴에서도 열 수 있습니다.

**맨 위 한 줄이 답입니다.** "지금 괜찮은가"에 정상 / 진행 중인 사건 / 수집 멈춤 중
하나로 답하고, 사건이면 눌러서 그 사건을 폅니다. 아래 수치와 차트는 그 답의 근거이고,
지금 병목인 자원의 타일에는 "← 지금 병목"이 붙습니다. **판정은 탐지기가 한 것을
그대로 옮깁니다** — 창에는 임계값이 없습니다(하드웨어마다 다르므로).

브라우저 판(Streamlit)은 2026-08-09 에 지웠습니다. 조회 계층(`argus/dashboard/data.py`)만
남아 창이 그대로 씁니다 — 그 계층이 UI 를 모르게 만들어 둔 덕에 화면을 갈아 끼우는 데
조회 코드를 건드리지 않았습니다.

**상주와 별도 프로세스입니다** — 창이 죽어도 수집은 계속됩니다. 개발 중 창 위치는
`ARGUS_UI_SCREEN`(0-기반 모니터 번호)으로 지정합니다. 배포 exe 에는 영향이 없습니다.

### exe 로 묶기

```powershell
.venv\Scripts\pyinstaller.exe packaging\argus.spec --noconfirm      # 상주
dist\argus\argus.exe --check --out check.txt                        # 결과는 파일로

.venv\Scripts\pyinstaller.exe packaging\argus_ui.spec --noconfirm   # 네이티브 창
dist\argus-ui\argus-ui.exe --seconds 12
```

> **상주 exe 는 창 없는 빌드입니다**(`console=False`, 2026-08-17). 상주라서 콘솔
> 창이 뜨면 계속 떠 있고, 매일 도는 스냅샷 작업이 하루 한 번 창을 번쩍입니다 —
> 첫 배포 대상이 **창모드 게임 + 마우스 매크로**를 24시간 돌리는 노트북이라 그
> 번쩍임 하나가 매크로를 흔듭니다.
>
> 그래서 **`print` 가 아무 데도 가지 않습니다.** windowed 빌드에는 `sys.stdout` 이
> 아예 없어서, 결과를 보려면 `--out <파일>` 을 줘야 합니다. `_ensure_std_streams()`
> 가 `sys.stdout=None` 으로 `AttributeError` 가 나는 것을 막습니다.

실측(2026-08-09): 상주 **198MB** · 창 **299MB**. 창 쪽이 큰 것은 Qt 런타임 때문이고,
`argus_ui.spec` 의 `excludes` 가 WebEngine·3D·Multimedia 를 걷어내 설치본 643MB 에서
그만큼 줄인 결과입니다.

> **상주에는 PySide6 를 넣지 않습니다.** `collect_submodules("argus")` 가 `argus.desktop`
> 까지 끌어와 한동안 **상주 exe 에 Qt 102MB 가 들어 있었습니다**(301MB). 창은 별도
> 프로세스로만 뜨므로 이름으로 걸러내고 `excludes` 에도 넣었습니다 — 거르기만 하면
> 다른 경로로 다시 딸려 옵니다.

`--onedir` 입니다(onefile 은 시작이 느리고 DLL 문제가 잦습니다). 실측 **빌드 30~73초 ·
`dist` 198MB · exe 11.8MB**. 격리 실행으로 검증하려면 `ARGUS_DATA_DIR` 을 지정하세요.

**상주 exe 에는 창을 넣지 않습니다.** 별도 프로세스여야 창이 죽어도 수집이 계속되고,
배포의 최소 단위는 "수집하고 탐지하는 상주"입니다. 트레이 메뉴가 `argus-ui.exe` 를 찾아
띄웁니다. 트레이와 알림 발송은 켜져 있습니다.

### 다른 기계에 배포하고 데이터를 회수하기

두 번째 기계를 붙이면 `PLAN.md` 의 여러 판정("근거가 이 PC 한 대뿐"에 막혀 있던 것)이
열립니다. 저쪽은 수집만 하고, 조회·분석·수정은 전부 개발 PC 에서 합니다.

```powershell
# 개발 PC — 배포 폴더를 만든다 (dist\argus 를 그대로 복사하면 안 됩니다)
powershell -ExecutionPolicy Bypass -File packaging\make_deploy.ps1

# 관측 기계 — 그 폴더를 옮긴 뒤 안에서 한 번만
powershell -ExecutionPolicy Bypass -File 설치.ps1

# 개발 PC — 켤 때마다
powershell -ExecutionPolicy Bypass -File tools\fetch_snapshots.ps1 -From \\<IP>\ArgusSnap
```

`--export-findings` 가 판정용 표만 뽑습니다 — 실측 **439MB → 18MB, 0.45초**이고
상주가 쓰는 중에도 일관된 스냅샷이 나옵니다(읽기 트랜잭션 하나로 복사). 네트워크
목적지(`net_connections`)와 초 단위 원본은 담지 않습니다. 자세한 것은
`tools/README.md` 의 "다른 기계에 배포하고 데이터를 회수한다".

**아이콘은 두 경로로 들어갑니다.** `icon=` 은 exe 파일 자체의 아이콘(탐색기가 보는 것)
이고, `datas` 의 `assets/argus.ico` 는 트레이가 런타임에 `LoadImage` 로 읽는 파일입니다.
한쪽만 넣으면 **예외 없이 절반만 Argus 로 보입니다** — exe 는 제 아이콘인데 트레이는
시스템 느낌표이거나, 그 반대입니다. 아이콘 자체는 `python tools\make_icon.py` 로
다시 만들 수 있습니다(Pillow 필요 · 산출물은 커밋되어 있어 빌드에는 불필요).

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
| `heap_census` | 5분 | 파이썬 힙에 무엇이 쌓이는지 — 객체 수·**컨테이너 원소 수**·상위 타입 |
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

### 화면 (Phase 10)

```powershell
.venv\Scripts\python.exe -m argus.desktop.app
```

| 페이지 | 내용 |
|---|---|
| **실시간** | 지금 이 순간과 최근 10분 |
| **타임라인** | 1분 집계 시간축 · 결함 주입 구간(정답)과 탐지 신호가 겹쳐 보인다 |
| **프로세스** | 프로세스별 사용량 랭킹, 포어그라운드, 개별 추이 |
| **사용시간** | 프로그램별 누적 실행 시간 · 실행 횟수 — 관측 시간 대비 비율로 |
| | 이름 옆에 **그게 무슨 프로그램인지**를 적습니다(`conhost` → 콘솔 창 호스트) |
| | 기본은 **내가 쓴 프로그램만** — 창을 띄운 적이 있는 것. 체크를 끄면 전부 나옵니다 |
| **일일 리포트** | 하루를 요약합니다 — 쓴 시간 · 분류별 · 상위 5개 · 시간대별 |
| | **사용시간과 세는 것이 다릅니다**: 저기는 켜져 있던 시간, 여기는 **앞에 놓여 있던** 시간. 게임을 켜 두고 자리를 비운 시간은 여기서 빠집니다 |
| | 하루가 끝난 뒤 만들어집니다(첫 결과는 자정 이후). 분류는 `usage.categories`, 시간대는 `usage.slots` 로 바꿉니다 |
| **자기 상태** | Argus 자신의 상태 — 관측자가 병목이 되고 있지 않은지 |
| **사건** | 무슨 일이 있었고 **왜** 그랬는가 · 피드백 버튼 |
| | 묻는 것은 **사실이 아니라 쓸모**입니다 — "그때 실제로 그랬나"가 아니라 "이 알림이 쓸모 있었나". 애매하면 `모르겠음` 으로 넘깁니다 |
| | 판정이 분명한 것은 **기계가 먼저 답합니다**(`·정상` 처럼 점이 붙습니다). 사람이 답하면 그쪽이 이기고, 근거는 상세에 적혀 있습니다 |
| **설정** | 알림 켜고 끄기 — 재시작 없이 반영됩니다 |

준비 중: 레짐(Phase 4-B) · 모델(학습 후). 데이터가 없는 페이지를 미리 만들어
두지 않는 이유는 "아직 없음"과 "고장남"이 구분되지 않기 때문입니다.

DB 가 없으면 창이 **찾은 경로와 시작 방법을 말합니다.** "데이터 없음"과 "고장남"이
같은 화면이면 처음 켠 사용자는 무엇이 잘못됐는지 알 수 없습니다.

화면은 **읽기 전용 연결**로 붙고 조회 결과를 캐시합니다. "PC 가 느린 이유"를 찾는
프로그램의 화면이 CPU 를 먹으면 자기가 만든 이상을 자기가 관측하게 됩니다.

### 검증

```powershell
.venv\Scripts\python.exe -m argus.collector.process   # 모듈별 스모크 ([OK]/[FAIL])
.venv\Scripts\python.exe -m argus.storage.history     # 핫+웜 병합 조회 (중복 계상 검출)
.venv\Scripts\python.exe -m pytest tests -q           # 정상 종료·DB 무결성·조회 계층·창
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
scikit-learn · river · PyTorch→ONNX Runtime · PySide6/pyqtgraph · PyInstaller

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

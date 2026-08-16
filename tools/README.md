# tools/

개발·검증용 도구. 배포 exe 에는 포함되지 않는다.

| 파일 | 용도 |
|---|---|
| `fault_injector.py` | 결함을 인위로 주입해 정답 라벨(`fault_injections`)을 만든다 |
| `soak_entry.py` | 창 없는 진입점. 스케줄러가 base pythonw 로 실행한다 (검증·상시 공용) |
| `soak_task.xml` | 장시간 상주 **검증**용 등록 정의 (수동 트리거 전용) |
| `argus_task.xml` | 로그온 **자동 시작**용 등록 정의. 경로는 아래 스크립트가 채운다 |
| `install_autostart.ps1` | 자동 시작 등록·해제 |
| `backfill_rollup.py` | 워터마크보다 과거에 남은 원본을 소급 집계한다 |
| `readiness.py` | 데이터 대기 중인 작업을 지금 시작해도 되는지 판정한다 |
| `autolabel_backfill.py` | 이미 쌓인 알림에 자동 라벨(`auto_label`)을 매긴다 |
| `ramp_replay.py` | 느린 누수(램프)를 실제 시계열에 얹어 **제품 탐지기**가 잡는지 본다. 60분 주입을 태우기 전에 먼저 묻는다 |
| `eval_snapshot.py` | 평가 입력을 고정해 둔다 (채점 재현용) |
| `inject_progress.py` | 주입 진행·판정을 `procleak.judge()` **자체**로 본다 (`--watch`) |
| `rescore_incidents.py` | 이미 닫힌 사건을 지금 코드로 다시 분석한다 |
| `mutation_sweep.py` | 규칙을 무력화했다 되돌리며 테스트가 잡는지 잰다 |
| `pyc_audit.py` | 소스와 `__pycache__` 의 모듈 상수가 어긋났는지 검사한다 |
| `grade_probe.py` | 등급 판정을 입력별로 찔러 본다 |
| `ui_snapshot.py` | 창을 띄우지 않고 `QWidget.grab()` 으로 화면을 뜬다 |
| `make_icon.py` | 트레이 아이콘 생성 |

## 착수 조건 판정

```powershell
.venv\Scripts\python.exe tools\readiness.py
```

`PLAN.md` 의 남은 작업 여럿이 "데이터가 며칠 쌓인 뒤"를 전제한다. **날짜를 세지 않는다** —
하루 종일 게임한 3일과 유휴 위주의 3일은 같은 3일이 아니다. 각 작업의 조건을 실제 DB 에서
세어 `[OK]`/`[대기]` 로 찍고, 부족하면 무엇이 얼마나 부족한지 말한다.

읽기 전용으로 열기 때문에 상주 인스턴스를 멈출 필요가 없다. 판정은 전부 **영구 보존되는
계층**(`metrics_1m`·`process_5m`·`incidents`)만 본다 — 원본은 24시간 창이라 며칠을
기다려도 늘지 않아 대기의 근거가 될 수 없다.

## 자동 라벨 백필

```powershell
.venv\Scripts\python.exe tools\autolabel_backfill.py            # 미리보기
.venv\Scripts\python.exe tools\autolabel_backfill.py --apply    # 저장
```

상주 경로는 앞으로 닫히는 사건만 판정한다. 이미 쌓인 것들은 이 도구가 채운다 —
자동 라벨을 넣은 이유가 바로 그 밀린 알림들이다.

**판정 로직을 복사하지 않고 `decide.autolabel.apply` 를 그대로 부른다.** 규칙이 두
곳에 있으면 조용히 갈리고, 그때 "제품이 매긴 라벨"과 "백필이 매긴 라벨"이 같은 칸에
섞여 구분할 방법이 없어진다. 사람이 답한 사건·안 나간 알림·결함 주입 구간은 건드리지
않는다(그 판정도 `apply` 안에 한 번만 있다).

## 롤업 누락 점검·소급 집계

```powershell
.venv\Scripts\python.exe tools\backfill_rollup.py            # 진단만
.venv\Scripts\python.exe tools\backfill_rollup.py --apply    # 실제로 접는다 (Argus 정지 필요)
```

롤업은 워터마크 **이후**만 접는다. 워터마크가 앞서 있는데 그 뒤에 원본이 남아 있으면
그 구간은 접힐 기회를 영영 얻지 못한 채 보존 기한에 지워진다. 보존 정리의 "워터마크를
넘지 못한다"는 보호 장치는 이 경우 통과 상태라 막아 주지 못한다.

**누락은 행 수가 아니라 버킷으로 센다.** 원본은 접힌 뒤에도 보존 기한까지 남으므로,
워터마크 이전 행을 세면 정상인 롤업도 수십만 행으로 나와 누락과 구분되지 않는다.

`--apply` 는 상주 인스턴스가 돌고 있으면 거부한다(`InstanceLock`). 같은 DB 에 롤업이
둘 돌면 워터마크가 서로 덮인다. `schtasks /end /tn "Argus"` 로 멈추고 돌린 뒤 다시 켠다.

## 자동 시작

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Start
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall
```

`soak_task.xml` 과 XML 을 공유하지 않는다. 검증용은 수동 트리거 전용이고 상시 운영은
로그온 트리거 + 실패 시 재시작이 필요해, 한쪽을 고치면 다른 쪽이 조용히 바뀐다.

스크립트가 하는 일 중 **직접 쓰면 틀리기 쉬운 것 세 가지**:

1. **파이썬 경로를 `.venv\pyvenv.cfg` 의 `home` 에서 읽는다.** XML 에 박아 두면 다른
   PC 에서 깨진다. 읽을 때 `-Encoding UTF8` 이 필수다 — PowerShell 5.1 의 기본값은 시스템
   ANSI 라 사용자 이름에 한글이 든 경로가 깨지고, 그러면 폴백으로 흘러 **다른 파이썬**이
   선택된다. 실제로 Microsoft Store 스텁(3.13)이 잡혀 venv(3.12)의 `pydantic_core` 가
   ABI 불일치로 죽었다.
2. **등록 전에 `--check` 로 기동을 확인한다.** 등록만 해 두면 실패를 다음 로그온까지 모른다.
   단 점검은 **콘솔형 `python.exe`** 로 돌린다. `pythonw.exe` 는 GUI 서브시스템이라
   PowerShell 이 종료를 기다리지 않아 `$LASTEXITCODE` 가 비고, 점검이 늘 통과한 것처럼 보인다.
3. **네이티브 exe 에 `2>&1` 을 붙이지 않는다.** PowerShell 5.1 은 stderr 한 줄을
   ErrorRecord 로 감싸며 종료 코드 0 에도 `$?` 를 false 로 만든다. 판정은 `$LASTEXITCODE` 로만.

**`.ps1` 은 UTF-8 BOM 으로 저장한다.** BOM 이 없으면 PowerShell 5.1 이 시스템 ANSI 로 읽어
한글 문자열의 바이트가 깨지고, **파서가 따옴표 경계를 잘못 잡아 뒤쪽 `Write-Host` 줄들이
코드가 아니라 문자열로 삼켜진 채 그대로 출력된다.** 등록은 성공하므로 오류로 보이지 않는다.
편집기가 BOM 을 떼면 조용히 재발한다.

**작업 정의 XML 의 주석에 자리표시자를 예시로 적지 않는다.** 스크립트의 미치환 검사는
주석까지 훑기 때문에, 설명하려고 적어 둔 `{{ }}` 가 등록을 막는다.

## 중복 실행

`argus/runtime/singleton.py` 가 named mutex 로 막는다. 이름에 데이터 디렉터리 해시를 섞어
`ARGUS_DATA_DIR` 로 분리한 인스턴스는 서로 막지 않는다. 이미 돌고 있으면 **종료 코드 0** 으로
물러난다 — 자동 시작과 수동 실행이 겹치는 것은 실수가 아니다. `--check` 는 아무것도 쓰지
않으므로 상주 인스턴스와 함께 돌 수 있고, 진단용으로 굳이 겹쳐 띄우려면 `--allow-multi`.

    python -m argus.runtime.singleton    # 뮤텍스·PID 파일 폴백 양쪽 스모크

## 결함 주입

```powershell
python tools\fault_injector.py --list
python tools\fault_injector.py cpu_spin --duration 120 --ramp
python tools\fault_injector.py memory_leak --duration 300 --mem-load 0.3

# 같은 총량을 여러 프로세스로 나눠 붙잡는다 (프로세스별 문턱을 피하는 누수)
python tools\fault_injector.py memory_leak_spread --ramp --duration 3600

# 부하를 만들지 않고 지금 하는 일에 라벨만 붙인다 (게임·빌드·인코딩)
python tools\fault_injector.py manual --label GAME --duration 1800
```

**`memory_leak_spread` 는 크기와 길이를 기본값에서 바꾸지 않는다.** `--ramp
--duration 3600` 이 만드는 `+18%p` 가 **시스템 룰의 사각지대 한가운데**다 —
`+30%p` 를 넘으면 시스템 룰이 잡고, 60분보다 짧으면(30분·20분) 램프가 빨라져 역시
시스템 룰이 잡는다. `--spread-procs` 를 늘려도 소용없다. 시스템 룰은 `mem_percent`
**전체**를 보므로 프로세스를 몇 개로 쪼개든 무관하기 때문이다. 2026-08-16 실측.

> **다만 `procleak` 의 사각지대는 아니다 (2026-08-17).** 그룹 축 문턱을 고친 뒤
> 같은 주입 3건(`#65`·`#66`·`#67`)을 전부 16~24분에 잡는다. 08-16 에 이 조합이
> "아무도 못 잡는다"로 보였던 것은 그룹 축 문턱이 PID별 문턱과 갈려 있어서였다
> (경위는 `CHANGELOG.md` 08-17). **이 시나리오를 미탐 표본으로 쓰지 말 것** —
> 지금은 탐지된다.

**주입기를 `Start-Job`·`Stop-Process` 로 다루지 않는다.** 둘 다 `finally` 를 돌리지
않아 라벨이 `ts_end` 없이 열린 채 남는다. 열린 라벨은 보존 정리가 그 구간을 계속
붙들게 하고, PID 가 재사용되면 남의 데이터가 섞여 읽힌다. 포그라운드로 부르고,
중단이 필요하면 종료 뒤 `ts_end` 를 손으로 닫으면서 **사후 기입이라는 사실과 효과
검증이 돌지 않았다는 것을 `notes` 에 적는다.** 끝나면 늘 확인한다:

```sql
SELECT COUNT(*) FROM fault_injections WHERE ts_end IS NULL;   -- 0 이어야 한다
```

**`--ramp` 는 30분 이상으로 걸어야 의미가 있다.** 계획서가 말하는 점진적 열화는
30~90분짜리다. 90초 램프는 임계 위에 22초만 머물러 "느린 열화를 잡는가"라는 질문에
답하지 못한다 — 실제로 그 때문에 룰 엔진이 기준선을 못 넘는 것처럼 보였다.

**강도는 전부 이 PC 능력 대비 비율이다** (`--cpu-load` / `--mem-load` / `--disk-load`).
절대량을 쓰면 PC 마다 전혀 다른 세기의 이상이 되고, 빠른 PC 에서는 아무 일도 일어나지
않는다 — `machine_profile` 에서 코어 수·RAM·실측 순차쓰기를 읽어 환산한다.

주입기는 끝나면 **효과를 검증한다.** 관측 가능한 열화가 없으면 `[FAIL]` 로 끝나고
라벨을 `completed=0` 으로 남겨 스코어보드가 자동으로 제외한다. 라벨만 있고 열화가
없는 구간을 채점에 쓰면 모든 탐지기가 오답으로 나오기 때문이다.

## 램프 리플레이 — 주입 전에 제품에게 먼저 묻는다

```powershell
# 60분 램프를 제품 탐지기에 태운다 (기본: per_program on·off 둘 다)
.venv\Scripts\python.exe tools\ramp_replay.py --minutes 60 --detector rules,procleak --with-process

# 여러 속도를 한 번에 — 대조군 없이는 "룰이 죽은 것"과 구분되지 않는다
.venv\Scripts\python.exe tools\ramp_replay.py --minutes 5,10,30,60

# 분산 누수 / 실주입과 같은 곡선 / 창 고정
.venv\Scripts\python.exe tools\ramp_replay.py --spread 8 --quadratic --end 1786846500
```

**60분을 태우기 전에 리플레이가 먼저 답한다.** 2026-08-16 에 이 도구가 실제 주입
70분을 한 번 취소시켰고("`procleak` 이 8.9분에 잡는다"), 주입 시간 단축 실험 30분을
6분으로 대체했다.

**옵션은 전부 "제품과 같게 맞추기" 위한 것이다.**

| 옵션 | 왜 필요한가 |
|---|---|
| `--detector rules,procleak` | 상주는 둘을 돌린다. `rules` 만 재고 "룰이 못 잡는다"고 결론 내면 틀린다 |
| `--with-process` | `procleak` 은 `rss_mb` 를 본다. 시스템 지표에만 램프를 얹으면 그 탐지기에게는 **아무 일도 일어나지 않은 것과 같다** |
| `--quadratic` | 주입기의 `--ramp` 는 `intensity` 가 선형이라 **누적이 `t²`** 다. 선형을 가정하면 15분 지점에서 3.6%p 어긋난다 |
| `--spread N` | 같은 총량을 N개 프로세스로. `procleak` 의 프로세스별 문턱을 피하는 형태를 잰다 |
| `--end` | 창을 고정한다. 기본값은 "최근 N분"이라 **게임이 켜지면 수치가 통째로 바뀌어 재현이 안 된다** |
| `--saw-drop` · `--saw-period` | 톱니의 되돌림 비율·주기. **한 점만 재고 사각지대라 부르지 않기 위해 있다** — 2026-08-17 에 되돌림 4점을 훑어 미탐이 *연속적*임을 보이고서야 "`monotonic_ratio` 가 설계대로 거른 것"이라 말할 수 있었다. 한 점(30%)만 봤을 때는 사각지대처럼 보였다 |

**결과를 읽을 때 세 가지를 조심한다. 셋 다 실제로 정반대 결론을 낼 뻔했다.**

1. **`per_program on/off` 가 갈리면 "불확실"이다.** 프로그램별 베이스라인은 그
   프로그램이 **포어그라운드일 때만** 학습하므로, 판정이 *그때 무엇을 쓰고 있었나*에
   달린다. 리플레이 워밍 구간이 게임 중이면 chrome 베이스라인이 백지인 채로 램프를
   맞아 실주입과 정반대 결과가 나온다. 그래서 기본값이 `--per-program both` 다.
2. **발화한 룰의 이름까지 읽는다.** 입력이 실제 시계열이라 램프와 무관한 룰이
   얼마든지 발화한다. 한 틱에 여러 룰이 걸리면 `rules.py` 가 **심각도가 가장 높은
   하나만 대표로 세우므로**, 「메모리 이상 증가」(`info`)가 「CPU 과부하」에 가려
   *발화했는데도 미탐으로 집계*될 수 있다.
3. **대조군 없이 "미탐"을 결론으로 삼지 않는다.** 짧은 램프(`--minutes 5`)나
   `--delta 0` 을 함께 돌려 탐지기가 살아 있음을 먼저 보인다.

## 장시간 실행은 반드시 작업 스케줄러로 띄운다

```powershell
# 등록 (최초 1회) — XML 은 UTF-16 이어야 schtasks 가 받는다
$tmp = "$env:TEMP\argus_soak.xml"
[System.IO.File]::WriteAllText($tmp, [System.IO.File]::ReadAllText("tools\soak_task.xml"), [System.Text.Encoding]::Unicode)
schtasks /create /tn "ArgusSoak" /xml $tmp /f

schtasks /run /tn "ArgusSoak"    # 시작
schtasks /end /tn "ArgusSoak"    # 중지
```

**2026-07-27 에 같은 날 세 번 죽고 나서야 알아낸 것이다.** 세 번 다 "백그라운드로
띄웠다"고 생각했지만 한 번도 분리된 적이 없었다.

| 시도 | 죽은 이유 |
|---|---|
| `Start-Process -WindowStyle Hidden` | 부모 세션이 닫히자 같이 죽음 (16분). 숨김은 분리가 아니다 |
| 스케줄러 + `.cmd` 래퍼 | 래퍼의 cmd.exe 가 대화형 콘솔에 붙어 Ctrl+C 를 같이 맞음 (로그 끝에 `^C`) |
| `DETACHED_PROCESS` + 새 프로세스 그룹 | 콘솔 신호는 막았지만 **job object 상속은 못 막음**. 개발 셸의 job 이 닫히자 같이 종료 |
| 스케줄러 + `.venv\Scripts\pythonw.exe` | uv 의 venv 실행파일은 **트램폴린**이라 콘솔형 `python.exe` 를 다시 띄운다. 콘솔 창이 생겼고 종료 코드 `0xC000013A`(STATUS_CONTROL_C_EXIT) 로 죽음 |

핵심은 세 가지다.

1. **개발 셸이 만든 프로세스는 그 셸의 job object 를 상속한다.** `DETACHED_PROCESS`
   로도 끊어지지 않는다. job 밖에서 태어나야 하고, 그러려면 **다른 프로세스가
   만들어 줘야 한다** — 작업 스케줄러 서비스가 그 역할을 한다.
2. **콘솔을 갖지 않아야 한다.** 콘솔이 있으면 창을 닫을 수도, Ctrl+C 가 도달할 수도
   있다. `.cmd`·`cmd /c` 를 경유하지 않는다.
3. **uv 의 `.venv\Scripts\pythonw.exe` 는 콘솔을 없애 주지 않는다.** 45KB 트램폴린이
   콘솔형 인터프리터를 다시 띄운다. base `pythonw.exe`(GUI 서브시스템)를 직접 실행하고,
   venv 의 `site-packages` 는 `soak_entry.py` 가 `sys.path` 에 넣는다.

`argus` 가 venv 에 설치돼 있지 않아 import 가 cwd 에 의존한다. 스케줄러는 cwd 를
system32 로 주므로 XML 의 `<WorkingDirectory>` 를 반드시 둔다.

살아 있는지 보려면 창을 찾지 말고 프로세스를 본다 — **창은 없는 게 정상이다.**

## 살아 있는지 확인

```powershell
schtasks /query /tn "ArgusSoak" /fo LIST
Get-Content "$env:APPDATA\Argus\logs\argus.jsonl" -Encoding UTF8 -Tail 5
```

프로세스가 사라졌는데 `argus.jsonl` 에 종료 기록이 없으면 **강제 종료된 것**이다
(정상 종료·예외는 로그를 남긴다). 위 표의 세 가지를 먼저 의심할 것.

## 정상적으로 끄기

```powershell
.venv\Scripts\python.exe -m argus --stop
```

`%APPDATA%\Argus\STOP` 을 만들 뿐이다. 상주 인스턴스가 2초 주기로 그것을 보고, 파일을
지운 뒤 시그널을 받은 것과 같은 경로로 내려간다. 상주 인스턴스가 없으면 파일만 남고
**다음 기동이 그것을 치운다** — 그러지 않으면 새 인스턴스가 뜨자마자 다시 죽는다.

`schtasks /end` 는 강제 종료다. DB 는 WAL 이라 안전하지만 `shutdown` 이벤트가 남지 않아
다음 기동이 `unclean_shutdown` / `process_killed_or_crash` 로 판정한다. **정상 종료를
크래시로 기록하면 사후 진단의 근거가 오염된다** — 남의 PC 에서 "17건 중 몇 건이 진짜
크래시였나"를 볼 때 우리가 넣은 잡음이 섞인다.

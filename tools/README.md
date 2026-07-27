# tools/

개발·검증용 도구. 배포 exe 에는 포함되지 않는다.

| 파일 | 용도 |
|---|---|
| `fault_injector.py` | 결함을 인위로 주입해 정답 라벨(`fault_injections`)을 만든다 |
| `soak_entry.py` | 창 없는 진입점. 스케줄러가 base pythonw 로 실행한다 |
| `soak_task.xml` | 장시간 상주 검증용 작업 스케줄러 등록 정의 |

## 결함 주입

```powershell
python tools\fault_injector.py --list
python tools\fault_injector.py cpu_spin --duration 120 --ramp
python tools\fault_injector.py memory_leak --duration 300 --mem-load 0.3

# 부하를 만들지 않고 지금 하는 일에 라벨만 붙인다 (게임·빌드·인코딩)
python tools\fault_injector.py manual --label GAME --duration 1800
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

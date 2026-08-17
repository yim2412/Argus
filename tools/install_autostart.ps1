<#
.SYNOPSIS
    Argus 를 로그온 시 자동 시작하도록 작업 스케줄러에 등록한다.

.DESCRIPTION
    관리자 권한을 요구하지 않는다 (설계 규칙 4). 현재 사용자 계정의 작업으로 등록된다.

    파이썬 경로는 `.venv\pyvenv.cfg` 의 `home` 에서 읽어 낸다. XML 에 박아 두면 다른 PC·
    다른 파이썬 설치에서 조용히 깨지기 때문이다. venv 의 `pythonw.exe` 를 쓰지 않는
    이유(uv 트램폴린이 콘솔 창을 만든다)는 tools\README.md 에 정리돼 있다.

.NOTES
    **이 파일은 UTF-8 BOM 으로 저장해야 한다.** PowerShell 5.1 은 BOM 이 없으면 시스템
    ANSI 로 읽는다. 그러면 한글 문자열의 바이트가 깨지면서 파서가 따옴표 경계를 잘못
    잡아, 아래쪽 `Write-Host` 줄들이 코드가 아니라 **문자열로 삼켜져 그대로 출력된다.**
    2026-07-28 에 실제로 그랬다. 등록은 성공했으므로 오류로 보이지도 않는다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
    powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Start
    powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # 등록 직후 바로 시작한다 (다음 로그온까지 기다리지 않음).
    [switch]$Start,
    # 등록을 해제한다. 실행 중이면 먼저 중지한다.
    [switch]$Uninstall,
    # 작업 이름. 소크 검증용 ArgusSoak 과 구분한다.
    [string]$TaskName = "Argus",
    # 판정용 스냅샷을 하루 한 번 뽑아 이 폴더에 쌓는다 (다른 기계에 배포할 때).
    # 지정하지 않으면 스냅샷 작업을 만들지 않는다 — 개발 PC 에는 필요 없다.
    [string]$SnapshotTo,
    # 스냅샷을 뽑을 시각 (HH:mm). 기본은 새벽 4시 — 게임·작업 중일 가능성이 낮다.
    [string]$SnapshotAt = "04:00",
    # 스냅샷 폴더에 남길 개수. 하루 한 개씩 쌓이므로 30이면 한 달치다.
    [int]$SnapshotKeep = 30
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

function Fail([string]$msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

# ── 해제 ────────────────────────────────────────────────────────────────────
if ($Uninstall) {
    # 네이티브 exe 에 2>&1 을 붙이지 않는다. PowerShell 5.1 은 stderr 한 줄을
    # ErrorRecord 로 감싸며 종료 코드 0 에도 $? 를 false 로 만든다. 판정은
    # $LASTEXITCODE 로만 한다.
    schtasks /query /tn $TaskName > $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  등록된 작업이 없습니다: $TaskName"
        exit 0
    }
    schtasks /end /tn $TaskName > $null   # 안 돌고 있으면 실패해도 무방
    schtasks /delete /tn $TaskName /f
    if ($LASTEXITCODE -ne 0) { Fail "작업 삭제 실패: $TaskName" }
    Write-Host "[OK] 자동 시작 해제됨: $TaskName" -ForegroundColor Green

    # 스냅샷 작업도 같이 지운다. 남겨 두면 상주가 없는데 매일 돌아 빈 스냅샷을 쌓는다.
    # **쌓인 스냅샷 파일은 지우지 않는다** — 회수한 데이터가 그 안에 있고,
    # 해제는 "그만 수집한다"이지 "지금까지 모은 것을 버린다"가 아니다.
    $SnapTask = "${TaskName}Snapshot"
    schtasks /query /tn $SnapTask > $null
    if ($LASTEXITCODE -eq 0) {
        schtasks /delete /tn $SnapTask /f > $null
        Write-Host "[OK] 스냅샷 작업도 해제됨: $SnapTask" -ForegroundColor Green
        Write-Host "  이미 뽑아 둔 스냅샷 파일은 그대로 있습니다."
    }
    Write-Host "  이미 실행 중인 Argus 프로세스는 그대로 남아 있습니다."
    exit 0
}

# ── exe 배포판인가 ──────────────────────────────────────────────────────────
# **파이썬이 없는 PC 에 폴더만 복사한 경우다** (2026-08-17, 두 번째 기계를 붙이면서).
# 그전까지 이 스크립트는 `.venv\pyvenv.cfg` 를 반드시 요구해서, exe 만 받은 PC 에서는
# "pyvenv.cfg 에서 home 항목을 읽지 못했습니다" 로 죽었다.
#
# exe 를 먼저 본다 — 개발 PC 에는 `.venv` 와 `dist\` 가 **둘 다** 있는데, 거기서
# 이 스크립트를 exe 대상으로 부르는 경우는 없기 때문이다(그때는 소스로 돌린다).
# 그래서 판정 기준은 "옆에 argus.exe 가 있는가", 즉 **배포된 폴더 안에서 돌고 있는가**다.
$ExePath = $null
foreach ($candidate in @(
    (Join-Path $ProjectRoot "argus.exe"),          # 배포 폴더에 그대로 복사한 경우
    (Join-Path $ScriptDir  "argus.exe")            # tools\ 안에 함께 둔 경우
)) {
    if (Test-Path $candidate) { $ExePath = $candidate; break }
}

$Entry = $null
$ArgsLine = ""

if ($ExePath) {
    $PythonW = $ExePath
    # exe 는 자기가 진입점이다. Arguments 줄 자체를 넣지 않는다.
    $ArgsLine = ""
    Write-Host "  실행 형태 : exe 배포판"
} else {

# ── 파이썬 경로 확인 ────────────────────────────────────────────────────────
# venv 의 pythonw.exe 는 트램폴린이라 콘솔이 생긴다. base 인터프리터를 직접 쓴다.
$VenvCfg = Join-Path $ProjectRoot ".venv\pyvenv.cfg"
$PythonW = $null

if (Test-Path $VenvCfg) {
    # -Encoding UTF8 이 필수다. pyvenv.cfg 는 UTF-8 인데 PowerShell 5.1 의 기본값은
    # 시스템 ANSI 코드페이지라, 사용자 이름에 한글이 들어간 경로가 깨져 Test-Path 가
    # 실패한다. 그러면 아래 폴백으로 흘러 **다른 파이썬**이 선택된다.
    foreach ($line in (Get-Content $VenvCfg -Encoding UTF8)) {
        if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
            $home_dir = $Matches[1]
            $candidate = Join-Path $home_dir "pythonw.exe"
            if (Test-Path $candidate) {
                $PythonW = $candidate
            } else {
                Fail "pyvenv.cfg 의 home 에 pythonw.exe 가 없습니다: $candidate"
            }
            break
        }
    }
    if (-not $PythonW) { Fail "pyvenv.cfg 에서 home 항목을 읽지 못했습니다: $VenvCfg" }
}

if (-not $PythonW) {
    # venv 가 아예 없는 경우(시스템 파이썬에 직접 설치). venv 가 있는데 여기로 오면
    # 안 된다 — 버전이 다른 인터프리터가 venv 의 site-packages 를 읽으면 확장 모듈이
    # ABI 불일치로 죽는다. 그래서 위에서는 폴백하지 않고 곧장 실패시킨다.
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*\WindowsApps\*") {
        # WindowsApps 아래의 것은 Microsoft Store 설치를 유도하는 스텁이라 제외한다.
        $PythonW = $cmd.Source
    }
}

if (-not $PythonW) {
    Fail "pythonw.exe 를 찾지 못했습니다. .venv 를 만들었는지, 또는 argus.exe 가 옆에 있는지 확인하세요."
}

$Entry = Join-Path $ProjectRoot "tools\soak_entry.py"
if (-not (Test-Path $Entry)) { Fail "진입점이 없습니다: $Entry" }
$ArgsLine = "<Arguments>tools\soak_entry.py</Arguments>"

}  # ← exe 분기 끝

# ── 기동 점검 ───────────────────────────────────────────────────────────────
# 등록해 놓고 기동에 실패하면 다음 로그온까지 모른다. 여기서 미리 확인한다.
# 스케줄러가 쓸 바로 그 실행 파일이어야 의미가 있다(pywin32 경로 문제가 여기서 난다).
#
# **점검 결과는 파일로 받는다.** exe 배포판은 창 없는 빌드(`console=False`)라
# 화면에 아무것도 남기지 않는다 — `--out` 이 결과를 볼 유일한 통로다.
$CheckOut = Join-Path $env:TEMP "argus_check.txt"
$checkCode = $null

Push-Location $ProjectRoot
try {
    if ($ExePath) {
        # GUI 서브시스템이라 PowerShell 이 종료를 안 기다린다. Start-Process -Wait 로 받는다.
        $proc = Start-Process -FilePath $ExePath -ArgumentList "--check", "--out", "`"$CheckOut`"" `
            -Wait -PassThru -WindowStyle Hidden
        $checkCode = $proc.ExitCode
    } else {
        # 점검은 **콘솔형 python.exe** 로 돌린다. pythonw.exe 는 GUI 서브시스템이라
        # PowerShell 이 종료를 기다리지 않아 $LASTEXITCODE 가 비고, 점검이 항상 통과한
        # 것처럼 보인다. 같은 폴더의 python.exe 는 서브시스템만 다른 동일 인터프리터다.
        $PythonExe = Join-Path (Split-Path -Parent $PythonW) "python.exe"
        if (Test-Path $PythonExe) {
            & $PythonExe $Entry --check --out $CheckOut > $null
            $checkCode = $LASTEXITCODE
        } else {
            $proc = Start-Process -FilePath $PythonW -ArgumentList $Entry, "--check", "--out", "`"$CheckOut`"" `
                -Wait -PassThru -WindowStyle Hidden
            $checkCode = $proc.ExitCode
        }
    }
} finally {
    Pop-Location
}

# 점검 결과를 그대로 보여 준다. **남의 PC 에서 무엇이 켜지고 꺼졌는지가 여기 있다** —
# GPU 가 없다거나 PDH 를 못 쓴다는 것이 이 출력에서만 드러난다(설계 규칙 4).
if (Test-Path $CheckOut) {
    Write-Host ""
    Get-Content $CheckOut -Encoding UTF8 | ForEach-Object { Write-Host "  | $_" }
    Write-Host ""
    Remove-Item $CheckOut -Force -ErrorAction SilentlyContinue
}

if ($checkCode -ne 0) {
    Fail "기동 점검 실패(종료 코드 $checkCode). 위 출력을 확인하세요."
}
Write-Host "  기동 점검 : OK"

# ── XML 준비 ────────────────────────────────────────────────────────────────
$Template = Join-Path $ScriptDir "argus_task.xml"
if (-not (Test-Path $Template)) { Fail "작업 정의가 없습니다: $Template" }

$UserId = "$env:USERDOMAIN\$env:USERNAME"

$xml = Get-Content $Template -Raw -Encoding UTF8
$xml = $xml.Replace("{{PYTHONW}}", $PythonW)
$xml = $xml.Replace("{{WORKDIR}}", $ProjectRoot)
$xml = $xml.Replace("{{USERID}}", $UserId)
$xml = $xml.Replace("{{ARGSLINE}}", $ArgsLine)

if ($xml -match "\{\{") { Fail "치환되지 않은 자리표시자가 남았습니다." }

# schtasks /xml 은 UTF-16 만 받는다. UTF-8 을 주면 오류 메시지 없이 거부한다.
$Tmp = Join-Path $env:TEMP "argus_task_install.xml"
[System.IO.File]::WriteAllText($Tmp, $xml, [System.Text.Encoding]::Unicode)

# ── 등록 ────────────────────────────────────────────────────────────────────
schtasks /create /tn $TaskName /xml $Tmp /f
if ($LASTEXITCODE -ne 0) { Fail "작업 등록 실패. 위 메시지를 확인하세요." }
Remove-Item $Tmp -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[OK] 자동 시작 등록됨" -ForegroundColor Green
Write-Host "  작업 이름 : $TaskName"
Write-Host "  실행 계정 : $UserId"
if ($ExePath) {
    Write-Host "  실행 파일 : $PythonW  (exe 배포판)"
} else {
    Write-Host "  파이썬    : $PythonW"
}
Write-Host "  작업 폴더 : $ProjectRoot"
Write-Host "  트리거    : 로그온 1분 뒤"

if ($Start) {
    schtasks /run /tn $TaskName
    if ($LASTEXITCODE -ne 0) { Fail "시작 실패: $TaskName" }
    Write-Host "  지금 시작했습니다. 창은 뜨지 않는 것이 정상입니다." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  지금 바로 띄우려면: schtasks /run /tn `"$TaskName`""
}

# ── 스냅샷 작업 (다른 기계에 배포할 때만) ───────────────────────────────────
if ($SnapshotTo) {
    $SnapTask = "${TaskName}Snapshot"
    $SnapTemplate = Join-Path $ScriptDir "argus_snapshot_task.xml"
    if (-not (Test-Path $SnapTemplate)) { Fail "스냅샷 작업 정의가 없습니다: $SnapTemplate" }

    if (-not (Test-Path $SnapshotTo)) {
        New-Item -ItemType Directory -Path $SnapshotTo -Force | Out-Null
    }
    # 상대경로로 두면 스케줄러의 cwd 기준으로 엉뚱한 곳에 쌓인다.
    $SnapshotTo = (Resolve-Path $SnapshotTo).Path

    # 시각은 오늘 날짜에 붙여 만든다. 이미 지난 시각이어도 상관없다 —
    # 일간 트리거라 다음 날 그 시각에 처음 돈다.
    try {
        $when = [datetime]::ParseExact($SnapshotAt, "HH:mm", $null)
    } catch {
        Fail "-SnapshotAt 은 HH:mm 형식이어야 합니다 (받은 값: $SnapshotAt)"
    }
    $startAt = (Get-Date -Hour $when.Hour -Minute $when.Minute -Second 0).ToString("yyyy-MM-ddTHH:mm:ss")

    # exe 는 자기가 진입점, 소스는 진입점 스크립트를 앞에 붙인다.
    $snapArgs = if ($ExePath) { "" } else { "tools\soak_entry.py " }
    $snapArgs += "--export-findings `"$SnapshotTo`" --snapshot-keep $SnapshotKeep"

    $sx = Get-Content $SnapTemplate -Raw -Encoding UTF8
    $sx = $sx.Replace("{{PYTHONW}}", $PythonW)
    $sx = $sx.Replace("{{WORKDIR}}", $ProjectRoot)
    $sx = $sx.Replace("{{USERID}}", $UserId)
    $sx = $sx.Replace("{{STARTAT}}", $startAt)
    # XML 이므로 인자의 따옴표를 이스케이프한다. 경로에 공백이 있으면 따옴표가 필요하고,
    # 그것을 그대로 넣으면 XML 이 깨진다.
    $sx = $sx.Replace("{{SNAPARGS}}", [System.Security.SecurityElement]::Escape($snapArgs))

    if ($sx -match "\{\{") { Fail "스냅샷 작업에 치환되지 않은 자리표시자가 남았습니다." }

    $SnapTmp = Join-Path $env:TEMP "argus_snapshot_task_install.xml"
    [System.IO.File]::WriteAllText($SnapTmp, $sx, [System.Text.Encoding]::Unicode)
    schtasks /create /tn $SnapTask /xml $SnapTmp /f
    if ($LASTEXITCODE -ne 0) { Fail "스냅샷 작업 등록 실패." }
    Remove-Item $SnapTmp -Force -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "[OK] 스냅샷 작업 등록됨" -ForegroundColor Green
    Write-Host "  작업 이름 : $SnapTask"
    Write-Host "  저장 위치 : $SnapshotTo"
    Write-Host "  시각      : 매일 $SnapshotAt (최근 $SnapshotKeep 개 보관)"

    # **등록만 하고 끝내지 않는다.** 하루를 기다린 뒤에야 실패를 아는 것은
    # 이 프로젝트가 여러 번 당한 유형이다 — 지금 한 번 돌려 실제로 파일이 생기는지 본다.
    Write-Host "  지금 한 번 돌려 확인합니다 …"
    schtasks /run /tn $SnapTask > $null
    $ok = $false
    foreach ($i in 1..20) {
        Start-Sleep -Milliseconds 500
        if (Get-ChildItem $SnapshotTo -Filter "*.db" -ErrorAction SilentlyContinue) { $ok = $true; break }
    }
    if ($ok) {
        $f = Get-ChildItem $SnapshotTo -Filter "*.db" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Write-Host ("  [OK] {0}  ({1:N0} bytes)" -f $f.Name, $f.Length) -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] 10초 안에 스냅샷이 생기지 않았습니다. 다음을 직접 돌려 보세요:" -ForegroundColor Red
        Write-Host "         schtasks /run /tn `"$SnapTask`""
        Write-Host "         (창 없는 빌드라 화면에 아무것도 안 나옵니다 — logs\ 를 보세요)"
    }
}

Write-Host ""
Write-Host "  상태 확인 : schtasks /query /tn `"$TaskName`" /fo LIST"
Write-Host "  중지      : schtasks /end /tn `"$TaskName`""
Write-Host "  해제      : powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall"

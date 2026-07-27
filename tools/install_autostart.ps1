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
    [string]$TaskName = "Argus"
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
    Write-Host "  이미 실행 중인 Argus 프로세스는 그대로 남아 있습니다."
    exit 0
}

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
    Fail "pythonw.exe 를 찾지 못했습니다. .venv 를 만들었는지 확인하세요."
}

$Entry = Join-Path $ProjectRoot "tools\soak_entry.py"
if (-not (Test-Path $Entry)) { Fail "진입점이 없습니다: $Entry" }

# 등록해 놓고 기동에 실패하면 다음 로그온까지 모른다. 여기서 미리 확인한다.
# 스케줄러가 쓸 바로 그 인터프리터여야 의미가 있다(pywin32 경로 문제가 여기서 난다).
#
# 다만 점검은 **콘솔형 python.exe** 로 돌린다. pythonw.exe 는 GUI 서브시스템이라
# PowerShell 이 종료를 기다리지 않아 $LASTEXITCODE 가 비고, 점검이 항상 통과한 것처럼
# 보인다. 같은 폴더의 python.exe 는 서브시스템만 다른 동일 인터프리터다.
$PythonExe = Join-Path (Split-Path -Parent $PythonW) "python.exe"
$checkCode = $null

Push-Location $ProjectRoot
try {
    if (Test-Path $PythonExe) {
        & $PythonExe $Entry --check > $null
        $checkCode = $LASTEXITCODE
    } else {
        $proc = Start-Process -FilePath $PythonW -ArgumentList $Entry, "--check" `
            -Wait -PassThru -WindowStyle Hidden
        $checkCode = $proc.ExitCode
    }
} finally {
    Pop-Location
}

if ($checkCode -ne 0) {
    Fail "기동 점검 실패(종료 코드 $checkCode). 먼저 이것부터 확인하세요:`n    & '$PythonExe' '$Entry' --check"
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
Write-Host "  파이썬    : $PythonW"
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

Write-Host ""
Write-Host "  상태 확인 : schtasks /query /tn `"$TaskName`" /fo LIST"
Write-Host "  중지      : schtasks /end /tn `"$TaskName`""
Write-Host "  해제      : powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall"

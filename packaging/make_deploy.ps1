<#
.SYNOPSIS
    관측 기계에 그대로 복사할 배포 폴더를 만든다.

.DESCRIPTION
    `dist\argus` 에는 exe 와 `_internal` 뿐이다 — **설치 스크립트도 작업 정의 XML 도
    설정 파일도 들어 있지 않다.** 2026-08-17 에 배포 절차를 적다가 발견했다.
    그대로 복사해 갔으면 현장에서 "그런 파일이 없습니다" 를 만났을 것이다.

    이 스크립트가 필요한 것을 모아 **폴더 하나**로 만든다. 그 폴더를 관측 기계에
    통째로 옮기고 안에서 `설치.ps1` 을 돌리면 끝난다.

.NOTES
    이 파일은 UTF-8 BOM 으로 저장해야 한다 (PowerShell 5.1).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\make_deploy.ps1
#>
[CmdletBinding()]
param(
    # 만들어질 위치. 기본은 바탕화면의 ArgusDeploy.
    [string]$To = (Join-Path ([Environment]::GetFolderPath("Desktop")) "ArgusDeploy"),
    # 스냅샷을 쌓을 폴더 (관측 기계 기준). 설치 스크립트에 그대로 박힌다.
    [string]$SnapshotDir = "C:\ArgusSnapshots"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Fail([string]$m) { Write-Host "[FAIL] $m" -ForegroundColor Red; exit 1 }

$Dist = Join-Path $Root "dist\argus"
if (-not (Test-Path (Join-Path $Dist "argus.exe"))) {
    Fail "먼저 exe 를 빌드하세요:`n    .venv\Scripts\pyinstaller.exe packaging\argus.spec --noconfirm"
}

# **빌드가 소스보다 오래됐으면 멈춘다.** 고친 코드를 안 담은 폴더를 들고 가는 것이
# 가장 흔하고 가장 알아채기 어려운 실수다 — 현장에서는 정상으로 보이고, 며칠 뒤
# 데이터가 이상할 때에야 드러난다.
$exeTime = (Get-Item (Join-Path $Dist "argus.exe")).LastWriteTime
$newer = Get-ChildItem (Join-Path $Root "argus") -Recurse -Filter "*.py" |
         Where-Object { $_.LastWriteTime -gt $exeTime }
if ($newer) {
    Write-Host "[FAIL] exe 가 소스보다 오래됐습니다. 다시 빌드하세요." -ForegroundColor Red
    Write-Host "  exe  : $exeTime"
    $newer | Select-Object -First 5 | ForEach-Object {
        Write-Host ("  나중 : {0}  {1}" -f $_.LastWriteTime, $_.FullName.Substring($Root.Length + 1))
    }
    exit 1
}

if (Test-Path $To) { Remove-Item $To -Recurse -Force }
New-Item -ItemType Directory -Path $To -Force | Out-Null

Write-Host "  exe 복사 중 … (198MB, 잠시 걸립니다)"
Copy-Item (Join-Path $Dist "*") $To -Recurse -Force

# 설치에 필요한 것만 추린다. tools\ 전체를 넣지 않는다 — 개발용 도구는 파이썬이
# 필요해서 저쪽에서 돌지 않고, 있으면 돌려 보다가 헷갈린다.
$toolsDst = Join-Path $To "tools"
New-Item -ItemType Directory -Path $toolsDst -Force | Out-Null
foreach ($f in @("install_autostart.ps1", "argus_task.xml", "argus_snapshot_task.xml")) {
    Copy-Item (Join-Path $Root "tools\$f") $toolsDst -Force
}
Copy-Item (Join-Path $Root "packaging\settings.quiet-observer.yaml") $To -Force

# ── 현장에서 돌릴 한 방 스크립트 ────────────────────────────────────────────
$installer = @"
<#
    관측 기계에서 이것 하나만 실행하면 된다.
    powershell -ExecutionPolicy Bypass -File 설치.ps1
#>
`$ErrorActionPreference = "Stop"
`$Here = Split-Path -Parent `$MyInvocation.MyCommand.Path
Set-Location `$Here

Write-Host ""
Write-Host "==== Argus 설치 ====" -ForegroundColor Cyan
Write-Host ""

# 1. 조용한 관측 모드 설정 — 알림을 띄우지 않는다 (게임·매크로 방해 금지)
`$cfgDir = Join-Path `$env:APPDATA "Argus"
New-Item -ItemType Directory -Path `$cfgDir -Force | Out-Null
Copy-Item "settings.quiet-observer.yaml" (Join-Path `$cfgDir "settings.yaml") -Force
Write-Host "[1/3] 조용한 관측 모드 설정 완료 (알림 꺼짐)" -ForegroundColor Green

# 2. 자동 시작 + 매일 스냅샷 등록. 기동 점검 결과가 아래에 그대로 나온다.
Write-Host ""
Write-Host "[2/3] 자동 시작 등록 중 …"
Write-Host ""
& powershell -ExecutionPolicy Bypass -File "tools\install_autostart.ps1" -Start -SnapshotTo "$SnapshotDir"
if (`$LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] 등록에 실패했습니다. 위 메시지를 개발 PC 로 보내 주세요." -ForegroundColor Red
    Read-Host "엔터를 누르면 닫힙니다"
    exit 1
}

# 3. 이 PC 의 IP — 개발 PC 가 스냅샷을 가져올 주소다.
Write-Host ""
Write-Host "[3/3] 이 PC 의 주소" -ForegroundColor Green
`$ips = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { `$_.IPAddress -notlike "127.*" -and `$_.IPAddress -notlike "169.254.*" }
foreach (`$ip in `$ips) {
    Write-Host ("    {0}   ({1})" -f `$ip.IPAddress, `$ip.InterfaceAlias)
}

# 공유 폴더 — 관리자 권한이 있으면 여기서 만들고, 없으면 안내만 한다.
#
# **줄 끝 백틱(줄 잇기)을 쓰지 않는다.** 이 스크립트는 here-string 안에서 만들어지는데
# 거기서 백틱은 이스케이프 문자라 생성 시점에 먹힌다 — 두 줄이 잘못 붙어 구문 오류가
# 된다. 2026-08-17 에 실제로 그랬고, 생성된 파일을 파싱해 보고서야 잡혔다.
`$id = [Security.Principal.WindowsIdentity]::GetCurrent()
`$admin = (New-Object Security.Principal.WindowsPrincipal(`$id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host ""
if (`$admin) {
    if (Get-SmbShare -Name "ArgusSnap" -ErrorAction SilentlyContinue) {
        Write-Host "공유 폴더 ArgusSnap 은 이미 있습니다." -ForegroundColor Green
    } else {
        New-SmbShare -Name "ArgusSnap" -Path "$SnapshotDir" -ReadAccess "Everyone" | Out-Null
        Write-Host "공유 폴더 ArgusSnap 을 만들었습니다." -ForegroundColor Green
    }
} else {
    Write-Host "!! 공유 폴더는 못 만들었습니다 (관리자 권한 필요)" -ForegroundColor Yellow
    Write-Host "   관리자 PowerShell 을 열고 이 한 줄을 실행하세요:"
    Write-Host ""
    Write-Host '   New-SmbShare -Name "ArgusSnap" -Path "$SnapshotDir" -ReadAccess "Everyone"' -ForegroundColor White
}

# 네트워크 프로필 — 공용이면 공유가 막힌다. 제일 흔한 실패 원인이다.
`$pub = Get-NetConnectionProfile | Where-Object { `$_.NetworkCategory -eq "Public" }
if (`$pub) {
    Write-Host ""
    Write-Host "!! 네트워크가 '공용' 으로 잡혀 있어 공유가 막힐 수 있습니다:" -ForegroundColor Yellow
    foreach (`$p in `$pub) { Write-Host ("   - {0} ({1})" -f `$p.Name, `$p.InterfaceAlias) }
    Write-Host "   설정 > 네트워크 > 속성에서 '개인' 으로 바꾸세요."
}

Write-Host ""
Write-Host "==== 끝났습니다 ====" -ForegroundColor Cyan
Write-Host "위에 나온 IP 주소를 개발 PC 에 알려주세요."
Write-Host "매크로를 다시 켜셔도 됩니다 — 이제 창은 뜨지 않습니다."
Write-Host ""
Read-Host "엔터를 누르면 닫힙니다"
"@

$installerPath = Join-Path $To "설치.ps1"
[System.IO.File]::WriteAllText($installerPath, $installer, (New-Object System.Text.UTF8Encoding($true)))

# 해제용도 같이 넣는다. 되돌리는 법이 없는 배포는 남의 PC 에 두고 오는 짐이 된다.
$uninstaller = @"
`$Here = Split-Path -Parent `$MyInvocation.MyCommand.Path
Set-Location `$Here
& powershell -ExecutionPolicy Bypass -File "tools\install_autostart.ps1" -Uninstall
Write-Host ""
Write-Host "수집한 데이터는 %APPDATA%\Argus 에 그대로 있습니다."
Write-Host "완전히 지우려면 그 폴더와 이 폴더를 삭제하세요."
Read-Host "엔터를 누르면 닫힙니다"
"@
[System.IO.File]::WriteAllText(
    (Join-Path $To "제거.ps1"), $uninstaller, (New-Object System.Text.UTF8Encoding($true)))

# ── 만든 스크립트를 실제로 파싱해 본다 ─────────────────────────────────────
#
# **생성한 코드는 만든 사람이 읽지 않는다.** here-string 안에서 조립되므로 이스케이프
# 하나가 어긋나도 만드는 쪽은 성공으로 끝나고, 현장에서 처음 터진다. 2026-08-17 에
# 줄 끝 백틱이 먹혀 `설치.ps1` 이 구문 오류인 채로 만들어졌다 — 이 검사가 잡았다.
#
# 문법만 본다. 실행까지 해 볼 수는 없다(작업을 등록하고 설정을 덮어쓴다).
foreach ($script in @("설치.ps1", "제거.ps1")) {
    $path = Join-Path $To $script
    $perr = $null
    [System.Management.Automation.PSParser]::Tokenize(
        (Get-Content $path -Raw), [ref]$perr) | Out-Null
    if ($perr.Count -gt 0) {
        Write-Host "[FAIL] 만든 $script 에 구문 오류가 있습니다:" -ForegroundColor Red
        $perr | ForEach-Object { Write-Host "  - $($_.Message)" }
        exit 1
    }
    # BOM 확인. 없으면 PowerShell 5.1 이 시스템 ANSI 로 읽어 한글이 깨지고,
    # 파서가 따옴표 경계를 잘못 잡아 코드가 문자열로 삼켜진다.
    $bom = [System.IO.File]::ReadAllBytes($path)[0..2]
    if (-not ($bom[0] -eq 239 -and $bom[1] -eq 187 -and $bom[2] -eq 191)) {
        Write-Host "[FAIL] $script 에 UTF-8 BOM 이 없습니다." -ForegroundColor Red
        exit 1
    }
}
Write-Host "  스크립트 검사 : OK (구문 · BOM)"

$size = (Get-ChildItem $To -Recurse | Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host "[OK] 배포 폴더를 만들었습니다" -ForegroundColor Green
Write-Host "  위치 : $To"
Write-Host ("  크기 : {0:N0} MB" -f ($size / 1MB))
Write-Host "  스냅샷 폴더 : $SnapshotDir  (관측 기계 기준)"
Write-Host ""
Write-Host "  이 폴더를 관측 기계로 통째로 옮긴 뒤, 그 안에서:"
Write-Host "    powershell -ExecutionPolicy Bypass -File 설치.ps1" -ForegroundColor White

<#
.SYNOPSIS
    다른 기계가 뽑아 둔 판정용 스냅샷을 이 PC 로 가져온다.

.DESCRIPTION
    2026-08-17 에 두 번째 기계(내장그래픽 노트북)를 붙이면서 만들었다. 그 기계는
    24시간 돌며 하루 한 번 스냅샷을 뽑고, 이 PC 는 낮에 켤 때 그것을 당겨온다.

    **가져오는 쪽에 두는 이유**는 두 기계의 가동 시간이 다르기 때문이다. 노트북은
    24시간 켜져 있고 이 PC 는 가끔 켜진다 — 공유 폴더를 노트북에 두고 이쪽이
    당겨오면, 이 PC 가 며칠 꺼져 있어도 그동안의 스냅샷이 저쪽에 그대로 쌓인다.

    **네트워크 경로에서 SQLite 를 직접 열지 않는다.** SMB 는 파일 잠금이 불안정해
    읽는 중에 깨질 수 있다. 반드시 로컬로 복사한 뒤 연다 — 이 스크립트가 하는 일이다.

.NOTES
    이 파일은 UTF-8 BOM 으로 저장해야 한다. PowerShell 5.1 은 BOM 이 없으면 시스템
    ANSI 로 읽어 한글 문자열의 따옴표 경계를 잘못 잡는다.

.EXAMPLE
    # 첫 실행 — 원본 위치를 알려주면 다음부터는 기억한다
    powershell -ExecutionPolicy Bypass -File tools\fetch_snapshots.ps1 -From \\192.168.219.50\ArgusSnap

    # 이후 — 인자 없이
    powershell -ExecutionPolicy Bypass -File tools\fetch_snapshots.ps1
#>
[CmdletBinding()]
param(
    # 원본 공유 폴더 (예: \\192.168.219.50\ArgusSnap). 한 번 주면 기억한다.
    [string]$From,
    # 받아 둘 로컬 폴더. 기본은 %APPDATA%\Argus\remote 다.
    [string]$To,
    # 받기만 하고 요약을 찍지 않는다 (스크립트에서 부를 때).
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $env:APPDATA "Argus\remote_source.txt"

if (-not $To) { $To = Join-Path $env:APPDATA "Argus\remote" }

# ── 원본 위치 결정 ──────────────────────────────────────────────────────────
# 매번 IP 를 치게 하지 않는다. 다만 **기억한 값이 무엇인지 항상 보여 준다** —
# 조용히 옛 주소를 쓰다가 "왜 새 파일이 없지" 를 묻게 되는 것이 최악이다.
if ($From) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigPath) -Force | Out-Null
    Set-Content -Path $ConfigPath -Value $From -Encoding UTF8
} elseif (Test-Path $ConfigPath) {
    $From = (Get-Content $ConfigPath -Encoding UTF8 -TotalCount 1).Trim()
} else {
    Write-Host "[FAIL] 원본 위치를 모릅니다. 처음 한 번은 -From 을 주세요:" -ForegroundColor Red
    Write-Host "       .\tools\fetch_snapshots.ps1 -From \\<노트북IP>\ArgusSnap"
    exit 1
}

Write-Host "  원본 : $From"
Write-Host "  대상 : $To"

if (-not (Test-Path $From)) {
    Write-Host ""
    Write-Host "[FAIL] 원본 폴더에 닿지 못했습니다." -ForegroundColor Red
    Write-Host "  확인할 것:"
    Write-Host "   1. 저쪽 PC 가 켜져 있는가"
    Write-Host "   2. 저쪽 네트워크 프로필이 '개인'인가 (공용이면 공유가 막힌다)"
    Write-Host "   3. IP 가 바뀌지 않았는가 (공유기에서 고정 할당을 권함)"
    Write-Host "  기억된 주소를 바꾸려면 -From 을 다시 주세요."
    exit 1
}

New-Item -ItemType Directory -Path $To -Force | Out-Null

# ── 복사 ────────────────────────────────────────────────────────────────────
# /XO = 원본이 더 새 것일 때만. 이미 받은 것은 건너뛰므로 켤 때마다 돌려도 된다.
# /NJH /NJS = 머리글·요약 줄 억제.
robocopy $From $To "*.db" /XO /NJH /NJS /NDL /NP | Out-Null

# **robocopy 의 종료 코드는 0 이 아니어도 성공이다.** 0~7 이 정상(8 이상이 실패)이고,
# 1 은 "파일을 복사했다"는 뜻이다. 이것을 모르고 `-ne 0` 으로 판정하면 정상 복사가
# 매번 실패로 보인다.
$rc = $LASTEXITCODE
if ($rc -ge 8) {
    Write-Host "[FAIL] 복사 실패 (robocopy 코드 $rc)" -ForegroundColor Red
    exit 1
}

if ($Quiet) { exit 0 }

# ── 요약 ────────────────────────────────────────────────────────────────────
$files = Get-ChildItem $To -Filter "*.db" -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending

if (-not $files) {
    Write-Host ""
    Write-Host "[FAIL] 받은 스냅샷이 없습니다. 저쪽에서 아직 한 번도 안 뽑았을 수 있습니다." -ForegroundColor Yellow
    Write-Host "  저쪽에서 확인: schtasks /query /tn `"ArgusSnapshot`" /fo LIST"
    exit 1
}

$newest = $files[0]
$age = (Get-Date) - $newest.LastWriteTime

Write-Host ""
Write-Host "[OK] 스냅샷 $($files.Count)개" -ForegroundColor Green
Write-Host ("  최신 : {0}  ({1:N1} MB)" -f $newest.Name, ($newest.Length / 1MB))
Write-Host ("  시각 : {0:yyyy-MM-dd HH:mm}  ({1:N1}일 전)" -f $newest.LastWriteTime, $age.TotalDays)

# **파일의 나이가 곧 저쪽의 생존 신호다.** 별도 하트비트를 만들지 않은 이유가 이것이다.
# 하루 한 번 뽑으므로 이틀이 넘으면 저쪽에서 무언가 멈춘 것이다.
if ($age.TotalDays -gt 2) {
    Write-Host ""
    Write-Host "  [!] 최신 스냅샷이 2일보다 오래됐습니다. 저쪽 상주나 스냅샷 작업이 멈췄을 수 있습니다." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  분석하려면 이 경로를 데이터 폴더로 지정해 읽습니다:"
Write-Host "    `$env:ARGUS_DATA_DIR = `"$To`""

[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$PersistUserPath
)

$ErrorActionPreference = "Stop"

function Fail-KordocSetup([string]$Message) {
    Write-Error $Message
    exit 1
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Fail-KordocSetup "Node.js/npm을 찾지 못했습니다. Node.js LTS를 설치한 뒤 이 스크립트를 다시 실행하세요."
}

if (-not $SkipInstall) {
    Write-Host "Kordoc을 현재 사용자 환경에 전역 설치합니다..."
    & $npm.Source install -g kordoc
    if ($LASTEXITCODE -ne 0) {
        Fail-KordocSetup "npm install -g kordoc가 실패했습니다. 위 npm 오류를 확인하세요."
    }
}

$npmGlobal = (& $npm.Source prefix -g 2>$null | Select-Object -First 1).Trim()
if ([string]::IsNullOrWhiteSpace($npmGlobal)) {
    Fail-KordocSetup "npm 전역 prefix를 확인하지 못했습니다. npm prefix -g를 직접 실행해 확인하세요."
}

# Make the current PowerShell process see the npm shim immediately.
$env:Path = "$npmGlobal;$env:Path"
if ($PersistUserPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $npmGlobal.TrimEnd('\') })) {
        [Environment]::SetEnvironmentVariable("Path", (($entries + $npmGlobal) -join ';'), "User")
        Write-Host "사용자 PATH에 npm 전역 경로를 추가했습니다. 새 터미널부터 적용됩니다."
    }
}

$kordoc = Get-Command kordoc -ErrorAction SilentlyContinue
if (-not $kordoc) {
    $whereResult = @(where.exe kordoc 2>$null)
    if ($whereResult.Count -eq 0) {
        Fail-KordocSetup "Kordoc 설치 후에도 명령을 찾지 못했습니다. npmGlobal=$npmGlobal 및 PATH를 확인하세요."
    }
}

& kordoc --version
if ($LASTEXITCODE -ne 0) {
    Fail-KordocSetup "Kordoc 명령은 찾았지만 실행에 실패했습니다. npm shim과 Node.js 설치를 확인하세요."
}

Write-Host "Kordoc 준비 완료. 앱을 완전히 종료하고 다시 시작한 뒤 원본 문서를 재처리하세요."
Write-Host "필수 순서: 재처리 -> 사람 승인 -> 승인하고 색인 -> MCP 묶음 생성"

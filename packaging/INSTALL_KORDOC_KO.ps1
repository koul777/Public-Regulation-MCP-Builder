[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$PersistUserPath
)

$ErrorActionPreference = "Stop"
# Keep Korean status readable when Windows PowerShell output is redirected.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$KordocPackage = "kordoc@4.16.0"

function Fail-KordocSetup([string]$Message, [int]$ExitCode = 1) {
    [Console]::Error.WriteLine($Message)
    exit $ExitCode
}

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Fail-KordocSetup "Node.js/npm을 찾지 못했습니다. Node.js LTS를 설치한 뒤 이 스크립트를 다시 실행하세요."
}

if (-not $SkipInstall) {
    Write-Host "검증된 Kordoc 4.16.0 고정 버전을 현재 사용자 환경에 전역 설치·업데이트합니다..."
    & $npm.Source install -g $KordocPackage
    if ($LASTEXITCODE -ne 0) {
        Fail-KordocSetup "npm install -g $KordocPackage가 실패했습니다. 위 npm 오류를 확인하세요."
    }
}

$npmGlobal = [string](& $npm.Source prefix -g 2>$null | Select-Object -First 1)
$npmGlobal = $npmGlobal.Trim()
if ([string]::IsNullOrWhiteSpace($npmGlobal)) {
    Fail-KordocSetup "npm 전역 prefix를 확인하지 못했습니다. npm prefix -g를 직접 실행해 확인하세요."
}

$kordocShim = Join-Path $npmGlobal "kordoc.cmd"
if (-not (Test-Path -LiteralPath $kordocShim -PathType Leaf)) {
    Fail-KordocSetup "npm 전역 prefix에 Kordoc 실행 파일이 없습니다. 설치 상태를 다시 확인하세요." 10
}

# Make the current PowerShell process see the npm shim immediately.
$env:Path = "$npmGlobal;$env:Path"

$kordoc = Get-Command kordoc -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $kordoc -or
    -not [string]::Equals(
        [IO.Path]::GetFullPath([string]$kordoc.Source),
        [IO.Path]::GetFullPath($kordocShim),
        [StringComparison]::OrdinalIgnoreCase
    )) {
    Fail-KordocSetup "실행할 Kordoc 명령이 npm 전역 prefix의 설치 파일과 일치하지 않습니다. PATH를 확인하세요." 10
}

$versionLines = @(& $kordocShim --version 2>$null)
$versionExitCode = $LASTEXITCODE
if ($versionExitCode -ne 0) {
    Fail-KordocSetup "Kordoc 명령은 찾았지만 실행에 실패했습니다. npm shim과 Node.js 설치를 확인하세요."
}
$versionOutput = $versionLines | Select-Object -First 1
$actualVersion = ([string]$versionOutput).Trim()
if ($actualVersion -notmatch '^(?:kordoc\s+)?v?(\d+\.\d+\.\d+)$' -or $Matches[1] -ne '4.16.0') {
    Fail-KordocSetup "설치된 Kordoc 버전이 검증된 4.16.0과 다릅니다. 다시 설치한 뒤 확인하세요." 11
}
if ($PersistUserPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $npmGlobal.TrimEnd('\') })) {
        [Environment]::SetEnvironmentVariable("Path", (($entries + $npmGlobal) -join ';'), "User")
        Write-Host "사용자 PATH에 npm 전역 경로를 추가했습니다. 새 터미널부터 적용됩니다."
    }
}

Write-Host "Kordoc 준비 완료. 앱을 완전히 종료하고 다시 시작한 뒤 원본 문서를 재처리하세요."
Write-Host "필수 순서: 재처리 -> 사람 승인 -> 승인하고 색인 -> MCP 묶음 생성"

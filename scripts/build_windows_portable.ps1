[CmdletBinding()]
param(
    [string]$Version = "0.1.0-dev",
    [string]$BasePython = "python",
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistRoot = Join-Path $ProjectRoot "dist"
$BuildRoot = Join-Path $ProjectRoot "build"
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$BuildPip = Join-Path $BuildVenv "Scripts\pip.exe"
$PackageName = "PR-MCP-Builder-Windows-x64-$Version"
$StageRoot = Join-Path $BuildRoot $PackageName
$ZipPath = Join-Path $DistRoot "$PackageName.zip"

Push-Location $ProjectRoot
try {
    if (-not $SkipExeBuild) {
        if (-not (Test-Path -LiteralPath $BuildPython)) {
            & $BasePython -m venv $BuildVenv
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to create the dedicated build virtual environment."
            }
        }
        if (-not (Test-Path -LiteralPath $BuildPip)) {
            Write-Host "[INFO] Build environment pip is missing; bootstrapping with ensurepip."
            & $BuildPython -m ensurepip --upgrade
            if ($LASTEXITCODE -ne 0) {
                throw "Failed to bootstrap pip in the build virtual environment."
            }
        }
        & $BuildPython -m pip install --disable-pip-version-check --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upgrade pip in the build virtual environment."
        }
        & $BuildPython -m pip install --disable-pip-version-check -e . pyinstaller
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install build dependencies."
        }

        & $BuildPython -m PyInstaller --noconfirm --clean "packaging\PR-MCP-Builder.spec"
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller build failed with exit code $LASTEXITCODE"
        }
    }

    $BuiltApp = Join-Path $DistRoot "PR MCP Builder"
    $BuiltExe = Join-Path $BuiltApp "PR MCP Builder.exe"
    if (-not (Test-Path -LiteralPath $BuiltExe)) {
        throw "Built executable was not found: $BuiltExe"
    }

    if (Test-Path -LiteralPath $StageRoot) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $StageRoot | Out-Null
    Copy-Item -Path (Join-Path $BuiltApp "*") -Destination $StageRoot -Recurse -Force
    Copy-Item -LiteralPath "LICENSE" -Destination $StageRoot
    Copy-Item -LiteralPath "README.md" -Destination $StageRoot
    Copy-Item -LiteralPath "SECURITY.md" -Destination $StageRoot
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.md" -Destination $StageRoot

    $StageDocs = Join-Path $StageRoot "docs"
    New-Item -ItemType Directory -Path $StageDocs -Force | Out-Null
    Copy-Item -LiteralPath "docs\mcp_quickconnect_ko.md" -Destination $StageDocs
    Copy-Item -LiteralPath "docs\public_repository_history_policy_ko.md" -Destination $StageDocs

    Copy-Item -LiteralPath "packaging\README_RUN_KO.txt" -Destination $StageRoot

    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Compress-Archive -LiteralPath $StageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
    Write-Host "[OK] Windows portable ZIP: $ZipPath"
}
finally {
    Pop-Location
}

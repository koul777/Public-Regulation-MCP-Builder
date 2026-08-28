[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$BasePython = "python",
    [string]$DistRoot = "",
    [string]$BuildRoot = "",
    [switch]$SkipExeBuild,
    [switch]$SkipWheelBuild,
    [switch]$RecreateBuildVenv
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($DistRoot)) {
    $DistRoot = Join-Path $ProjectRoot "dist"
} elseif (-not [System.IO.Path]::IsPathRooted($DistRoot)) {
    $DistRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $DistRoot))
} else {
    $DistRoot = [System.IO.Path]::GetFullPath($DistRoot)
}
if ([string]::IsNullOrWhiteSpace($BuildRoot)) {
    $BuildRoot = Join-Path $ProjectRoot "build"
} elseif (-not [System.IO.Path]::IsPathRooted($BuildRoot)) {
    $BuildRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $BuildRoot))
} else {
    $BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
}
$PreferredBuildVenv = Join-Path $ProjectRoot ".build-venv"
$BuildVenv = $PreferredBuildVenv
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$BuildPip = Join-Path $BuildVenv "Scripts\pip.exe"
$IsolationHelper = Join-Path $ProjectRoot "scripts\check_build_environment_isolation.py"
if ($RecreateBuildVenv -and $SkipExeBuild) {
    throw "-RecreateBuildVenv cannot be combined with -SkipExeBuild."
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Content
    )
    $Parent = Split-Path -Parent $LiteralPath
    if (-not [string]::IsNullOrWhiteSpace($Parent)) {
        New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    }
    [System.IO.File]::WriteAllText(
        $LiteralPath,
        $Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Get-Sha256Lower {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    return (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-PathWithinRoot {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$RootPath
    )
    $ResolvedPath = [System.IO.Path]::GetFullPath($LiteralPath)
    $ResolvedRoot = [System.IO.Path]::GetFullPath($RootPath)
    if ([string]::Equals(
        $ResolvedPath,
        $ResolvedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $true
    }
    $RootWithSeparator = $ResolvedRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    return $ResolvedPath.StartsWith(
        $RootWithSeparator,
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-SafeBuildVenvPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$BuildRoot
    )
    $ResolvedPath = [System.IO.Path]::GetFullPath($LiteralPath)
    $ResolvedProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    $ResolvedBuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
    $ResolvedPreferred = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot ".build-venv"))
    if ([string]::Equals(
        $ResolvedPath,
        $ResolvedProjectRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -or [string]::Equals(
        $ResolvedPath,
        $ResolvedBuildRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The build virtual environment path must not resolve to a broad workspace root."
    }
    if (
        -not [string]::Equals(
            $ResolvedPath,
            $ResolvedPreferred,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        -not (Test-PathWithinRoot -LiteralPath $ResolvedPath -RootPath $BuildRoot)
    ) {
        throw "The build virtual environment path must stay within the project .build-venv or build root."
    }
}

function Get-PortableWheelSourceFingerprint {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)
    $TrackedExtensions = @(
        ".py"
        ".ps1"
        ".bat"
        ".spec"
        ".txt"
        ".md"
        ".toml"
        ".in"
    )
    $IncludedPaths = @(
        "app"
        "frontend"
        "scripts"
        "packaging"
        "pyproject.toml"
        "MANIFEST.in"
        "README.md"
        "LICENSE"
        "SECURITY.md"
        "THIRD_PARTY_NOTICES.md"
    )
    $Entries = New-Object System.Collections.Generic.List[string]
    foreach ($IncludedPath in $IncludedPaths) {
        $ResolvedPath = Join-Path $ProjectRoot $IncludedPath
        if (-not (Test-Path -LiteralPath $ResolvedPath)) {
            continue
        }
        $Item = Get-Item -LiteralPath $ResolvedPath -Force
        $Files = if ($Item.PSIsContainer) {
            Get-ChildItem -LiteralPath $ResolvedPath -Recurse -File |
                Where-Object {
                    $TrackedExtensions -contains $_.Extension.ToLowerInvariant() -and
                    $_.FullName -notmatch '[\\/]__pycache__[\\/]'
                } |
                Sort-Object FullName
        } else {
            @($Item)
        }
        foreach ($File in $Files) {
            $RootWithSeparator = $ProjectRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
            $RootUri = New-Object System.Uri($RootWithSeparator)
            $FileUri = New-Object System.Uri($File.FullName)
            $RelativePath = [System.Uri]::UnescapeDataString(
                $RootUri.MakeRelativeUri($FileUri).ToString()
            )
            $Entries.Add(
                (
                    "{0}:{1}" -f $RelativePath.Replace("\", "/"),
                    (Get-Sha256Lower -LiteralPath $File.FullName)
                )
            )
        }
    }
    $Payload = [string]::Join("`n", $Entries)
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Payload)
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
}

function Assert-PortableExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ProbeRoot
    )
    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        throw "Built executable was not found: $ExecutablePath"
    }
    $VersionInfo = (Get-Item -LiteralPath $ExecutablePath).VersionInfo
    $ProductVersion = ([string]$VersionInfo.ProductVersion).Trim()
    if ($ProductVersion -ne $ExpectedVersion) {
        throw "Executable ProductVersion mismatch. Expected $ExpectedVersion but found '$ProductVersion': $ExecutablePath"
    }

    New-Item -ItemType Directory -Path $ProbeRoot -Force | Out-Null
    $VersionProbeText = Invoke-PortableExecutableProbe `
        -ExecutablePath $ExecutablePath `
        -ProbeRoot $ProbeRoot `
        -Arguments @("--version") `
        -ProbeName "version"
    if (([string]$VersionProbeText).Trim() -ne $ExpectedVersion) {
        throw "Executable --version probe mismatch. Expected $ExpectedVersion but found '$(([string]$VersionProbeText).Trim())'."
    }
    $HelpProbeText = Invoke-PortableExecutableProbe `
        -ExecutablePath $ExecutablePath `
        -ProbeRoot $ProbeRoot `
        -Arguments @("--mcp-server", "--help") `
        -ProbeName "MCP help"
    if (
        $HelpProbeText -notmatch "Run a local approved-regulation MCP server\." -or
        $HelpProbeText -notmatch "--transport"
    ) {
        throw "Executable MCP help probe returned unexpected output."
    }
    $QwenHelpProbeText = Invoke-PortableExecutableProbe `
        -ExecutablePath $ExecutablePath `
        -ProbeRoot $ProbeRoot `
        -Arguments @("--qwen-chat", "--help") `
        -ProbeName "standalone Qwen chat help"
    if (
        $QwenHelpProbeText -notmatch "qwen3:8b" -or
        $QwenHelpProbeText -notmatch "--port" -or
        $QwenHelpProbeText -notmatch "--headless"
    ) {
        throw "Executable standalone Qwen chat help probe returned unexpected output."
    }

    $SelfCheckText = Invoke-PortableExecutableProbe `
        -ExecutablePath $ExecutablePath `
        -ProbeRoot $ProbeRoot `
        -Arguments @("--portable-self-check") `
        -ProbeName "PDF parser self-check"
    try {
        $SelfCheck = $SelfCheckText | ConvertFrom-Json
    }
    catch {
        throw "Executable PDF parser self-check returned invalid JSON."
    }
    if (
        $SelfCheck.schema_version -ne "pr-mcp-builder-portable-self-check-v1" -or
        $SelfCheck.status -ne "ok" -or
        [int]$SelfCheck.pages -ne 1 -or
        $SelfCheck.text_verified -ne $true
    ) {
        throw "Executable PDF parser self-check returned an unexpected result."
    }
}

function Invoke-PortableExecutableProbe {
    param(
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$ProbeRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$ProbeName
    )
    $ProbeId = [Guid]::NewGuid().ToString("N")
    $ProbeStdout = Join-Path $ProbeRoot "$ProbeId.stdout.txt"
    $ProbeStderr = Join-Path $ProbeRoot "$ProbeId.stderr.txt"
    $ProbeProcess = $null
    try {
        $ProbeProcess = Start-Process `
            -FilePath $ExecutablePath `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -PassThru `
            -RedirectStandardOutput $ProbeStdout `
            -RedirectStandardError $ProbeStderr
        # Windows PowerShell 5 does not reliably populate ExitCode unless the
        # process handle is materialized before waiting.
        $null = $ProbeProcess.Handle
        if (-not $ProbeProcess.WaitForExit(60000)) {
            Stop-Process -Id $ProbeProcess.Id -Force -ErrorAction SilentlyContinue
            $ProbeProcess.WaitForExit()
            throw "Executable $ProbeName probe timed out after 60 seconds."
        }
        $ProbeProcess.WaitForExit()
        $StdoutText = if (Test-Path -LiteralPath $ProbeStdout) {
            Get-Content -LiteralPath $ProbeStdout -Raw
        } else { "" }
        $StderrText = if (Test-Path -LiteralPath $ProbeStderr) {
            Get-Content -LiteralPath $ProbeStderr -Raw
        } else { "" }
        if ($ProbeProcess.ExitCode -ne 0) {
            $FailureDetail = ([string]$StderrText).Trim()
            if ([string]::IsNullOrWhiteSpace($FailureDetail)) {
                throw "Executable $ProbeName probe failed with exit code $($ProbeProcess.ExitCode)."
            }
            throw "Executable $ProbeName probe failed with exit code $($ProbeProcess.ExitCode): $FailureDetail"
        }
        # Functional contracts are emitted on stdout.  Dependency warnings on
        # stderr (for example PyMuPDF's legacy `fitz` warning) must not alter a
        # version string or JSON probe payload.
        return $StdoutText
    }
    finally {
        Remove-Item -LiteralPath $ProbeStdout -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ProbeStderr -Force -ErrorAction SilentlyContinue
    }
}

function Write-PortableArtifactBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$WheelPath
    )
    $Payload = [ordered]@{
        schema_version = "pr-mcp-builder-windows-artifact-v1"
        package_version = $ExpectedVersion
        executable_name = Split-Path -Leaf $ExecutablePath
        executable_sha256 = Get-Sha256Lower -LiteralPath $ExecutablePath
        wheel_name = Split-Path -Leaf $WheelPath
        wheel_sha256 = Get-Sha256Lower -LiteralPath $WheelPath
        execution_probe = @("--mcp-server", "--help")
        functional_pdf_parser_probe = @("--portable-self-check")
    }
    Write-Utf8NoBom -LiteralPath $ManifestPath -Content ($Payload | ConvertTo-Json -Depth 4)
}

function Write-PortableWheelSourceBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$WheelPath,
        [Parameter(Mandatory = $true)][string]$SourceFingerprint
    )
    $Payload = [ordered]@{
        schema_version = "pr-mcp-builder-wheel-source-v1"
        package_version = $ExpectedVersion
        wheel_name = Split-Path -Leaf $WheelPath
        wheel_sha256 = Get-Sha256Lower -LiteralPath $WheelPath
        source_fingerprint_sha256 = $SourceFingerprint
    }
    Write-Utf8NoBom -LiteralPath $ManifestPath -Content ($Payload | ConvertTo-Json -Depth 4)
}

function Assert-PortableArtifactBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][string]$WheelPath
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "-SkipExeBuild requires a prior executable/wheel binding manifest: $ManifestPath"
    }
    try {
        $Binding = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Portable executable/wheel binding manifest is not valid JSON: $ManifestPath"
    }
    $ExpectedExeHash = Get-Sha256Lower -LiteralPath $ExecutablePath
    $ExpectedWheelHash = Get-Sha256Lower -LiteralPath $WheelPath
    if (
        $Binding.schema_version -ne "pr-mcp-builder-windows-artifact-v1" -or
        $Binding.package_version -ne $ExpectedVersion -or
        $Binding.executable_name -ne (Split-Path -Leaf $ExecutablePath) -or
        $Binding.wheel_name -ne (Split-Path -Leaf $WheelPath) -or
        $Binding.executable_sha256 -ne $ExpectedExeHash -or
        $Binding.wheel_sha256 -ne $ExpectedWheelHash -or
        (@($Binding.execution_probe) -join "|") -ne "--mcp-server|--help" -or
        (@($Binding.functional_pdf_parser_probe) -join "|") -ne "--portable-self-check"
    ) {
        throw "-SkipExeBuild refused stale or mixed executable/wheel artifacts. Rebuild the Windows executable."
    }
}

function Assert-PortableWheelSourceBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][string]$WheelPath,
        [Parameter(Mandatory = $true)][string]$SourceFingerprint
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "-SkipWheelBuild requires a prior wheel/source binding manifest: $ManifestPath"
    }
    try {
        $Binding = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Portable wheel/source binding manifest is not valid JSON: $ManifestPath"
    }
    if (
        $Binding.schema_version -ne "pr-mcp-builder-wheel-source-v1" -or
        $Binding.package_version -ne $ExpectedVersion -or
        $Binding.wheel_name -ne (Split-Path -Leaf $WheelPath) -or
        $Binding.wheel_sha256 -ne (Get-Sha256Lower -LiteralPath $WheelPath) -or
        $Binding.source_fingerprint_sha256 -ne $SourceFingerprint
    ) {
        throw "-SkipWheelBuild refused a stale wheel for the current source tree. Rebuild the wheel."
    }
}

Push-Location $ProjectRoot
try {
    $PreviousPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
    $PreviousPythonHome = [Environment]::GetEnvironmentVariable("PYTHONHOME", "Process")
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $DetectedVersion = & $BasePython -c "from app import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read the package version from app.__version__."
    }
    $DetectedVersion = ([string]$DetectedVersion).Trim()
    $BasePythonExecutable = & $BasePython -I -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to resolve the base Python executable."
    }
    $BasePythonExecutable = ([string]$BasePythonExecutable).Trim()
    if (-not (Test-Path -LiteralPath $BasePythonExecutable -PathType Leaf)) {
        throw "The resolved base Python executable was not found."
    }
    $BasePythonRoot = Split-Path -Parent $BasePythonExecutable
    $SystemDirectory = [Environment]::SystemDirectory
    if (-not (Test-Path -LiteralPath $SystemDirectory -PathType Container)) {
        throw "The Windows system directory could not be resolved."
    }
    $WindowsRoot = Split-Path -Parent $SystemDirectory
    if ([string]::IsNullOrWhiteSpace($Version)) {
        $Version = $DetectedVersion
    } elseif ($Version -ne $DetectedVersion) {
        throw "Requested release version $Version does not match app.__version__ $DetectedVersion."
    }
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        throw "Version must be a semantic release version such as 1.2.3: $Version"
    }

    $PackageName = "PR-MCP-Builder-Windows-x64-$Version"
    $StageRoot = Join-Path $BuildRoot $PackageName
    $ZipPath = Join-Path $DistRoot "$PackageName.zip"
    $BuiltApp = Join-Path $DistRoot "PR MCP Builder"
    $BuiltExe = Join-Path $BuiltApp "PR MCP Builder.exe"
    $BundledWheelPath = Join-Path $DistRoot "reg_rag_preprocessor-$Version-py3-none-any.whl"
    $WheelSourceBindingPath = Join-Path $DistRoot "reg_rag_preprocessor-$Version-wheel-source.json"
    $ArtifactManifestPath = Join-Path $BuiltApp "release_artifact_manifest.json"
    $ProbeRoot = Join-Path $BuildRoot "portable-executable-probes"
    $WheelSourceFingerprint = Get-PortableWheelSourceFingerprint -ProjectRoot $ProjectRoot
    New-Item -ItemType Directory -Path $DistRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
    Assert-SafeBuildVenvPath -LiteralPath $BuildVenv -ProjectRoot $ProjectRoot -BuildRoot $BuildRoot

    if (-not $SkipExeBuild) {
        if ($RecreateBuildVenv -and (Test-Path -LiteralPath $BuildVenv)) {
            $BuildVenvItem = Get-Item -LiteralPath $BuildVenv -Force
            if (
                ($BuildVenvItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                throw "Refusing to recreate a build virtual environment through a reparse point."
            }
            try {
                Remove-Item -LiteralPath $BuildVenv -Recurse -Force -ErrorAction Stop
            }
            catch {
                $BuildVenv = Join-Path $BuildRoot (".build-venv-" + [Guid]::NewGuid().ToString("N"))
                Assert-SafeBuildVenvPath -LiteralPath $BuildVenv -ProjectRoot $ProjectRoot -BuildRoot $BuildRoot
                $BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
                $BuildPip = Join-Path $BuildVenv "Scripts\pip.exe"
                Write-Warning (
                    "Unable to remove the existing .build-venv cleanly; " +
                    "falling back to a new isolated build venv under build root."
                )
            }
        }
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
        & $BuildPython -m pip install --disable-pip-version-check -e . pyinstaller build
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install build dependencies."
        }
        & $BuildPython -I $IsolationHelper --venv-root $BuildVenv --fail-on-issue
        if ($LASTEXITCODE -ne 0) {
            throw "Build dependency isolation check failed. Recreate .build-venv with an isolated CPython installation."
        }

        if (-not $SkipWheelBuild) {
            & $BuildPython -m build --wheel --outdir $DistRoot
            if ($LASTEXITCODE -ne 0) {
                throw "Wheel build failed with exit code $LASTEXITCODE"
            }
            Write-PortableWheelSourceBinding `
                -ManifestPath $WheelSourceBindingPath `
                -ExpectedVersion $Version `
                -WheelPath $BundledWheelPath `
                -SourceFingerprint $WheelSourceFingerprint
        }

        $VersionParts = @($Version.Split(".") | ForEach-Object { [int]$_ })
        $VersionInfoPath = Join-Path $BuildRoot "$PackageName.version.txt"
        $VersionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), 0),
    prodvers=($($VersionParts[0]), $($VersionParts[1]), $($VersionParts[2]), 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'Public Regulation MCP Builder'),
          StringStruct('FileDescription', 'PR MCP Builder'),
          StringStruct('FileVersion', '$Version'),
          StringStruct('InternalName', 'PR MCP Builder'),
          StringStruct('OriginalFilename', 'PR MCP Builder.exe'),
          StringStruct('ProductName', 'PR MCP Builder'),
          StringStruct('ProductVersion', '$Version')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
        Write-Utf8NoBom -LiteralPath $VersionInfoPath -Content $VersionInfo
        $PreviousVersionFile = $env:PR_MCP_BUILDER_VERSION_FILE
        $PreviousProcessPath = $env:Path
        try {
            $env:PR_MCP_BUILDER_VERSION_FILE = $VersionInfoPath
            $IsolatedBuildPathEntries = @(
                (Join-Path $BuildVenv "Scripts")
                (Join-Path $BasePythonRoot "Scripts")
                $BasePythonRoot
                $SystemDirectory
                $WindowsRoot
                (Join-Path $SystemDirectory "Wbem")
            ) | Select-Object -Unique
            $env:Path = $IsolatedBuildPathEntries -join ";"
            & $BuildPython -I -m PyInstaller `
                --noconfirm `
                --clean `
                --distpath $DistRoot `
                --workpath (Join-Path $BuildRoot "pyinstaller") `
                "packaging\PR-MCP-Builder.spec"
            if ($LASTEXITCODE -ne 0) {
                throw "PyInstaller build failed with exit code $LASTEXITCODE"
            }
        }
        finally {
            $env:Path = $PreviousProcessPath
            if ($null -eq $PreviousVersionFile) {
                Remove-Item Env:PR_MCP_BUILDER_VERSION_FILE -ErrorAction SilentlyContinue
            } else {
                $env:PR_MCP_BUILDER_VERSION_FILE = $PreviousVersionFile
            }
        }
        $AnalysisToc = Join-Path $BuildRoot "pyinstaller\PR-MCP-Builder\Analysis-00.toc"
        $CollectToc = Join-Path $BuildRoot "pyinstaller\PR-MCP-Builder\COLLECT-00.toc"
        $ArtifactAllowedRoots = @(
            $BuildVenv
            $BasePythonRoot
            $WindowsRoot
            (Join-Path $ProjectRoot "app")
            (Join-Path $ProjectRoot "frontend")
            (Join-Path $ProjectRoot "packaging")
            (Join-Path $ProjectRoot "scripts")
        )
        $ArtifactIsolationArgs = @(
            $IsolationHelper
            "--venv-root"
            $BuildVenv
            "--analysis-toc"
            $AnalysisToc
            "--allowed-path"
            $ProjectRoot
            "--allowed-path"
            (Join-Path $BuildRoot "pyinstaller\PR-MCP-Builder\base_library.zip")
            "--binary-toc"
            $AnalysisToc
            "--binary-toc"
            $CollectToc
            "--binary-allowed-root"
            $BuildVenv
            "--binary-allowed-root"
            $BasePythonRoot
            "--binary-allowed-root"
            $WindowsRoot
        )
        foreach ($AllowedRoot in $ArtifactAllowedRoots) {
            $ArtifactIsolationArgs += @("--allowed-root", $AllowedRoot)
        }
        $ArtifactIsolationArgs += "--fail-on-issue"
        & $BuildPython -I @ArtifactIsolationArgs
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller artifact provenance check failed. Rebuild with isolated dependency and DLL search paths."
        }
    }

    if (-not (Test-Path -LiteralPath $BundledWheelPath -PathType Leaf)) {
        throw "Built wheel was not found under $DistRoot. A transferable MCP ZIP requires the wheel."
    }
    if ($SkipWheelBuild) {
        Assert-PortableWheelSourceBinding `
            -ManifestPath $WheelSourceBindingPath `
            -ExpectedVersion $Version `
            -WheelPath $BundledWheelPath `
            -SourceFingerprint $WheelSourceFingerprint
    }
    if ($SkipExeBuild) {
        Assert-PortableArtifactBinding `
            -ManifestPath $ArtifactManifestPath `
            -ExpectedVersion $Version `
            -ExecutablePath $BuiltExe `
            -WheelPath $BundledWheelPath
    }
    Assert-PortableExecutable `
        -ExecutablePath $BuiltExe `
        -ExpectedVersion $Version `
        -ProbeRoot $ProbeRoot
    Write-Host "[OK] Windows executable verified: ProductVersion $Version; MCP and standalone Qwen help"
    if (-not $SkipExeBuild) {
        Write-PortableArtifactBinding `
            -ManifestPath $ArtifactManifestPath `
            -ExpectedVersion $Version `
            -ExecutablePath $BuiltExe `
            -WheelPath $BundledWheelPath
    }
    Write-Host "[OK] Executable/wheel artifact binding verified for version $Version"

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
    Copy-Item -LiteralPath "packaging\INSTALL_KORDOC_KO.ps1" -Destination $StageRoot
    Copy-Item -LiteralPath $BundledWheelPath -Destination $StageRoot

    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    # Windows PowerShell 5 can fail inside Write-Progress while compressing a
    # large portable tree. Suppressing progress avoids that host-only failure
    # without changing the archive contents or compression level.
    $PreviousProgressPreference = $ProgressPreference
    try {
        $ProgressPreference = "SilentlyContinue"
        Compress-Archive `
            -LiteralPath $StageRoot `
            -DestinationPath $ZipPath `
            -CompressionLevel Optimal
    }
    finally {
        $ProgressPreference = $PreviousProgressPreference
    }
    Write-Host "[OK] Windows portable ZIP: $ZipPath"
}
finally {
    if ($null -eq $PreviousPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $PreviousPythonPath
    }
    if ($null -eq $PreviousPythonHome) {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONHOME = $PreviousPythonHome
    }
    Pop-Location
}

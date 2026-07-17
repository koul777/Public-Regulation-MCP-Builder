param(
    [Parameter(Mandatory = $true)]
    [string]$ManifestCsv,

    [string]$ProfileId = "public_portal-etc-law",
    [string]$SourceSystem = "PUBLIC_PORTAL",
    [string]$InstitutionProfiles = ".\config\institution_profiles.example.json",
    [string]$QualityProfiles = ".\config\quality_profiles.example.json",
    [string]$ReportsDir = ".\reports",
    [string]$DataDir = ".\data",
    [int]$MaxUploadMb = 1000,
    [string]$WebhookUrl = "",
    [string]$AlertLog = ".\reports\batch_failure_alerts.jsonl",
    [switch]$ForceReprocess,
    [switch]$FailOnAlert,
    [switch]$IncludeVectorDbHandoff
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Invoke-PythonStep {
    param(
        [string[]]$Arguments,
        [string]$StepName,
        [int[]]$AllowedExitCodes = @(0)
    )

    Write-Host "==> $StepName"
    $output = & python @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($AllowedExitCodes -notcontains $exitCode) {
        Write-Host $output
        throw "$StepName failed with exit code $exitCode"
    }
    return ($output | Out-String)
}

$batchArgs = @(
    "scripts\batch_process_regulations.py",
    "--manifest-csv", $ManifestCsv,
    "--profile-id", $ProfileId,
    "--source-system", $SourceSystem,
    "--institution-profiles", $InstitutionProfiles,
    "--strict-institution-profiles",
    "--quality-profiles", $QualityProfiles,
    "--strict-quality-profiles",
    "--reports-dir", $ReportsDir,
    "--data-dir", $DataDir,
    "--max-upload-mb", "$MaxUploadMb"
)
if ($ForceReprocess.IsPresent) {
    $batchArgs += "--force-reprocess"
}

$batchOutput = Invoke-PythonStep -Arguments $batchArgs -StepName "batch_process_regulations" -AllowedExitCodes @(0, 1, 2)
$batchResult = $batchOutput | ConvertFrom-Json
$batchReport = $batchResult.reports.json
if (-not $batchReport) {
    throw "Batch report path was not returned by batch_process_regulations.py"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$retryJson = Join-Path $ReportsDir "batch_retry_manifest_$timestamp.json"
$ocrJson = Join-Path $ReportsDir "ocr_manifest_$timestamp.json"
$readinessJson = Join-Path $ReportsDir "public_batch_readiness_$timestamp.json"
$readinessMd = Join-Path $ReportsDir "public_batch_readiness_$timestamp.md"
$alertJson = Join-Path $ReportsDir "batch_failure_alert_$timestamp.json"

Invoke-PythonStep -Arguments @(
    "scripts\export_batch_retry_manifest.py",
    "--batch-report", $batchReport,
    "--out-json", $retryJson,
    "--require-existing-files"
) -StepName "export_batch_retry_manifest" | Out-Null

Invoke-PythonStep -Arguments @(
    "scripts\export_ocr_manifest.py",
    "--batch-report", $batchReport,
    "--out-json", $ocrJson,
    "--price-per-page", "0.03"
) -StepName "export_ocr_manifest" | Out-Null

Invoke-PythonStep -Arguments @(
    "scripts\validate_public_batch_readiness.py",
    "--batch-report", $batchReport,
    "--institution-profiles", $InstitutionProfiles,
    "--strict-institution-profiles",
    "--out-json", $readinessJson,
    "--out-md", $readinessMd
) -StepName "validate_public_batch_readiness" | Out-Null

$alertArgs = @(
    "scripts\emit_batch_failure_alert.py",
    "--batch-report", $batchReport,
    "--readiness-report", $readinessJson,
    "--out-json", $alertJson,
    "--alert-log", $AlertLog,
    "--include-local-paths"
)
if ($WebhookUrl) {
    $alertArgs += @("--webhook-url", $WebhookUrl)
}
if ($FailOnAlert.IsPresent) {
    $alertArgs += "--fail-on-alert"
}

$alertOutput = Invoke-PythonStep -Arguments $alertArgs -StepName "emit_batch_failure_alert" -AllowedExitCodes @(0, 1)
$alert = $alertOutput | ConvertFrom-Json

$result = [ordered]@{
    batch_report = $batchReport
    retry_manifest = $retryJson
    ocr_manifest = $ocrJson
    readiness_json = $readinessJson
    readiness_md = $readinessMd
    alert_json = $alertJson
    alert_status = $alert.status
    alert_severity = $alert.severity
}

if ($IncludeVectorDbHandoff.IsPresent) {
    $ingestionJsonl = Join-Path $ReportsDir "vectordb_ingestion_$timestamp.jsonl"
    $ingestionManifest = Join-Path $ReportsDir "vectordb_ingestion_$timestamp.manifest.json"
    $embeddedJsonl = Join-Path $ReportsDir "vectordb_embedded_$timestamp.jsonl"
    $embeddedManifest = Join-Path $ReportsDir "vectordb_embedded_$timestamp.manifest.json"
    $qdrantJsonl = Join-Path $ReportsDir "qdrant_points_$timestamp.jsonl"
    $qdrantManifest = Join-Path $ReportsDir "qdrant_points_$timestamp.manifest.json"

    Invoke-PythonStep -Arguments @(
        "scripts\export_vectordb_ingestion.py",
        "--batch-report", $batchReport,
        "--out-jsonl", $ingestionJsonl,
        "--out-manifest", $ingestionManifest,
        "--fail-on-leak"
    ) -StepName "export_vectordb_ingestion" | Out-Null

    Invoke-PythonStep -Arguments @(
        "scripts\embed_vectordb_records.py",
        "--records-jsonl", $ingestionJsonl,
        "--out-jsonl", $embeddedJsonl,
        "--out-manifest", $embeddedManifest,
        "--dimensions", "384",
        "--fail-on-leak"
    ) -StepName "embed_vectordb_records" | Out-Null

    Invoke-PythonStep -Arguments @(
        "scripts\upsert_vectordb_ingestion.py",
        "--records-jsonl", $embeddedJsonl,
        "--target-type", "qdrant-local-jsonl",
        "--target-path", $qdrantJsonl,
        "--out-manifest", $qdrantManifest,
        "--dry-run"
    ) -StepName "upsert_vectordb_ingestion_qdrant_dry_run" | Out-Null

    Invoke-PythonStep -Arguments @(
        "scripts\upsert_vectordb_ingestion.py",
        "--records-jsonl", $embeddedJsonl,
        "--target-type", "pgvector-local-jsonl",
        "--target-path", (Join-Path $ReportsDir "pgvector_rows_$timestamp.jsonl"),
        "--out-manifest", (Join-Path $ReportsDir "pgvector_rows_$timestamp.manifest.json"),
        "--dry-run"
    ) -StepName "upsert_vectordb_ingestion_pgvector_dry_run" | Out-Null

    Invoke-PythonStep -Arguments @(
        "scripts\upsert_vectordb_ingestion.py",
        "--records-jsonl", $embeddedJsonl,
        "--target-type", "chroma-local-jsonl",
        "--target-path", (Join-Path $ReportsDir "chroma_rows_$timestamp.jsonl"),
        "--out-manifest", (Join-Path $ReportsDir "chroma_rows_$timestamp.manifest.json"),
        "--dry-run"
    ) -StepName "upsert_vectordb_ingestion_chroma_dry_run" | Out-Null

    Invoke-PythonStep -Arguments @(
        "scripts\upsert_vectordb_ingestion.py",
        "--records-jsonl", $embeddedJsonl,
        "--target-type", "qdrant-rest-manifest",
        "--target-path", (Join-Path $ReportsDir "qdrant_rest_manifest_$timestamp.json"),
        "--collection-name", "reg-rag-public-institution",
        "--out-manifest", (Join-Path $ReportsDir "qdrant_rest_manifest_$timestamp.manifest.json"),
        "--dry-run"
    ) -StepName "upsert_vectordb_ingestion_qdrant_rest_manifest" | Out-Null

    $result.vectordb_ingestion_jsonl = $ingestionJsonl
    $result.vectordb_embedded_jsonl = $embeddedJsonl
    $result.qdrant_points_jsonl = $qdrantJsonl
    $result.qdrant_manifest = $qdrantManifest
}

$result | ConvertTo-Json -Depth 6
if ($FailOnAlert.IsPresent -and $alert.status -eq "needs_attention") {
    exit 1
}

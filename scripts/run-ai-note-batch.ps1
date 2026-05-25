param(
    [int]$BatchSize = 3,
    [int]$ScanLimit = 100000,
    [string[]]$Keys = @(),
    [string]$Model = "gpt-5.5",
    [string]$BaseUrl = "http://127.0.0.1:8317/v1",
    [string]$PdfInputMode = "mineru-markdown-images",
    [string]$ApiMode = "auto",
    [string]$OutputDir = "",
    [int]$MaxImages = 24,
    [int]$MaxImageMb = 8,
    [int]$MaxExtractedChars = 180000,
    [int]$ClassifyMaxTokens = 800,
    [int]$AnalysisMaxTokens = 8192,
    [int]$MaxFiles = 8,
    [int]$MaxPdfMb = 100,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$NoCleanIntermediate,
    [switch]$StopOnError,
    [switch]$RefreshMineruCache
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).Path
}

function Read-CliProxyApiKey {
    if ($env:CLIPROXYAPI_KEY) {
        return $env:CLIPROXYAPI_KEY
    }

    $configPath = "C:\Users\chengliu\Apps\CLIProxyAPI\config.yaml"
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "CLIProxyAPI config not found: $configPath"
    }

    foreach ($line in Get-Content -LiteralPath $configPath) {
        if ($line -match '^\s*-\s*"?(sk-cliproxy[^"\s]+)"?\s*$') {
            return $Matches[1]
        }
    }

    throw "No sk-cliproxy API key found in $configPath"
}

function New-RunOutputDir([string]$RepoRoot, [string]$RequestedOutputDir) {
    if ($RequestedOutputDir) {
        return $RequestedOutputDir
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    return Join-Path $RepoRoot ".workspace\ai-note-analysis-batch-$stamp"
}

function Invoke-AiNoteBatch {
    param(
        [string]$RepoRoot,
        [string]$RunOutputDir,
        [int]$Iteration
    )

    $checkpoint = Join-Path $RunOutputDir "checkpoint.json"
    $logPath = Join-Path $RunOutputDir ("logs\batch-{0:000}.log" -f $Iteration)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null

    $cmd = @(
        "run", "python", "-u", "scripts/analyze_ai_notes.py",
        "--limit", "$BatchSize",
        "--scan-limit", "$ScanLimit",
        "--pdf-input-mode", $PdfInputMode,
        "--api-mode", $ApiMode,
        "--model", $Model,
        "--base-url", $BaseUrl,
        "--api-key-env", "CLIPROXYAPI_KEY",
        "--output-dir", $RunOutputDir,
        "--checkpoint", $checkpoint,
        "--max-images", "$MaxImages",
        "--max-image-mb", "$MaxImageMb",
        "--max-extracted-chars", "$MaxExtractedChars",
        "--classify-max-tokens", "$ClassifyMaxTokens",
        "--analysis-max-tokens", "$AnalysisMaxTokens",
        "--max-files", "$MaxFiles",
        "--max-pdf-mb", "$MaxPdfMb"
    )

    if (-not $DryRun) {
        $cmd += "--apply"
    }
    if ($Force) {
        $cmd += "--force"
    }
    if ($StopOnError) {
        $cmd += "--stop-on-error"
    }
    if ($RefreshMineruCache) {
        $cmd += "--refresh-mineru-cache"
    }
    if ($Keys.Count -gt 0) {
        $cmd += "--keys"
        $cmd += $Keys
    }

    Write-Host ""
    Write-Host ("========== AI note batch {0} ==========" -f $Iteration)
    Write-Host "Output: $RunOutputDir"
    Write-Host "Log:    $logPath"
    Write-Host "Mode:   $PdfInputMode -> $Model"

    Push-Location $RepoRoot
    try {
        & uv @cmd 2>&1 | Tee-Object -FilePath $logPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "Batch $Iteration failed with exit code $exitCode. See $logPath"
    }
}

function Read-BatchSummary([string]$RunOutputDir) {
    $summaryPath = Join-Path $RunOutputDir "summary.json"
    $previewPath = Join-Path $RunOutputDir "preview.json"
    if (Test-Path -LiteralPath $summaryPath) {
        return Get-Content -LiteralPath $summaryPath -Raw | ConvertFrom-Json
    }
    if (Test-Path -LiteralPath $previewPath) {
        return Get-Content -LiteralPath $previewPath -Raw | ConvertFrom-Json
    }
    return $null
}

function Remove-IntermediateAssets([string]$RunOutputDir) {
    if ($NoCleanIntermediate) {
        return
    }
    $assetsDir = Join-Path $RunOutputDir "mineru-assets"
    if (Test-Path -LiteralPath $assetsDir) {
        Remove-Item -LiteralPath $assetsDir -Recurse -Force
        Write-Host "Cleaned intermediate MinerU assets: $assetsDir"
    }
}

$repoRoot = Get-RepoRoot
$runOutputDir = New-RunOutputDir -RepoRoot $repoRoot -RequestedOutputDir $OutputDir
New-Item -ItemType Directory -Force -Path $runOutputDir | Out-Null

$env:CLIPROXYAPI_KEY = Read-CliProxyApiKey

Write-Host "Repo:   $repoRoot"
Write-Host "Output: $runOutputDir"
Write-Host "DryRun: $DryRun"
Write-Host "Clean intermediate assets after successful batches: $(-not $NoCleanIntermediate)"

$iteration = 1
while ($true) {
    Invoke-AiNoteBatch -RepoRoot $repoRoot -RunOutputDir $runOutputDir -Iteration $iteration
    $summary = Read-BatchSummary -RunOutputDir $runOutputDir
    if ($null -eq $summary) {
        throw "No summary or preview file found under $runOutputDir"
    }

    $results = 0
    $failures = 0
    $prepared = 0
    if ($summary.PSObject.Properties.Name -contains "results") {
        $results = [int]$summary.results
    }
    if ($summary.PSObject.Properties.Name -contains "batch_results") {
        $results = [int]$summary.batch_results
    }
    if ($summary.PSObject.Properties.Name -contains "failures") {
        $failures = [int]$summary.failures
    }
    if ($summary.PSObject.Properties.Name -contains "batch_failures") {
        $failures = [int]$summary.batch_failures
    }
    if ($summary.PSObject.Properties.Name -contains "candidate_items") {
        $prepared = [int]$summary.candidate_items
    }

    if ($DryRun) {
        Write-Host "Dry-run prepared candidates: $prepared"
        break
    }

    if ($failures -gt 0) {
        Write-Warning "Batch $iteration finished with $failures failure(s). Intermediate files are kept for diagnosis."
        break
    }

    if ($results -gt 0) {
        Remove-IntermediateAssets -RunOutputDir $runOutputDir
        Write-Host "Batch $iteration completed: $results item(s) written."
        $iteration += 1
        continue
    }

    Write-Host "No more writable candidates found. Batch processing complete."
    break
}

Write-Host ""
Write-Host "Final output directory:"
Write-Host $runOutputDir
Write-Host "Keep Zotero open or run Zotero sync so local SQLite can reflect Web API writes."

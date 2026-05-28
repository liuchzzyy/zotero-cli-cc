param(
    [string]$Workspace = "log\ai-note-keyword-update",
    [string]$Model = "deepseek-v4-flash",
    [int]$BatchSize = 4,
    [int]$RetryBatchSize = 1,
    [int]$ZoteroTimeout = 90,
    [string]$PromptPath = "",
    [string]$Profile = "",
    [switch]$Generate,
    [switch]$RetryFailed,
    [switch]$DryRunApply,
    [switch]$Apply,
    [switch]$Status,
    [switch]$FullRun,
    [switch]$NoSkipDoneTag
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).Path
}

function Resolve-RunPath([string]$RepoRoot, [string]$PathValue) {
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return $PathValue
    }
    return Join-Path $RepoRoot $PathValue
}

function Invoke-LoggedCommand {
    param(
        [string]$RepoRoot,
        [string]$LogPath,
        [string[]]$Command
    )

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
    Push-Location $RepoRoot
    try {
        $exe = $Command[0]
        $args = $Command[1..($Command.Count - 1)]
        Write-Host ""
        Write-Host "========== $($Command -join ' ') =========="
        Write-Host "Log: $LogPath"
        & $exe @args 2>&1 | Tee-Object -FilePath $LogPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "Command failed with exit code $exitCode. See $LogPath"
    }
}

function New-BaseCommand([string]$WorkspacePath) {
    $cmd = @("uv", "run", "python", "scripts/update_ai_note_keywords.py", "--workspace", $WorkspacePath)
    if ($PromptPath) {
        $resolvedPromptPath = Resolve-RunPath -RepoRoot $repoRoot -PathValue $PromptPath
        $cmd += @("--prompt-path", $resolvedPromptPath)
    }
    if ($Profile) {
        $cmd += @("--profile", $Profile)
    }
    return $cmd
}

$repoRoot = Get-RepoRoot
$workspacePath = Resolve-RunPath -RepoRoot $repoRoot -PathValue $Workspace
$logsDir = Join-Path $workspacePath "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

if ($FullRun) {
    $Generate = $true
    $RetryFailed = $true
    $DryRunApply = $true
    $Apply = $true
    $Status = $true
}

if (-not ($Generate -or $RetryFailed -or $DryRunApply -or $Apply -or $Status)) {
    $Status = $true
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$base = New-BaseCommand -WorkspacePath $workspacePath

Write-Host "Repo:      $repoRoot"
Write-Host "Workspace: $workspacePath"
Write-Host "Model:     $Model"
if ($PromptPath) {
    Write-Host "Prompt:    $(Resolve-RunPath -RepoRoot $repoRoot -PathValue $PromptPath)"
}

if ($Generate) {
    $cmd = $base + @("generate", "--batch-size", "$BatchSize", "--model", $Model)
    if (-not $NoSkipDoneTag) {
        $cmd += "--skip-done-tag"
    }
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "generate-$stamp.log") -Command $cmd
}

if ($RetryFailed) {
    $cmd = $base + @("generate", "--retry-failed", "--batch-size", "$RetryBatchSize", "--model", $Model)
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "retry-failed-$stamp.log") -Command $cmd
}

if ($DryRunApply) {
    $cmd = $base + @("apply", "--dry-run", "--zotero-timeout", "$ZoteroTimeout")
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "apply-dry-run-$stamp.log") -Command $cmd
}

if ($Apply) {
    $cmd = $base + @("apply", "--zotero-timeout", "$ZoteroTimeout")
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "apply-$stamp.log") -Command $cmd
}

if ($Status) {
    $cmd = $base + @("status")
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "status-$stamp.log") -Command $cmd
}

param(
    [string]$WorkspaceName = "full-library-pdf-rag",
    [string]$Extractor = "mineru",
    [int]$ScanLimit = 100000,
    [int]$ProgressEvery = 100,
    [string]$OutputDir = "",
    [switch]$DryRun,
    [switch]$NoIndex,
    [switch]$ForceRebuild,
    [switch]$KeepInventory,
    [switch]$StopOnError
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..")).Path
}

function New-RunOutputDir([string]$RepoRoot, [string]$RequestedOutputDir) {
    if ($RequestedOutputDir) {
        return $RequestedOutputDir
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    return Join-Path $RepoRoot ".workspace\rag-full-library-$stamp"
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

function Write-InventoryScript([string]$ScriptPath) {
    @'
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from zotero_cli_agents.config import get_data_dir, get_prefs_js_path, load_config, resolve_library_id
from zotero_cli_agents.core.reader import ZoteroReader
from zotero_cli_agents.core.rag_index import RagIndex
from zotero_cli_agents.core.workspace import Workspace, load_workspace, save_workspace, workspace_exists, workspace_index_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--scan-limit", type=int, required=True)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    data_dir = get_data_dir(cfg)
    db_path = data_dir / "zotero.sqlite"
    library_ctx = {"library_type": "user", "group_id": None}
    library_id = resolve_library_id(db_path, library_ctx)
    reader = ZoteroReader(db_path, library_id=library_id, prefs_js_path=get_prefs_js_path(cfg))

    try:
        print(f"[inventory] reading local Zotero DB {db_path}", flush=True)
        items = reader.search("", limit=args.scan_limit).items
        total = len(items)
        pdf_items: list[dict[str, object]] = []
        skipped_no_local_pdf = 0

        for idx, item in enumerate(items, 1):
            pdfs = reader.get_pdf_attachments(item.key)
            local_pdfs = [
                {
                    "key": att.key,
                    "filename": att.filename,
                    "path": str(att.path) if att.path else "",
                }
                for att in pdfs
                if att.path is not None and att.path.exists()
            ]
            if local_pdfs:
                pdf_items.append(
                    {
                        "key": item.key,
                        "title": item.title,
                        "item_type": item.item_type,
                        "pdf_count": len(local_pdfs),
                        "pdfs": local_pdfs,
                    }
                )
            elif pdfs:
                skipped_no_local_pdf += 1

            if idx % args.progress_every == 0 or idx == total:
                print(
                    "[inventory] "
                    f"scanned={idx}/{total} local_pdf_items={len(pdf_items)} "
                    f"pdf_but_missing={skipped_no_local_pdf}",
                    flush=True,
                )

        existing_keys: set[str] = set()
        indexed_keys: set[str] = set()
        added = 0
        workspace_created = False

        if workspace_exists(args.workspace):
            ws = load_workspace(args.workspace)
            existing_keys = {entry.key for entry in ws.items}
        else:
            ws = Workspace(
                name=args.workspace,
                created=utc_now(),
                description="Auto-maintained workspace containing all local Zotero parent items with existing PDF attachments.",
            )
            workspace_created = True

        for row in pdf_items:
            key = str(row["key"])
            if key not in existing_keys:
                existing_keys.add(key)
                added += 1
                if not args.dry_run:
                    ws.add_item(key, str(row.get("title") or ""))

        if not args.dry_run:
            save_workspace(ws)

        idx_path = workspace_index_path(args.workspace)
        if idx_path.exists():
            idx = RagIndex(idx_path)
            try:
                indexed_keys = idx.get_indexed_keys()
            finally:
                idx.close()

        pdf_keys = {str(row["key"]) for row in pdf_items}
        pending_index = sorted(pdf_keys - indexed_keys)
        payload = {
            "created_at": utc_now(),
            "workspace": args.workspace,
            "dry_run": args.dry_run,
            "db_path": str(db_path),
            "scanned_items": total,
            "local_pdf_items": len(pdf_items),
            "pdf_but_missing_local_file": skipped_no_local_pdf,
            "workspace_created": workspace_created,
            "workspace_existing_items": len(existing_keys) - added,
            "workspace_added_items": added,
            "indexed_items": len(indexed_keys),
            "pending_index_items": len(pending_index),
            "pending_index_keys": pending_index,
            "items": pdf_items,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            "[inventory-summary] "
            f"local_pdf_items={len(pdf_items)} added_to_workspace={added} "
            f"indexed={len(indexed_keys)} pending_index={len(pending_index)} "
            f"output={args.output}",
            flush=True,
        )
    finally:
        reader.close()


if __name__ == "__main__":
    main()
'@ | Set-Content -LiteralPath $ScriptPath -Encoding UTF8
}

$repoRoot = Get-RepoRoot
$runOutputDir = New-RunOutputDir -RepoRoot $repoRoot -RequestedOutputDir $OutputDir
$logsDir = Join-Path $runOutputDir "logs"
$inventoryPath = Join-Path $runOutputDir "inventory.json"
$inventoryScript = Join-Path $runOutputDir "inventory_full_pdf_workspace.py"

New-Item -ItemType Directory -Force -Path $runOutputDir | Out-Null
Write-InventoryScript -ScriptPath $inventoryScript

Write-Host "Repo:       $repoRoot"
Write-Host "Workspace:  $WorkspaceName"
Write-Host "Extractor:  $Extractor"
Write-Host "Output:     $runOutputDir"
Write-Host "DryRun:     $DryRun"
Write-Host "NoIndex:    $NoIndex"
Write-Host "Force:      $ForceRebuild"

$inventoryCmd = @(
    "uv", "run", "python", "-u", $inventoryScript,
    "--workspace", $WorkspaceName,
    "--scan-limit", "$ScanLimit",
    "--progress-every", "$ProgressEvery",
    "--output", $inventoryPath
)
if ($DryRun) {
    $inventoryCmd += "--dry-run"
}

try {
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "inventory.log") -Command $inventoryCmd

    $inventory = Get-Content -LiteralPath $inventoryPath -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host "Inventory summary:"
    Write-Host ("  scanned_items:                {0}" -f $inventory.scanned_items)
    Write-Host ("  local_pdf_items:              {0}" -f $inventory.local_pdf_items)
    Write-Host ("  pdf_but_missing_local_file:   {0}" -f $inventory.pdf_but_missing_local_file)
    Write-Host ("  workspace_added_items:        {0}" -f $inventory.workspace_added_items)
    Write-Host ("  indexed_items:                {0}" -f $inventory.indexed_items)
    Write-Host ("  pending_index_items:          {0}" -f $inventory.pending_index_items)

    if ($DryRun) {
        Write-Host "Dry-run complete. No workspace or RAG index changes were made."
        return
    }

    if ($NoIndex) {
        Write-Host "Workspace inventory updated. Skipped RAG indexing because -NoIndex was set."
        return
    }

    if (($inventory.local_pdf_items -eq 0) -and (-not $ForceRebuild)) {
        Write-Host "No local PDF items found. Nothing to index."
        return
    }

    if (($inventory.pending_index_items -eq 0) -and (-not $ForceRebuild)) {
        Write-Host "RAG index is already up to date for workspace '$WorkspaceName'."
        return
    }

    $indexCmd = @("uv", "run", "zot", "workspace", "index", $WorkspaceName, "--extractor", $Extractor)
    if ($ForceRebuild) {
        $indexCmd += "--force"
    }

    Write-Host ""
    Write-Host "Starting RAG index. This may take a long time for MinerU extraction."
    Write-Host "Progress is streamed and also saved to logs\index.log."
    Invoke-LoggedCommand -RepoRoot $repoRoot -LogPath (Join-Path $logsDir "index.log") -Command $indexCmd
}
catch {
    Write-Error $_
    if ($StopOnError) {
        throw
    }
    exit 1
}
finally {
    if (-not $KeepInventory) {
        Remove-Item -LiteralPath $inventoryScript -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "RAG full-library run complete."
Write-Host "Workspace and index are under .workspace\$WorkspaceName."
Write-Host "PDF text cache is under .workspace\_cache and is intentionally kept for incremental reuse."

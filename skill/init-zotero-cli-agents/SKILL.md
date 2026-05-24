---
name: init-zotero-cli-agents
description: "Guide agents through installing, configuring, and validating zotero-cli-agents after cloning or on a new machine. Use when users ask to initialize, set up, configure, verify, or troubleshoot zot/zotero-cli-agents, including Zotero data directory, API credentials, read/write checks, and MCP readiness."
---

# Init zotero-cli-agents

Use this skill as a guided setup wizard. Do not jump straight to commands without explaining what each stage checks and what the user must provide.

The normal daily literature workflow uses the `zotero-cli-agents` skill. This skill is only for installation, configuration, and validation.

## First Step

Run the bundled checker first when possible:

```powershell
uv run python skill/init-zotero-cli-agents/scripts/check_init.py
```

If `uv` is not available, try:

```powershell
python skill/init-zotero-cli-agents/scripts/check_init.py
```

Use `--json` when another tool or agent needs to parse the result. The checker is read-only by default and prints next actions.

## Guided Workflow

### 1. Identify execution mode

If the current folder contains `pyproject.toml` for `zotero-cli-agents`, use source commands:

```powershell
uv run zot ...
```

If outside the repo, use installed CLI commands:

```powershell
zot ...
```

If neither works, tell the user whether the missing piece is `uv`, the cloned repo, or the installed `zot` executable.

### 2. Install or sync dependencies

In a cloned repo, guide the user to run:

```powershell
uv sync --dev --extra mcp
```

Validate:

```powershell
uv run zot --help
uv run zot schema
```

Only run `uv sync --dev --extra mcp` after telling the user it installs development, test, docs-adjacent, and MCP dependencies into the local environment.

### 3. Configure Zotero data directory

Explain that the Zotero data directory is the folder containing `zotero.sqlite`, not the Zotero application folder and not a cloud PDF folder.

Ask the user to find it in Zotero:

```text
Zotero Settings -> Advanced -> Data Directory Location
```

If auto-detection works, show the detected path and validate it. If not, ask for the path and run, after confirmation:

```powershell
uv run zot config init --data-dir "C:\path\to\Zotero"
```

Validate read access:

```powershell
uv run zot config show
uv run zot --json stats
```

If stats succeeds, report `Local read access: OK`.

### 4. Decide whether write access is needed

Read-only setup is valid. If the user only needs search/read/export/PDF extraction/workspace metadata, stop after read validation and report:

```text
OK: read-only configured
```

If they need add/update/delete/tag/note/attach/collection writes, continue.

### 5. Configure Zotero Web API credentials

Tell the user to create a Zotero Web API key:

```text
https://www.zotero.org/settings/keys
```

Required properties:

- Use the numeric user Library ID, not the username.
- Give the key write access if write commands are needed.
- Do not print the full API key in final answers or logs.

After the user provides values or confirms they are available, run one of:

```powershell
uv run zot config init
```

or:

```powershell
uv run zot config init --library-id "<numeric-id>" --api-key "<api-key>" --data-dir "<data-dir>"
```

Validate without mutating Zotero:

```powershell
uv run zot add --doi "10.1038/s41586-023-06139-9" --dry-run
```

If dry-run succeeds, report `Write credentials: OK`.

### 6. Check MCP readiness

Do not start a long-running MCP server just to validate setup. Check the entry point:

```powershell
uv run zot mcp serve --help
```

Then point the user to `docs/en/mcp/setup.md` or `docs/zh/mcp/setup.md` for client-specific JSON snippets.

### 7. Final report

End with one of these statuses:

- `OK: fully configured` - CLI, local read access, write dry-run, and MCP entry point all work.
- `OK: read-only configured` - CLI and local read access work, API credentials are absent or intentionally skipped.
- `PARTIAL: missing API credentials` - local reads work but write dry-run cannot run.
- `NEEDS_ACTION: Zotero data directory not found` - ask for the folder containing `zotero.sqlite`.
- `FAILED: CLI cannot run` - explain the failing command and first fix.

Include the exact commands already run and the next command the user should run. Mask API keys, for example `***abcd`.

## Safety Rules

- Never write directly to `zotero.sqlite`.
- Never run real write commands for validation; use `--dry-run`.
- Never echo a full API key.
- Do not edit shell profiles or global environment variables unless the user explicitly asks.
- If configuration already works, still check each stage and say it is already configured.

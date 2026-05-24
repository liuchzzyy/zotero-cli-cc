#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    command: str | None = None


def run_command(args: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "") + "\nTimed out"
    return proc.returncode, proc.stdout.strip()


def command_detail(rc: int, output: str, ok_message: str) -> str:
    if rc == 0:
        return ok_message
    return output[:500]


def find_repo_root(start: Path) -> Path | None:
    for path in (start, *start.parents):
        pyproject = path / "pyproject.toml"
        if not pyproject.exists():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("project", {}).get("name") == "zotero-cli-agents":
            return path
    return None


def load_config() -> dict[str, Any]:
    path = Path.home() / ".config" / "zot" / "config.toml"
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    return {"path": str(path), "exists": True, "data": data}


def config_value(config: dict[str, Any], key: str) -> str:
    data = config.get("data", {})
    profile_name = data.get("default", {}).get("profile", "")
    if profile_name and profile_name in data.get("profile", {}):
        return str(data["profile"][profile_name].get(key, "") or "")
    return str(data.get("zotero", {}).get(key, "") or "")


def detect_data_dir(config: dict[str, Any]) -> Path | None:
    env_dir = os.environ.get("ZOT_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    cfg_dir = config_value(config, "data_dir")
    if cfg_dir:
        return Path(cfg_dir).expanduser()
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Zotero")
        if localappdata:
            candidates.append(Path(localappdata) / "Zotero")
        candidates.append(Path.home() / "Zotero")
    else:
        candidates.append(Path.home() / "Zotero")
    for candidate in candidates:
        if (candidate / "zotero.sqlite").exists():
            return candidate
    return candidates[0] if candidates else None


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "***"
    return "***" + value[-4:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check zotero-cli-agents initialization status.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--fix", action="store_true", help="Run low-risk setup steps such as uv sync in a source repo.")
    args = parser.parse_args()

    cwd = Path.cwd()
    repo_root = find_repo_root(cwd)
    uv = shutil.which("uv")
    zot = shutil.which("zot")
    checks: list[Check] = []
    next_actions: list[str] = []

    if repo_root:
        checks.append(Check("repository", "OK", f"source repo found at {repo_root}"))
    else:
        checks.append(Check("repository", "INFO", "not running inside a zotero-cli-agents source repo"))

    if uv:
        checks.append(Check("uv", "OK", uv))
    else:
        checks.append(Check("uv", "NEEDS_ACTION", "uv is not on PATH"))
        next_actions.append("Install uv, then rerun this checker from the cloned repository.")

    command_root = repo_root or cwd
    if repo_root and uv:
        if args.fix:
            rc, out = run_command(["uv", "sync", "--dev", "--extra", "mcp"], repo_root, timeout=180)
            checks.append(
                Check("dependency-sync", "OK" if rc == 0 else "FAILED", out[-500:], "uv sync --dev --extra mcp")
            )
        zot_cmd = ["uv", "run", "zot"]
        zot_label = "uv run zot"
    elif zot:
        zot_cmd = ["zot"]
        zot_label = "zot"
    else:
        zot_cmd = []
        zot_label = "zot"

    if zot_cmd:
        rc, out = run_command([*zot_cmd, "--help"], command_root)
        checks.append(
            Check(
                "cli-help",
                "OK" if rc == 0 else "FAILED",
                command_detail(rc, out, "CLI help is available"),
                f"{zot_label} --help",
            )
        )
        rc, out = run_command([*zot_cmd, "schema"], command_root)
        checks.append(
            Check(
                "cli-schema",
                "OK" if rc == 0 else "FAILED",
                command_detail(rc, out, "CLI schema introspection works"),
                f"{zot_label} schema",
            )
        )
        if (
            repo_root
            and uv
            and any(check.status == "FAILED" for check in checks if check.name in {"cli-help", "cli-schema"})
        ):
            next_actions.append("Run: uv sync --dev --extra mcp")
    else:
        checks.append(Check("cli", "FAILED", "No source CLI or installed zot executable found"))
        next_actions.append("From the repo, run: uv sync --dev --extra mcp")

    config = load_config()
    if config.get("exists"):
        checks.append(Check("config-file", "OK", str(config["path"])))
    else:
        checks.append(Check("config-file", "INFO", f"not found at {config['path']}"))

    data_dir = detect_data_dir(config)
    if data_dir and (data_dir / "zotero.sqlite").exists():
        checks.append(Check("zotero-data-dir", "OK", str(data_dir)))
    else:
        detail = str(data_dir) if data_dir else "no candidate detected"
        checks.append(Check("zotero-data-dir", "NEEDS_ACTION", f"zotero.sqlite not found: {detail}"))
        next_actions.append("Find the Zotero data directory in Zotero Settings -> Advanced -> Data Directory Location.")
        next_actions.append(f'Run: {zot_label} config init --data-dir "<folder-containing-zotero.sqlite>"')

    env_library_id = os.environ.get("ZOT_LIBRARY_ID", "")
    env_api_key = os.environ.get("ZOT_API_KEY", "")
    library_id = env_library_id or config_value(config, "library_id")
    api_key = env_api_key or config_value(config, "api_key")
    if library_id and api_key:
        checks.append(Check("write-credentials", "OK", f"library_id={library_id}, api_key={mask_secret(api_key)}"))
    elif library_id or api_key:
        checks.append(Check("write-credentials", "PARTIAL", "library_id or api_key is missing"))
        next_actions.append(f'Run: {zot_label} config init --library-id "<numeric-id>" --api-key "<api-key>"')
    else:
        checks.append(Check("write-credentials", "INFO", "not configured; read-only setup may still be valid"))
        next_actions.append("For writes, create an API key at https://www.zotero.org/settings/keys")

    if zot_cmd and data_dir and (data_dir / "zotero.sqlite").exists():
        rc, out = run_command([*zot_cmd, "--json", "stats"], command_root)
        checks.append(
            Check(
                "read-validation",
                "OK" if rc == 0 else "FAILED",
                command_detail(rc, out, "Local Zotero SQLite read works"),
                f"{zot_label} --json stats",
            )
        )

    if zot_cmd and library_id and api_key:
        rc, out = run_command(
            [*zot_cmd, "add", "--doi", "10.1038/s41586-023-06139-9", "--dry-run"],
            command_root,
            timeout=60,
        )
        checks.append(
            Check(
                "write-dry-run",
                "OK" if rc == 0 else "FAILED",
                command_detail(rc, out, "Write credentials pass dry-run validation"),
                f"{zot_label} add --doi ... --dry-run",
            )
        )

    if zot_cmd:
        rc, out = run_command([*zot_cmd, "mcp", "serve", "--help"], command_root)
        checks.append(
            Check(
                "mcp-entrypoint",
                "OK" if rc == 0 else "FAILED",
                command_detail(rc, out, "MCP server entry point is available"),
                f"{zot_label} mcp serve --help",
            )
        )

    statuses = {check.name: check.status for check in checks}
    if (
        statuses.get("cli-help") == "OK"
        and statuses.get("read-validation") == "OK"
        and statuses.get("write-dry-run") == "OK"
        and statuses.get("mcp-entrypoint") == "OK"
    ):
        summary = "OK: fully configured"
    elif statuses.get("cli-help") == "OK" and statuses.get("read-validation") == "OK":
        summary = "OK: read-only configured"
    elif statuses.get("zotero-data-dir") == "NEEDS_ACTION":
        summary = "NEEDS_ACTION: Zotero data directory not found"
    elif statuses.get("cli-help") != "OK":
        summary = "FAILED: CLI cannot run"
    else:
        summary = "PARTIAL: missing API credentials"

    result = {"summary": summary, "checks": [asdict(check) for check in checks], "next_actions": next_actions}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(summary)
        for check in checks:
            command = f" [{check.command}]" if check.command else ""
            print(f"- {check.status}: {check.name}{command} - {check.detail}")
        if next_actions:
            print("\nNext actions:")
            for action in dict.fromkeys(next_actions):
                print(f"- {action}")

    return 0 if summary.startswith("OK:") else 1


if __name__ == "__main__":
    raise SystemExit(main())

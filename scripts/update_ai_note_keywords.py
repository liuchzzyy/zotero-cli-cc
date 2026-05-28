from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from markdownify import markdownify as md
from openai import OpenAI
from pyzotero.zotero_errors import PyZoteroError

from zotero_cli_agents.config import get_data_dir, load_ai_note_config, load_config, resolve_write_credentials
from zotero_cli_agents.core.writer import ZoteroWriteError, ZoteroWriter

AI_NOTE_TAG = "workflow/ai_note"
KEYWORD_DONE_TAG = "workflow/keyword"
DEFAULT_WORKSPACE = Path("log") / "ai-note-keyword-update"
DEFAULT_PROMPT_PATH = Path(__file__).with_name("update_ai_note_keywords_prompt.json")

CONTEXT_PATTERNS = (
    "研究问题",
    "重要性",
    "方法新颖",
    "创新",
    "机制",
    "性能",
    "容量",
    "循环",
    "倍率",
    "稳定",
    "表征",
    "XAS",
    "XRD",
    "Raman",
    "TEM",
    "SEM",
    "STEM",
    "XPS",
    "EPR",
    "ESR",
    "EXAFS",
    "NEXAFS",
    "TXM",
    "XRF",
    "DFT",
    "FEFF",
    "Pourbaix",
    "E-pH",
    "理论",
    "制备",
    "水热",
    "掺杂",
    "包覆",
    "插层",
    "预嵌入",
    "添加剂",
    "氧空位",
    "ZSH",
    "ZMHS",
    "ZnMn",
    "MnOOH",
    "溶解",
    "沉积",
    "Grotthuss",
    "EQCM",
    "operando",
    "原位",
    "非原位",
    "ex situ",
    "in situ",
    "结论",
)


@dataclass
class ItemRecord:
    item_id: int
    key: str
    item_type: str
    title: str
    doi: str
    current_citation_key: str
    tags: list[str]
    note_key: str
    note_text: str


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def html_note_to_text(note_html: str) -> str:
    text = md(note_html or "", strip=["img"]).replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_system_prompt(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    prompt = payload.get("system_prompt")
    if isinstance(prompt, str):
        return prompt.strip()
    if isinstance(prompt, list) and all(isinstance(line, str) for line in prompt):
        return "\n".join(prompt).strip()
    raise ValueError(f"Prompt file must define system_prompt as a string or string list: {path}")


def compact_context(note_text: str, *, max_chars: int) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in note_text.splitlines()]
    lines = [line for line in lines if line]
    first_part = "\n".join(lines[:45])
    selected: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if len(line) < 8:
            continue
        lowered = line.lower()
        if any(pattern.lower() in lowered for pattern in CONTEXT_PATTERNS):
            if line not in seen:
                selected.append(line)
                seen.add(line)
        if len("\n".join(selected)) > max_chars:
            break
    combined = first_part + "\n\n关键摘录:\n" + "\n".join(selected)
    return combined[:max_chars]


def read_ai_note_items(db_path: Path) -> list[ItemRecord]:
    conn = connect_readonly(db_path)
    try:
        field_ids = {
            row["fieldName"]: row["fieldID"]
            for row in conn.execute(
                "SELECT fieldID, fieldName FROM fields WHERE fieldName IN ('title','DOI','citationKey')"
            ).fetchall()
        }
        title_id = field_ids["title"]
        doi_id = field_ids.get("DOI", -1)
        citation_id = field_ids.get("citationKey", -1)
        rows = conn.execute(
            """
            SELECT
              i.itemID,
              i.key,
              ty.typeName AS itemType,
              title.value AS title,
              doi.value AS doi,
              ck.value AS citationKey,
              note_i.key AS noteKey,
              n.note AS noteHtml,
              GROUP_CONCAT(t_all.name, '||') AS tags
            FROM itemTags it
            JOIN tags t ON t.tagID = it.tagID
            JOIN items i ON i.itemID = it.itemID
            JOIN itemTypes ty ON ty.itemTypeID = i.itemTypeID
            JOIN itemNotes n ON n.parentItemID = i.itemID
            JOIN items note_i ON note_i.itemID = n.itemID
            LEFT JOIN deletedItems di ON di.itemID = i.itemID
            LEFT JOIN itemData id_title ON id_title.itemID = i.itemID AND id_title.fieldID = ?
            LEFT JOIN itemDataValues title ON title.valueID = id_title.valueID
            LEFT JOIN itemData id_doi ON id_doi.itemID = i.itemID AND id_doi.fieldID = ?
            LEFT JOIN itemDataValues doi ON doi.valueID = id_doi.valueID
            LEFT JOIN itemData id_ck ON id_ck.itemID = i.itemID AND id_ck.fieldID = ?
            LEFT JOIN itemDataValues ck ON ck.valueID = id_ck.valueID
            LEFT JOIN itemTags it_all ON it_all.itemID = i.itemID
            LEFT JOIN tags t_all ON t_all.tagID = it_all.tagID
            WHERE t.name = ?
              AND ty.typeName NOT IN ('note','attachment','annotation')
              AND di.itemID IS NULL
            GROUP BY i.itemID
            ORDER BY i.key
            """,
            (title_id, doi_id, citation_id, AI_NOTE_TAG),
        ).fetchall()
    finally:
        conn.close()

    records = []
    for row in rows:
        tags = [tag for tag in (row["tags"] or "").split("||") if tag]
        records.append(
            ItemRecord(
                item_id=int(row["itemID"]),
                key=str(row["key"]),
                item_type=str(row["itemType"] or ""),
                title=str(row["title"] or ""),
                doi=str(row["doi"] or ""),
                current_citation_key=str(row["citationKey"] or ""),
                tags=tags,
                note_key=str(row["noteKey"] or ""),
                note_text=html_note_to_text(str(row["noteHtml"] or "")),
            )
        )
    return records


def load_jsonl_by_key(path: Path) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        key = str(payload.get("key") or "")
        if key:
            data[key] = payload
    return data


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def unresolved_rows(failed: dict[str, dict[str, Any]], resolved: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: row for key, row in failed.items() if key not in resolved}


def write_status_files(workspace: Path, *, selected_count: int | None = None, model: str | None = None) -> dict[str, Any]:
    generated = load_jsonl_by_key(workspace / "generated.jsonl")
    generation_failed = load_jsonl_by_key(workspace / "failed_generation.jsonl")
    applied = load_jsonl_by_key(workspace / "applied.jsonl")
    apply_failed = load_jsonl_by_key(workspace / "failed_apply.jsonl")

    unresolved_generation_failed = unresolved_rows(generation_failed, generated)
    unresolved_apply_failed = unresolved_rows(apply_failed, applied)
    not_applied = {
        key: row
        for key, row in generated.items()
        if key not in applied and key not in unresolved_apply_failed
    }

    remaining_rows: list[dict[str, Any]] = []
    for key, row in sorted(unresolved_generation_failed.items()):
        remaining_rows.append({"stage": "generation", **row, "key": key})
    for key, row in sorted(unresolved_apply_failed.items()):
        remaining_rows.append({"stage": "apply", **row, "key": key})
    for key, row in sorted(not_applied.items()):
        remaining_rows.append({"stage": "generated_not_applied", **row, "key": key})

    write_jsonl(workspace / "remaining.jsonl", remaining_rows)
    write_jsonl(
        workspace / "updates.jsonl",
        [{"key": key, "fields": {"citationKey": row["citationKey"]}} for key, row in sorted(generated.items())],
    )

    known_total = len(generated) + len(unresolved_generation_failed)
    if selected_count is not None:
        known_total = max(known_total, selected_count)
    summary: dict[str, Any] = {
        "total_ai_note_items": known_total,
        "selected_items": selected_count,
        "generated": len(generated),
        "applied": len(applied),
        "remaining": len(remaining_rows),
        "generation_failed_history_total": len(generation_failed),
        "generation_failed_unresolved": len(unresolved_generation_failed),
        "apply_failed_history_total": len(apply_failed),
        "apply_failed_unresolved": len(unresolved_apply_failed),
        "not_applied": len(not_applied),
        "model": model,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (workspace / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def sanitize_citation_key(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = text.replace("`", "").replace("[", "").replace("]", "")
    text = re.sub(r"液态\s*Na[-–]K\s*合金负极", "Na-K", text, flags=re.IGNORECASE)
    text = re.sub(r"Na[-–]K\s*液态合金负极", "Na-K", text, flags=re.IGNORECASE)
    text = re.sub(r"液态\s*Na[-–]K\s*合金", "Na-K", text, flags=re.IGNORECASE)
    text = re.sub(r"Na[-–]K\s*液态合金", "Na-K", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\|\s*", " | ", text)
    parts = [part.strip() for part in text.split("|") if part.strip()]
    question_parts = [part for part in parts if part.startswith("疑问：")]
    if question_parts:
        parts = [part for part in parts if not part.startswith("疑问：")]
        parts.append(question_parts[0])
    text = " | ".join(parts)
    text = re.sub(r"\s+", " ", text).strip(" |")
    return text


def validate_citation_key(value: str) -> str | None:
    if not value:
        return "empty citationKey"
    if any(ch in value for ch in "[]`"):
        return "contains forbidden bracket/backtick"
    if value.endswith("|") or value.startswith("|"):
        return "leading/trailing pipe"
    parts = [part.strip() for part in value.split("|")]
    if any(not part for part in parts):
        return "empty pipe segment"
    if len(parts) < 3:
        return "fewer than 3 segments"
    if not parts[-1].startswith("疑问："):
        return "last segment must start with 疑问："
    if len(value) > 260:
        return "too long"
    return None


def is_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(token in message for token in ("402", "insufficient balance", "quota", "billing"))


def parse_ai_json(text: str) -> list[dict[str, Any]]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\[[\s\S]*\]", raw)
    if match:
        raw = match.group(0)
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("AI response is not a JSON array")
    return [row for row in data if isinstance(row, dict)]


def item_to_prompt_payload(item: ItemRecord, *, max_chars: int) -> dict[str, Any]:
    return {
        "key": item.key,
        "type": item.item_type,
        "title": item.title,
        "doi": item.doi,
        "currentCitationKey": item.current_citation_key,
        "tags": item.tags,
        "aiNoteContext": compact_context(item.note_text, max_chars=max_chars),
    }


def call_model(client: OpenAI, model: str, system_prompt: str, items: list[ItemRecord], *, max_chars: int) -> list[dict[str, Any]]:
    payload = [item_to_prompt_payload(item, max_chars=max_chars) for item in items]
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请为以下条目生成规范 citationKey。只输出 JSON 数组。\n"
                + json.dumps(payload, ensure_ascii=False),
            },
        ],
        temperature=0.1,
        max_tokens=3000,
    )
    content = response.choices[0].message.content or ""
    parsed = parse_ai_json(content)
    by_key = {str(row.get("key") or ""): row for row in parsed}
    result: list[dict[str, Any]] = []
    for item in items:
        row = by_key.get(item.key)
        if not row:
            raise ValueError(f"Missing result for {item.key}")
        citation_key = sanitize_citation_key(str(row.get("citationKey") or ""))
        error = validate_citation_key(citation_key)
        if error:
            raise ValueError(f"Invalid citationKey for {item.key}: {error}: {citation_key}")
        result.append(
            {
                "key": item.key,
                "title": item.title,
                "doi": item.doi,
                "old_citationKey": item.current_citation_key,
                "citationKey": citation_key,
                "reason": str(row.get("reason") or "")[:160],
            }
        )
    return result


def chunked(items: list[ItemRecord], size: int) -> list[list[ItemRecord]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def generate(args: argparse.Namespace) -> None:
    cfg = load_config(profile=args.profile)
    ai_cfg = load_ai_note_config()
    api_key = args.api_key or ai_cfg.api_key
    if not api_key:
        raise SystemExit("AI API key missing in .zot/config.toml [ai_notes].api_key")
    model = args.model or ai_cfg.model
    base_url = args.base_url or ai_cfg.base_url or None
    system_prompt = load_system_prompt(args.prompt_path)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout)

    db_path = get_data_dir(cfg) / "zotero.sqlite"
    all_items = read_ai_note_items(db_path)
    if args.skip_done_tag:
        all_items = [item for item in all_items if KEYWORD_DONE_TAG not in item.tags]
    if args.only_missing:
        all_items = [item for item in all_items if not item.current_citation_key.strip()]
    elif args.only_existing:
        all_items = [item for item in all_items if item.current_citation_key.strip()]
    if args.offset:
        all_items = all_items[args.offset :]
    if args.limit:
        all_items = all_items[: args.limit]

    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    items_jsonl = workspace / "items.jsonl"
    generated_jsonl = workspace / "generated.jsonl"
    failed_jsonl = workspace / "failed_generation.jsonl"
    updates_jsonl = workspace / "updates.jsonl"
    write_jsonl(
        items_jsonl,
        [
            {
                "key": item.key,
                "title": item.title,
                "doi": item.doi,
                "currentCitationKey": item.current_citation_key,
                "tags": item.tags,
                "noteKey": item.note_key,
            }
            for item in all_items
        ],
    )

    generated = load_jsonl_by_key(generated_jsonl)
    failed = load_jsonl_by_key(failed_jsonl)
    unresolved_failed = unresolved_rows(failed, generated)
    if args.retry_failed:
        pending = [item for item in all_items if item.key in unresolved_failed]
    else:
        pending = [item for item in all_items if args.force or (item.key not in generated and item.key not in failed)]
    print(
        json.dumps(
            {
                "phase": "generate_start",
                "workspace": str(workspace),
                "total_selected": len(all_items),
                "already_generated": len(generated),
                "already_failed": len(unresolved_failed),
                "failed_history_total": len(failed),
                "pending": len(pending),
                "model": model,
                "base_url": base_url,
                "prompt_path": str(args.prompt_path),
                "progress": f"0/{len(pending)}",
                "progress_label": f"generate 0/{len(pending)}",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    done_now = 0
    failed_now = 0
    attempted_now = 0
    total_pending = len(pending)
    pending_batches = (total_pending + args.batch_size - 1) // args.batch_size
    for batch_index, batch in enumerate(chunked(pending, args.batch_size), 1):
        try:
            rows = call_model(client, model, system_prompt, batch, max_chars=args.max_context_chars)
            for row in rows:
                append_jsonl(generated_jsonl, row)
                generated[row["key"]] = row
                done_now += 1
            attempted_now += len(batch)
        except Exception as exc:
            if is_quota_error(exc):
                print(
                    json.dumps(
                        {
                            "phase": "generate_quota_error",
                            "batch": batch_index,
                            "error": str(exc),
                            "progress": f"{attempted_now}/{total_pending}",
                            "progress_label": f"generate {attempted_now}/{total_pending}",
                            "message": "Stop without marking pending items as failed; rerun after balance/quota is restored.",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                raise SystemExit(75) from exc
            if len(batch) == 1:
                item = batch[0]
                append_jsonl(
                    failed_jsonl,
                    {"key": item.key, "title": item.title, "old_citationKey": item.current_citation_key, "error": str(exc)},
                )
                failed[item.key] = {"key": item.key, "error": str(exc)}
                attempted_now += 1
                failed_now += 1
            else:
                for item in batch:
                    try:
                        rows = call_model(client, model, system_prompt, [item], max_chars=args.max_context_chars)
                        for row in rows:
                            append_jsonl(generated_jsonl, row)
                            generated[row["key"]] = row
                            done_now += 1
                        attempted_now += 1
                    except Exception as single_exc:
                        if is_quota_error(single_exc):
                            print(
                                json.dumps(
                                    {
                                        "phase": "generate_quota_error",
                                        "batch": batch_index,
                                        "key": item.key,
                                        "error": str(single_exc),
                                        "progress": f"{attempted_now}/{total_pending}",
                                        "progress_label": f"generate {attempted_now}/{total_pending}",
                                        "message": "Stop without marking pending items as failed; rerun after balance/quota is restored.",
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            raise SystemExit(75) from single_exc
                        append_jsonl(
                            failed_jsonl,
                            {
                                "key": item.key,
                                "title": item.title,
                                "old_citationKey": item.current_citation_key,
                                "error": f"batch_error={exc}; single_error={single_exc}",
                            },
                        )
                        failed[item.key] = {"key": item.key, "error": str(single_exc)}
                        attempted_now += 1
                        failed_now += 1
        print(
            json.dumps(
                {
                    "phase": "generate_progress",
                    "batch": batch_index,
                    "pending_batches": pending_batches,
                    "progress": f"{attempted_now}/{total_pending}",
                    "progress_label": f"generate {attempted_now}/{total_pending}",
                    "done_now": done_now,
                    "failed_now": failed_now,
                    "attempted_now": attempted_now,
                    "generated_total": len(generated),
                    "failed_unresolved": len(unresolved_rows(failed, generated)),
                    "failed_history_total": len(failed),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.sleep:
            time.sleep(args.sleep)

    final_generated = load_jsonl_by_key(generated_jsonl)
    selected_keys = {item.key for item in all_items}
    update_rows = [
        {"key": key, "fields": {"citationKey": row["citationKey"]}}
        for key, row in sorted(final_generated.items())
        if key in selected_keys or key not in failed
    ]
    write_jsonl(updates_jsonl, update_rows)
    summary = write_status_files(workspace, selected_count=len(all_items), model=model)
    print(
        json.dumps(
            {
                "phase": "generate_complete",
                "generated": len(final_generated),
                "updates_path": str(updates_jsonl),
                "failed_path": str(failed_jsonl),
                "remaining": summary["remaining"],
                "progress": f"{attempted_now}/{total_pending}",
                "progress_label": f"generate {attempted_now}/{total_pending}",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def apply_updates(args: argparse.Namespace) -> None:
    cfg = load_config(profile=args.profile)
    library_id, api_key = resolve_write_credentials(cfg, library_type="user")
    if not library_id or not api_key:
        raise SystemExit("Zotero write credentials missing")
    workspace = args.workspace
    generated_jsonl = workspace / "generated.jsonl"
    applied_jsonl = workspace / "applied.jsonl"
    failed_jsonl = workspace / "failed_apply.jsonl"
    generated = load_jsonl_by_key(generated_jsonl)
    applied = load_jsonl_by_key(applied_jsonl)
    failed = load_jsonl_by_key(failed_jsonl)
    rows = [row for key, row in sorted(generated.items()) if args.force_apply or (key not in applied and key not in failed)]
    if args.limit:
        rows = rows[: args.limit]
    print(
        json.dumps(
            {
                "phase": "apply_start",
                "workspace": str(workspace),
                "pending": len(rows),
                "already_applied": len(applied),
                "already_failed": len(failed),
                "dry_run": args.dry_run,
                "progress": f"0/{len(rows)}",
                "progress_label": f"apply 0/{len(rows)}",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.dry_run:
        for row in rows[:10]:
            print(json.dumps({"key": row["key"], "citationKey": row["citationKey"], "addTag": KEYWORD_DONE_TAG}, ensure_ascii=False))
        summary = write_status_files(workspace)
        print(json.dumps({"phase": "status", **summary}, ensure_ascii=False), flush=True)
        return

    writer = ZoteroWriter(library_id=library_id, api_key=api_key, library_type="user", timeout=args.zotero_timeout)
    for index, row in enumerate(rows, 1):
        key = row["key"]
        citation_key = row["citationKey"]
        try:
            writer.update_item(key, {"citationKey": citation_key})
            writer.add_tags(key, [KEYWORD_DONE_TAG])
            append_jsonl(applied_jsonl, {"key": key, "citationKey": citation_key, "tag": KEYWORD_DONE_TAG})
            print(
                json.dumps(
                    {
                        "phase": "apply_progress",
                        "progress": f"{index}/{len(rows)}",
                        "progress_label": f"apply {index}/{len(rows)}",
                        "done": index,
                        "total": len(rows),
                        "key": key,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (ZoteroWriteError, PyZoteroError, Exception) as exc:
            append_jsonl(failed_jsonl, {"key": key, "citationKey": citation_key, "error": str(exc)})
            print(
                json.dumps(
                    {
                        "phase": "apply_failed",
                        "progress": f"{index}/{len(rows)}",
                        "progress_label": f"apply {index}/{len(rows)}",
                        "done": index,
                        "total": len(rows),
                        "key": key,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if args.sleep:
            time.sleep(args.sleep)
    summary = write_status_files(workspace)
    print(
        json.dumps(
            {
                "phase": "apply_complete",
                "attempted": len(rows),
                "remaining": summary["remaining"],
                "progress": f"{len(rows)}/{len(rows)}",
                "progress_label": f"apply {len(rows)}/{len(rows)}",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def status(args: argparse.Namespace) -> None:
    summary = write_status_files(args.workspace)
    print(json.dumps({"phase": "status", **summary}, ensure_ascii=False), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and apply citationKey keywords from workflow/ai_note notes.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=DEFAULT_WORKSPACE,
        help="Directory for resumable JSONL/log files. Default: log/ai-note-keyword-update.",
    )
    parser.add_argument(
        "--prompt-path",
        type=Path,
        default=DEFAULT_PROMPT_PATH,
        help="JSON prompt file. Default: scripts/update_ai_note_keywords_prompt.json.",
    )
    parser.add_argument("--profile", default=None)

    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate")
    gen.add_argument("--limit", type=int, default=0)
    gen.add_argument("--offset", type=int, default=0)
    gen.add_argument("--batch-size", type=int, default=4)
    gen.add_argument("--max-context-chars", type=int, default=12000)
    gen.add_argument("--timeout", type=float, default=180.0)
    gen.add_argument("--sleep", type=float, default=0.0)
    gen.add_argument("--model", default=None)
    gen.add_argument("--base-url", default=None)
    gen.add_argument("--api-key", default=None)
    gen.add_argument("--force", action="store_true")
    gen.add_argument("--retry-failed", action="store_true", help="Retry only unresolved keys in failed_generation.jsonl.")
    gen.add_argument("--skip-done-tag", action="store_true")
    group = gen.add_mutually_exclusive_group()
    group.add_argument("--only-existing", action="store_true")
    group.add_argument("--only-missing", action="store_true")
    gen.set_defaults(func=generate)

    app = sub.add_parser("apply")
    app.add_argument("--limit", type=int, default=0)
    app.add_argument("--dry-run", action="store_true")
    app.add_argument("--force-apply", action="store_true")
    app.add_argument("--sleep", type=float, default=0.0)
    app.add_argument("--zotero-timeout", type=float, default=60.0)
    app.set_defaults(func=apply_updates)

    stat = sub.add_parser("status")
    stat.set_defaults(func=status)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

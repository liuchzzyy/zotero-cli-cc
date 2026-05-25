from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

from zotero_cli_agents.config import (
    get_data_dir,
    get_prefs_js_path,
    load_config,
    resolve_library_id,
)
from zotero_cli_agents.core.pdf_extractor import MinerUExtractor, get_extractor
from zotero_cli_agents.core.rag import convert_pdfs_to_text
from zotero_cli_agents.core.reader import ZoteroReader
from zotero_cli_agents.core.writer import SYNC_REMINDER, ZoteroWriter
from zotero_cli_agents.models import Attachment, Creator, Item


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(Path(__file__).resolve().parents[1] / ".env")

DONE_TAG = "update/AInote"
NOTE_TITLE_PREFIX = "AI条目分析 - "
DEFAULT_OUTPUT_DIR = Path(".workspace") / "ai-note-analysis"
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "note-templates"
DEFAULT_MODEL = os.environ.get("ZOT_AI_NOTE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.5"
DEFAULT_REASONING_EFFORT = os.environ.get("ZOT_AI_NOTE_REASONING_EFFORT", "")
MAX_PDF_BYTES_DEFAULT = 100 * 1024 * 1024
BOOK_TYPES = {"book", "bookSection"}
CLASSIFICATION_TYPES = {"research_article", "review_article", "uncertain"}


@dataclass
class PdfRef:
    key: str
    filename: str
    path: Path
    size_bytes: int


@dataclass
class Candidate:
    item: Item
    pdfs: list[PdfRef] = field(default_factory=list)


@dataclass
class DocumentInput:
    text: str
    image_paths: list[Path] = field(default_factory=list)
    image_count_total: int = 0
    image_count_used: int = 0
    truncated: bool = False


@dataclass
class Progress:
    total: int = 0
    scanned: int = 0
    skipped_tag: int = 0
    skipped_checkpoint: int = 0
    skipped_type: int = 0
    skipped_no_pdf: int = 0
    skipped_missing_pdf: int = 0
    skipped_oversize: int = 0
    skipped_uncertain: int = 0
    classified_research: int = 0
    classified_review: int = 0
    analyzed: int = 0
    tagged: int = 0
    failed: int = 0

    def line(self) -> str:
        return (
            f"scanned={self.scanned}/{self.total} tagged={self.tagged} analyzed={self.analyzed} "
            f"research={self.classified_research} review={self.classified_review} "
            f"skip_tag={self.skipped_tag} skip_ckpt={self.skipped_checkpoint} "
            f"no_pdf={self.skipped_no_pdf} missing_pdf={self.skipped_missing_pdf} "
            f"oversize={self.skipped_oversize} uncertain={self.skipped_uncertain} failed={self.failed}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def print_progress(stage: str, message: str, progress: Progress | None = None) -> None:
    if progress is None:
        print(f"[{stage}] {message}", flush=True)
    else:
        print(f"[{stage}] {message} | {progress.line()}", flush=True)


def parse_library(library: str) -> dict[str, Any]:
    if library == "user":
        return {"library_type": "user", "group_id": None}
    if library.startswith("group:") and library[6:].isdigit():
        return {"library_type": "group", "group_id": int(library[6:])}
    raise ValueError("Invalid --library. Use 'user' or 'group:<id>'.")


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": {}, "created_at": utc_now()}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": {}, "created_at": utc_now(), "warning": "previous checkpoint was invalid JSON"}
    if not isinstance(payload, dict):
        return {"items": {}, "created_at": utc_now(), "warning": "previous checkpoint was not an object"}
    if not isinstance(payload.get("items"), dict):
        payload["items"] = {}
    return payload


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def update_checkpoint(
    checkpoint: dict[str, Any],
    path: Path,
    key: str,
    *,
    status: str,
    data: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    entry = checkpoint.setdefault("items", {}).setdefault(key, {})
    entry.update({"key": key, "status": status, "updated_at": utc_now()})
    if data:
        entry.update(data)
    if error:
        entry["error"] = error
    checkpoint["updated_at"] = entry["updated_at"]
    write_json_atomic(path, checkpoint)


def checkpoint_status(checkpoint: dict[str, Any], key: str) -> str:
    entry = checkpoint.get("items", {}).get(key, {})
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("status") or "")


def load_template(template_dir: Path, name: str) -> str:
    path = template_dir / name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path.read_text(encoding="utf-8")


def creators_to_text(creators: list[Creator], limit: int = 12) -> str:
    names = [creator.full_name for creator in creators if creator.full_name]
    if not names:
        return ""
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", et al. ({len(names)} total)"


def item_metadata_text(item: Item) -> str:
    extra_lines = []
    for key, value in sorted(item.extra.items()):
        if value:
            extra_lines.append(f"- {key}: {value}")
    return "\n".join(
        [
            "## Zotero metadata",
            f"- Key: {item.key}",
            f"- Item type: {item.item_type}",
            f"- Title: {item.title}",
            f"- Authors: {creators_to_text(item.creators) or '未知'}",
            f"- Date: {item.date or '未知'}",
            f"- DOI: {item.doi or '未知'}",
            f"- URL: {item.url or '未知'}",
            f"- Journal/Publication: {item.extra.get('publicationTitle') or item.extra.get('journalAbbreviation') or '未知'}",
            f"- Tags: {', '.join(item.tags) if item.tags else '无'}",
            "",
            "## Extra fields",
            "\n".join(extra_lines) if extra_lines else "- 无",
        ]
    )


def markdown_to_zotero_html(markdown: str) -> str:
    lines = markdown.strip().splitlines()
    out: list[str] = [
        '<div class="zotero-note znv1">',
        (
            '<div style="max-width: 860px; font-size: 1rem; color: black; line-height: 1.6; '
            'word-spacing: 0; letter-spacing: 0; font-family: Optima-Regular, Optima, '
            'PingFangSC-light, PingFangTC-light, &quot;PingFang SC&quot;, Cambria, Cochin, '
            'Georgia, Times, &quot;Times New Roman&quot;, serif; padding: 10px;">'
        ),
    ]
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not in_table:
            return
        close_ul()
        if table_rows:
            out.append('<table style="border-collapse: collapse; width: 100%; margin: 0.8em 0;">')
            for row_idx, row in enumerate(table_rows):
                tag = "th" if row_idx == 0 else "td"
                style = (
                    "border: 1px solid #ddd; padding: 6px 8px; text-align: left; "
                    "vertical-align: top; color: black;"
                )
                out.append("<tr>" + "".join(f'<{tag} style="{style}">{html.escape(cell.strip())}</{tag}>' for cell in row) + "</tr>")
            out.append("</table>")
        table_rows = []
        in_table = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_table()
            close_ul()
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
                continue
            in_table = True
            table_rows.append(cells)
            continue
        flush_table()
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            close_ul()
            level = min(len(heading.group(1)), 4)
            text = html.escape(heading.group(2))
            margin = "1em 0 0.6em" if level <= 2 else "0.8em 0 0.4em"
            out.append(
                f'<h{level} style="font-size: {1.55 - level * 0.1:.2f}rem; margin: {margin}; '
                f'font-weight: bold; color: black;">{text}</h{level}>'
            )
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            if not in_ul:
                out.append('<ul style="margin: 0.4em 0 0.8em 1.2em; padding-left: 1em;">')
                in_ul = True
            out.append(f'<li style="margin: 0.25em 0;">{html.escape(bullet.group(1))}</li>')
            continue
        close_ul()
        out.append(f'<p style="margin: 0.5em 0; line-height: 1.6; color: black;">{html.escape(stripped)}</p>')
    flush_table()
    close_ul()
    out.append("</div></div>")
    return "\n".join(out)


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    else:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match:
            stripped = match.group(0)
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("classification response was not a JSON object")
    return payload


def get_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(str(text))
    if chunks:
        return "\n".join(chunks).strip()
    return str(response)


def build_openai_client(api_key_env: str, base_url: str | None, timeout: float) -> OpenAI:
    api_key = os.environ.get(api_key_env) or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"OpenAI API key missing. Set {api_key_env} or OPENAI_API_KEY.")
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": httpx.Timeout(timeout)}
    resolved_base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    if resolved_base_url:
        kwargs["base_url"] = resolved_base_url
    return OpenAI(**kwargs)


def is_deepseek_model(model: str, base_url: str | None = None) -> bool:
    model_lower = model.lower()
    base_url_lower = (base_url or os.environ.get("OPENAI_BASE_URL") or "").lower()
    return "deepseek" in model_lower or "deepseek" in base_url_lower


def chat_reasoning_effort(model: str, base_url: str | None, reasoning_effort: str) -> str:
    if not reasoning_effort:
        return ""
    if is_deepseek_model(model, base_url):
        return reasoning_effort
    return "xhigh" if reasoning_effort == "max" else reasoning_effort


def upload_pdfs(client: OpenAI, pdfs: list[PdfRef]) -> list[str]:
    file_ids: list[str] = []
    for pdf in pdfs:
        with pdf.path.open("rb") as handle:
            uploaded = client.files.create(file=handle, purpose="user_data")
        file_ids.append(uploaded.id)
    return file_ids


def delete_uploaded_files(client: OpenAI, file_ids: list[str]) -> None:
    for file_id in file_ids:
        try:
            client.files.delete(file_id)
        except Exception:
            pass


def create_response(
    client: OpenAI,
    *,
    model: str,
    prompt: str,
    file_ids: list[str],
    max_output_tokens: int,
    reasoning_effort: str,
) -> str:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for file_id in file_ids:
        content.append({"type": "input_file", "file_id": file_id})
    kwargs: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": max_output_tokens,
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}
    response = client.responses.create(**kwargs)
    return get_response_text(response)


def image_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def create_chat_completion(
    client: OpenAI,
    *,
    model: str,
    prompt: str,
    document_input: DocumentInput,
    max_output_tokens: int,
    reasoning_effort: str,
    base_url: str | None,
    chat_token_param: str,
) -> str:
    if document_input.image_paths and is_deepseek_model(model, base_url):
        raise RuntimeError("DeepSeek chat/reasoner does not accept image_url content; use mineru-text or a vision-capable model.")

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if document_input.text.strip():
        content.append({"type": "text", "text": document_input.text})
    for image_path in document_input.image_paths:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}})

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    token_param = chat_token_param
    if token_param == "auto":
        token_param = "max_tokens" if is_deepseek_model(model, base_url) else "max_completion_tokens"
    kwargs[token_param] = max_output_tokens

    resolved_reasoning_effort = chat_reasoning_effort(model, base_url, reasoning_effort)
    if resolved_reasoning_effort:
        if is_deepseek_model(model, base_url):
            kwargs["extra_body"] = {"thinking": {"type": "enabled", "reasoning_effort": resolved_reasoning_effort}}
        else:
            kwargs["reasoning_effort"] = resolved_reasoning_effort

    response = client.chat.completions.create(**kwargs)
    message = response.choices[0].message
    text = message.content
    if not text:
        return str(response)
    if isinstance(text, str):
        return text.strip()
    return str(text).strip()


def create_ai_text(
    client: OpenAI,
    *,
    model: str,
    prompt: str,
    file_ids: list[str],
    document_input: DocumentInput | None,
    max_output_tokens: int,
    reasoning_effort: str,
    api_mode: str,
    base_url: str | None,
    chat_token_param: str,
) -> str:
    if api_mode == "responses":
        return create_response(
            client,
            model=model,
            prompt=prompt,
            file_ids=file_ids,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
    if document_input is None:
        raise RuntimeError("chat API mode requires extracted document text or images")
    return create_chat_completion(
        client,
        model=model,
        prompt=prompt,
        document_input=document_input,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        base_url=base_url,
        chat_token_param=chat_token_param,
    )


def classify_paper(
    client: OpenAI,
    *,
    model: str,
    template: str,
    item: Item,
    file_ids: list[str],
    document_input: DocumentInput | None,
    max_output_tokens: int,
    reasoning_effort: str,
    api_mode: str,
    base_url: str | None,
    chat_token_param: str,
) -> dict[str, Any]:
    prompt = template + "\n\n" + item_metadata_text(item)
    text = create_ai_text(
        client,
        model=model,
        prompt=prompt,
        file_ids=file_ids,
        document_input=document_input,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        api_mode=api_mode,
        base_url=base_url,
        chat_token_param=chat_token_param,
    )
    payload = extract_json_object(text)
    paper_type = str(payload.get("paper_type") or "").strip()
    if paper_type not in CLASSIFICATION_TYPES:
        raise ValueError(f"invalid paper_type from classifier: {paper_type!r}")
    return payload


def analyze_paper(
    client: OpenAI,
    *,
    model: str,
    template: str,
    item: Item,
    file_ids: list[str],
    document_input: DocumentInput | None,
    max_output_tokens: int,
    reasoning_effort: str,
    api_mode: str,
    base_url: str | None,
    chat_token_param: str,
) -> str:
    prompt = template.format(title=item.title or item.key) + "\n\n" + item_metadata_text(item)
    text = create_ai_text(
        client,
        model=model,
        prompt=prompt,
        file_ids=file_ids,
        document_input=document_input,
        max_output_tokens=max_output_tokens,
        reasoning_effort=reasoning_effort,
        api_mode=api_mode,
        base_url=base_url,
        chat_token_param=chat_token_param,
    )
    if not text.strip():
        raise ValueError("AI returned an empty analysis")
    return text.strip()


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    marker = f"\n\n[TRUNCATED: input was {len(text)} characters; retained first {max_chars} characters.]\n"
    return text[:max_chars] + marker, True


def format_pdf_markdown_sections(sections: list[tuple[PdfRef, str]], max_chars: int) -> tuple[str, bool]:
    parts: list[str] = ["## Extracted PDF content", "The following content was extracted locally from Zotero PDF attachments."]
    for idx, (pdf, markdown) in enumerate(sections, 1):
        parts.extend(
            [
                "",
                f"### PDF {idx}: {pdf.filename}",
                f"- Zotero attachment key: {pdf.key}",
                f"- Local path: {pdf.path}",
                "",
                markdown.strip(),
            ]
        )
    return truncate_text("\n".join(parts), max_chars)


def mineru_progress_callback(pdf_name: str) -> Any:
    def callback(phase: str, current: int, total: int, pages: int) -> None:
        page_text = f" pages={pages}" if pages else ""
        print(f"[mineru] {pdf_name} {phase} {current}/{total}{page_text}", flush=True)

    return callback


def prepare_mineru_text_input(candidate: Candidate, *, max_extracted_chars: int) -> DocumentInput:
    paths = [pdf.path for pdf in candidate.pdfs]
    results = convert_pdfs_to_text(paths, "mineru", mineru_progress_callback(candidate.item.key))
    sections: list[tuple[PdfRef, str]] = []
    for pdf in candidate.pdfs:
        extracted = results.get(pdf.path)
        if isinstance(extracted, Exception):
            raise extracted
        if not isinstance(extracted, str) or not extracted.strip():
            raise RuntimeError(f"MinerU returned empty text for {pdf.path}")
        sections.append((pdf, extracted))
    text, truncated = format_pdf_markdown_sections(sections, max_extracted_chars)
    return DocumentInput(text=text, truncated=truncated)


def mineru_asset_cache_dir(output_dir: Path, pdf: PdfRef) -> Path:
    fingerprint = sha1(
        f"{pdf.path}|{pdf.size_bytes}|{pdf.path.stat().st_mtime}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf.key or pdf.path.stem)
    return output_dir / "mineru-assets" / f"{safe_key}-{fingerprint}"


def load_cached_mineru_assets(cache_dir: Path) -> tuple[str, list[Path]] | None:
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    markdown_path = cache_dir / str(manifest.get("markdown_path", "full.raw.md"))
    if not markdown_path.exists():
        return None
    image_paths = [cache_dir / str(path) for path in manifest.get("image_paths", [])]
    image_paths = [path for path in image_paths if path.exists()]
    return markdown_path.read_text(encoding="utf-8"), image_paths


def write_mineru_asset_manifest(cache_dir: Path, markdown: str, image_paths: list[Path]) -> None:
    markdown_path = cache_dir / "full.raw.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    manifest = {
        "created_at": utc_now(),
        "markdown_path": markdown_path.relative_to(cache_dir).as_posix(),
        "image_paths": [path.relative_to(cache_dir).as_posix() for path in image_paths if path.exists()],
    }
    write_json_atomic(cache_dir / "manifest.json", manifest)


def prepare_mineru_markdown_images_input(
    candidate: Candidate,
    *,
    output_dir: Path,
    max_extracted_chars: int,
    max_images: int,
    max_image_bytes: int,
    refresh_mineru_cache: bool,
) -> DocumentInput:
    extractor = get_extractor("mineru")
    if not isinstance(extractor, MinerUExtractor):
        raise RuntimeError("Configured mineru extractor is not available")

    sections: list[tuple[PdfRef, str]] = []
    all_images: list[Path] = []
    for pdf in candidate.pdfs:
        cache_dir = mineru_asset_cache_dir(output_dir, pdf)
        cached = None if refresh_mineru_cache else load_cached_mineru_assets(cache_dir)
        if cached is None:
            print(f"[mineru] {candidate.item.key} extracting assets from {pdf.filename}", flush=True)
            extracted = extractor.extract_markdown_assets(
                pdf.path,
                cache_dir,
                progress_callback=mineru_progress_callback(pdf.filename),
            )
            write_mineru_asset_manifest(cache_dir, extracted.markdown, extracted.image_paths)
            markdown = extracted.markdown
            image_paths = extracted.image_paths
        else:
            print(f"[mineru] {candidate.item.key} using cached assets for {pdf.filename}", flush=True)
            markdown, image_paths = cached
        sections.append((pdf, markdown))
        all_images.extend(image_paths)

    usable_images = [path for path in all_images if path.exists() and path.stat().st_size <= max_image_bytes]
    selected_images = usable_images[:max_images]
    text, truncated = format_pdf_markdown_sections(sections, max_extracted_chars)
    image_lines = [
        "",
        "## Attached extracted images",
        f"- MinerU extracted images found: {len(all_images)}",
        f"- Images attached to this AI request: {len(selected_images)}",
        f"- Images skipped by count or size: {len(all_images) - len(selected_images)}",
    ]
    for idx, image_path in enumerate(selected_images, 1):
        image_lines.append(f"- Image {idx}: {image_path.name}")
    return DocumentInput(
        text=text + "\n".join(image_lines),
        image_paths=selected_images,
        image_count_total=len(all_images),
        image_count_used=len(selected_images),
        truncated=truncated,
    )


def prepare_document_input(candidate: Candidate, args: argparse.Namespace) -> DocumentInput | None:
    if args.pdf_input_mode == "openai-file":
        return None
    if args.pdf_input_mode == "mineru-text":
        return prepare_mineru_text_input(candidate, max_extracted_chars=args.max_extracted_chars)
    if args.pdf_input_mode == "mineru-markdown-images":
        return prepare_mineru_markdown_images_input(
            candidate,
            output_dir=args.output_dir,
            max_extracted_chars=args.max_extracted_chars,
            max_images=args.max_images,
            max_image_bytes=args.max_image_mb * 1024 * 1024,
            refresh_mineru_cache=args.refresh_mineru_cache,
        )
    raise RuntimeError(f"Unsupported pdf input mode: {args.pdf_input_mode}")


def candidate_items(reader: ZoteroReader, *, keys: list[str], collection: str | None, scan_limit: int | None) -> list[Item]:
    if keys:
        items: list[Item] = []
        for key in keys:
            item = reader.get_item(key)
            if item:
                items.append(item)
        return items[:scan_limit] if scan_limit is not None else items
    search_limit = scan_limit if scan_limit is not None else 100_000
    return reader.search("", collection=collection, limit=search_limit).items


def resolve_pdfs(
    reader: ZoteroReader,
    item: Item,
    *,
    max_pdf_bytes: int,
    max_files: int,
) -> tuple[list[PdfRef], str | None]:
    attachments = reader.get_pdf_attachments(item.key)
    if not attachments:
        return [], "skipped_no_pdf"
    pdfs: list[PdfRef] = []
    missing: list[Attachment] = []
    oversize: list[PdfRef] = []
    for attachment in attachments[:max_files]:
        if attachment.path is None or not attachment.path.exists():
            missing.append(attachment)
            continue
        size = attachment.path.stat().st_size
        ref = PdfRef(key=attachment.key, filename=attachment.filename, path=attachment.path, size_bytes=size)
        if size > max_pdf_bytes:
            oversize.append(ref)
            continue
        pdfs.append(ref)
    if not pdfs and missing:
        return [], "skipped_pdf_missing"
    if not pdfs and oversize:
        return [], "skipped_pdf_oversize"
    return pdfs, None


def prepare_candidates(
    reader: ZoteroReader,
    items: list[Item],
    *,
    force: bool,
    max_pdf_bytes: int,
    max_files: int,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    progress: Progress,
    ready_limit: int | None,
) -> list[Candidate]:
    prepared: list[Candidate] = []
    progress.total = len(items)
    for item in items:
        if ready_limit is not None and len(prepared) >= ready_limit:
            print_progress("scan", f"ready limit reached ({ready_limit})", progress)
            break
        progress.scanned += 1
        if item.item_type in BOOK_TYPES:
            progress.skipped_type += 1
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="skipped_book", data={"title": item.title})
            print_progress("scan", f"skip book {item.key}", progress)
            continue
        if not force and checkpoint_status(checkpoint, item.key) == "tagged":
            progress.skipped_checkpoint += 1
            print_progress("scan", f"skip checkpoint-tagged {item.key}", progress)
            continue
        if DONE_TAG in item.tags and not force:
            progress.skipped_tag += 1
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="skipped_done_tag", data={"title": item.title})
            print_progress("scan", f"skip tagged {item.key}", progress)
            continue
        pdfs, skip_status = resolve_pdfs(reader, item, max_pdf_bytes=max_pdf_bytes, max_files=max_files)
        if skip_status:
            if skip_status == "skipped_no_pdf":
                progress.skipped_no_pdf += 1
            elif skip_status == "skipped_pdf_missing":
                progress.skipped_missing_pdf += 1
            else:
                progress.skipped_oversize += 1
            update_checkpoint(checkpoint, checkpoint_path, item.key, status=skip_status, data={"title": item.title})
            print_progress("pdf_check", f"{skip_status} {item.key}", progress)
            continue
        prepared.append(Candidate(item=item, pdfs=pdfs))
        update_checkpoint(
            checkpoint,
            checkpoint_path,
            item.key,
            status="pending",
            data={"title": item.title, "pdfs": [asdict(pdf) | {"path": str(pdf.path)} for pdf in pdfs]},
        )
        print_progress("pdf_check", f"ready {item.key} pdfs={len(pdfs)}", progress)
    return prepared


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir
    checkpoint_path = args.checkpoint or output_dir / "checkpoint.json"
    checkpoint = load_checkpoint(checkpoint_path)
    template_dir = args.template_dir
    classify_template = load_template(template_dir, "classify-paper.md")
    research_template = load_template(template_dir, "research-article.md")
    review_template = load_template(template_dir, "review-article.md")

    library_ctx = parse_library(args.library)
    cfg = load_config(profile=args.profile)
    data_dir = get_data_dir(cfg)
    db_path = data_dir / "zotero.sqlite"
    library_id = resolve_library_id(db_path, library_ctx)
    reader = ZoteroReader(db_path, library_id=library_id, prefs_js_path=get_prefs_js_path(cfg))
    progress = Progress()

    try:
        print_progress("scan", f"reading local Zotero DB {db_path}")
        items = candidate_items(reader, keys=args.keys, collection=args.collection, scan_limit=args.scan_limit)
        candidates = prepare_candidates(
            reader,
            items,
            force=args.force,
            max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
            max_files=args.max_files,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            progress=progress,
            ready_limit=args.limit,
        )
    finally:
        reader.close()

    preview = {
        "apply": args.apply,
        "model": args.model,
        "pdf_input_mode": args.pdf_input_mode,
        "api_mode": args.api_mode,
        "candidate_items": len(candidates),
        "progress": asdict(progress),
        "checkpoint": str(checkpoint_path),
        "template_dir": str(template_dir),
    }
    write_json_atomic(output_dir / "preview.json", preview)
    if not args.apply:
        print_progress("dry_run", f"prepared {len(candidates)} item(s); no AI calls or Zotero writes were made", progress)
        return preview

    resolved_base_url = args.base_url or os.environ.get("OPENAI_BASE_URL")
    client = build_openai_client(args.api_key_env, args.base_url, args.openai_timeout)
    writer_library_id: str | int | None = os.environ.get("ZOT_LIBRARY_ID", cfg.library_id)
    if library_ctx["library_type"] == "group" and library_ctx.get("group_id"):
        writer_library_id = library_ctx["group_id"]
    api_key = os.environ.get("ZOT_API_KEY", cfg.api_key)
    if not writer_library_id or not api_key:
        raise RuntimeError("Zotero write credentials are missing. Run 'zot config init' or export ZOT_API_KEY / ZOT_LIBRARY_ID.")
    writer = ZoteroWriter(str(writer_library_id), api_key, library_type=library_ctx["library_type"])

    results = load_json_list(output_dir / "results.json")
    failures = load_json_list(output_dir / "failures.json")
    batch_results = 0
    batch_failures = 0
    for index, candidate in enumerate(candidates, 1):
        item = candidate.item
        uploaded_file_ids: list[str] = []
        try:
            api_mode = args.api_mode
            if api_mode == "auto":
                api_mode = "responses" if args.pdf_input_mode == "openai-file" else "chat"

            document_input = prepare_document_input(candidate, args)
            if args.pdf_input_mode == "openai-file":
                print_progress("upload", f"{index}/{len(candidates)} {item.key} uploading {len(candidate.pdfs)} pdf(s)", progress)
                update_checkpoint(checkpoint, checkpoint_path, item.key, status="uploading", data={"title": item.title})
                uploaded_file_ids = upload_pdfs(client, candidate.pdfs)
            else:
                update_checkpoint(
                    checkpoint,
                    checkpoint_path,
                    item.key,
                    status="extracted",
                    data={
                        "title": item.title,
                        "pdf_input_mode": args.pdf_input_mode,
                        "text_chars": len(document_input.text) if document_input else 0,
                        "image_count_used": document_input.image_count_used if document_input else 0,
                        "image_count_total": document_input.image_count_total if document_input else 0,
                        "truncated": document_input.truncated if document_input else False,
                    },
                )

            print_progress("classify", f"{item.key} classifying", progress)
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="classifying")
            classification = classify_paper(
                client,
                model=args.model,
                template=classify_template,
                item=item,
                file_ids=uploaded_file_ids,
                document_input=document_input,
                max_output_tokens=args.classify_max_tokens,
                reasoning_effort=args.reasoning_effort,
                api_mode=api_mode,
                base_url=resolved_base_url,
                chat_token_param=args.chat_token_param,
            )
            paper_type = str(classification["paper_type"])
            update_checkpoint(checkpoint, checkpoint_path, item.key, status=f"classified_{paper_type}", data=classification)
            if paper_type == "review_article":
                progress.classified_review += 1
                template = review_template
            elif paper_type == "research_article":
                progress.classified_research += 1
                template = research_template
            else:
                progress.skipped_uncertain += 1
                update_checkpoint(checkpoint, checkpoint_path, item.key, status="skipped_uncertain", data=classification)
                print_progress("classify", f"{item.key} uncertain; skipped", progress)
                continue

            print_progress("analyze", f"{item.key} using {paper_type}", progress)
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="analyzing", data=classification)
            markdown = analyze_paper(
                client,
                model=args.model,
                template=template,
                item=item,
                file_ids=uploaded_file_ids,
                document_input=document_input,
                max_output_tokens=args.analysis_max_tokens,
                reasoning_effort=args.reasoning_effort,
                api_mode=api_mode,
                base_url=resolved_base_url,
                chat_token_param=args.chat_token_param,
            )
            progress.analyzed += 1
            note_html = markdown_to_zotero_html(markdown)
            note_md_path = output_dir / "notes" / f"{item.key}.md"
            note_html_path = output_dir / "notes" / f"{item.key}.html"
            write_text_atomic(note_md_path, markdown)
            write_text_atomic(note_html_path, note_html)

            print_progress("note", f"{item.key} writing note", progress)
            note_key = writer.add_note(item.key, note_html)
            update_checkpoint(
                checkpoint,
                checkpoint_path,
                item.key,
                status="note_written",
                data={"paper_type": paper_type, "note_key": note_key},
            )

            print_progress("tag", f"{item.key} adding {DONE_TAG}", progress)
            writer.add_tags(item.key, [DONE_TAG])
            progress.tagged += 1
            row = {
                "key": item.key,
                "title": item.title,
                "paper_type": paper_type,
                "note_key": note_key,
                "pdf_count": len(candidate.pdfs),
                "pdf_input_mode": args.pdf_input_mode,
                "image_count_used": document_input.image_count_used if document_input else 0,
                "note_markdown_path": str(note_md_path),
                "note_html_path": str(note_html_path),
            }
            results.append(row)
            batch_results += 1
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="tagged", data=row)
            write_json_atomic(output_dir / "results.json", results)
            print_progress("done", f"{item.key} complete", progress)
        except Exception as exc:
            progress.failed += 1
            batch_failures += 1
            failure = {"key": item.key, "title": item.title, "error": str(exc)}
            failures.append(failure)
            update_checkpoint(checkpoint, checkpoint_path, item.key, status="failed", error=str(exc), data={"title": item.title})
            write_json_atomic(output_dir / "failures.json", failures)
            print_progress("failed", f"{item.key}: {exc}", progress)
            if args.stop_on_error:
                raise
        finally:
            if uploaded_file_ids and not args.keep_uploaded_files:
                delete_uploaded_files(client, uploaded_file_ids)

    summary = {
        "apply": True,
        "model": args.model,
        "pdf_input_mode": args.pdf_input_mode,
        "results": len(results),
        "batch_results": batch_results,
        "failures": len(failures),
        "batch_failures": batch_failures,
        "progress": asdict(progress),
        "checkpoint": str(checkpoint_path),
        "sync_reminder": SYNC_REMINDER if results else "",
    }
    write_json_atomic(output_dir / "summary.json", summary)
    print_progress("summary", json.dumps(summary, ensure_ascii=False), progress)
    return summary


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[1]
    default_pdf_input_mode = os.environ.get("ZOT_AI_NOTE_PDF_INPUT_MODE")
    if default_pdf_input_mode is None:
        default_pdf_input_mode = "mineru-text" if is_deepseek_model(DEFAULT_MODEL) else "mineru-markdown-images"
    parser = argparse.ArgumentParser(
        description="Generate AI analysis Zotero notes from all local PDF attachments and tag completed parent items."
    )
    parser.add_argument("--apply", action="store_true", help="Call AI and write Zotero notes/tags. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help=f"Process items even if they already have {DONE_TAG}.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of ready-to-analyze items to prepare.")
    parser.add_argument("--scan-limit", type=int, default=None, help="Maximum number of Zotero items to scan before filtering.")
    parser.add_argument("--keys", nargs="*", default=[], help="Specific Zotero parent item keys to process.")
    parser.add_argument("--collection", default=None, help="Collection key or name to scan.")
    parser.add_argument("--library", default="user", help="Library: 'user' or 'group:<id>'.")
    parser.add_argument("--profile", default=None, help="Optional zot profile name.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI-compatible model for classification and analysis.")
    parser.add_argument(
        "--pdf-input-mode",
        choices=["openai-file", "mineru-text", "mineru-markdown-images"],
        default=default_pdf_input_mode,
        help="How PDF attachments are supplied to AI. Use mineru-markdown-images for CLIProxyAPI vision models.",
    )
    parser.add_argument(
        "--api-mode",
        choices=["auto", "responses", "chat"],
        default=os.environ.get("ZOT_AI_NOTE_API_MODE", "auto"),
        help="AI API surface. auto uses Responses for openai-file and Chat Completions for MinerU modes.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help="Optional reasoning effort forwarded to the AI API, e.g. max/high/medium.",
    )
    parser.add_argument("--base-url", default=None, help="Optional OpenAI-compatible API base URL.")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Environment variable containing the AI API key.")
    parser.add_argument("--openai-timeout", type=float, default=600.0, help="OpenAI request timeout in seconds.")
    parser.add_argument(
        "--chat-token-param",
        choices=["auto", "max_completion_tokens", "max_tokens"],
        default=os.environ.get("ZOT_AI_NOTE_CHAT_TOKEN_PARAM", "auto"),
        help="Token parameter for Chat Completions. auto uses max_tokens for DeepSeek and max_completion_tokens otherwise.",
    )
    parser.add_argument("--classify-max-tokens", type=int, default=800, help="Max output tokens for classification.")
    parser.add_argument("--analysis-max-tokens", type=int, default=8192, help="Max output tokens for final note analysis.")
    parser.add_argument("--max-files", type=int, default=8, help="Maximum PDF attachments sent for one item.")
    parser.add_argument("--max-pdf-mb", type=int, default=100, help="Skip any PDF larger than this size.")
    parser.add_argument(
        "--max-extracted-chars",
        type=int,
        default=int(os.environ.get("ZOT_AI_NOTE_MAX_EXTRACTED_CHARS", "180000")),
        help="Maximum extracted Markdown characters sent per item in MinerU modes. 0 disables truncation.",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=int(os.environ.get("ZOT_AI_NOTE_MAX_IMAGES", "24")),
        help="Maximum MinerU-extracted images attached per item in mineru-markdown-images mode.",
    )
    parser.add_argument(
        "--max-image-mb",
        type=int,
        default=int(os.environ.get("ZOT_AI_NOTE_MAX_IMAGE_MB", "8")),
        help="Skip extracted images larger than this size in mineru-markdown-images mode.",
    )
    parser.add_argument("--refresh-mineru-cache", action="store_true", help="Ignore cached MinerU Markdown/images and extract again.")
    parser.add_argument("--template-dir", type=Path, default=DEFAULT_TEMPLATE_DIR, help="Directory containing note templates.")
    parser.add_argument("--output-dir", type=Path, default=repo_root / DEFAULT_OUTPUT_DIR, help="Directory for progress files.")
    parser.add_argument("--checkpoint", type=Path, default=None, help="Checkpoint JSON path. Defaults under --output-dir.")
    parser.add_argument("--keep-uploaded-files", action="store_true", help="Do not delete uploaded AI files after each item.")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop immediately on the first item failure.")
    return parser.parse_args()


def main() -> None:
    try:
        run(parse_args())
    except KeyboardInterrupt:
        print("\n[interrupted] checkpoint has been preserved", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

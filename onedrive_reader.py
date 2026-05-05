#!/usr/bin/env python3
"""Convert OneDrive PDFs in YYYYMM folders to Markdown files.

The script processes the previous and current month folders, where folder
names use YYYYMM format (for example 202604).

For app mode, DRIVE_ID is required and should be set in .env.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List

import fitz
import msal
import pdfplumber
from pypdf import PdfReader
import requests
from dotenv import load_dotenv
from PIL import Image
import pytesseract

try:
    from cerebras.cloud.sdk import Cerebras
except ImportError:
    Cerebras = None

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MonthFolderMissingError(Exception):
    """Raised when a target YYYYMM folder does not exist."""


class LiveProgress:
    """Global ASCII loader and overall progress bar that updates continuously."""

    def __init__(self, label: str, total: int, width: int = 30) -> None:
        self.label = label
        self.total = max(total, 1)
        self.width = width
        self.done = 0
        self.note = "starting"
        self._frame_idx = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        style = os.getenv("LOADER_STYLE", "docker").strip().lower()
        if style == "yarn":
            # Yarn-like braille spinner
            self._frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        else:
            # Docker-like ASCII spinner
            self._frames = ["|", "/", "-", "\\"]

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def add_total(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            self.total += count

    def advance(self, note: str = "") -> None:
        with self._lock:
            self.done = min(self.done + 1, self.total)
            if note:
                self.note = note

    def set_note(self, note: str) -> None:
        with self._lock:
            self.note = note

    def _render(self) -> None:
        with self._lock:
            done = self.done
            total = self.total
            note = self.note
            frame = self._frames[self._frame_idx]
        ratio = done / max(total, 1)
        percent = int(ratio * 100)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        suffix = f" {note}" if note else ""
        print(
            f"\r{self.label} {frame} [{bar}] {done}/{total} {percent:3d}%{suffix}",
            end="",
            flush=True,
        )

    def _run(self) -> None:
        while self._running:
            self._render()
            self._frame_idx = (self._frame_idx + 1) % len(self._frames)
            time.sleep(0.12)

    def finish(self, note: str = "done") -> None:
        with self._lock:
            self.done = self.total
            self.note = note
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._render()
        print()


# Standard dotenv behavior: load variables from .env in current working directory.
load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def ensure_ocr_binary_available() -> None:
    """Fail fast when OCR runtime is unavailable."""
    if shutil.which("tesseract") is None:
        raise RuntimeError(
            "Required OCR binary 'tesseract' is not installed or not in PATH. "
            "Install it first (for Ubuntu/Debian: sudo apt install tesseract-ocr)."
        )


def get_app_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

    if not result or "access_token" not in result:
        error = (result or {}).get("error_description", result)
        raise RuntimeError(f"Failed to acquire app token: {error}")
    return result["access_token"]


def get_delegated_token(tenant_id: str, client_id: str) -> str:
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.PublicClientApplication(client_id=client_id, authority=authority)

    flow = app.initiate_device_flow(scopes=["Files.Read.All", "User.Read"])
    if "user_code" not in flow:
        raise RuntimeError(f"Failed to start device flow: {flow}")

    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)

    if not result or "access_token" not in result:
        error = (result or {}).get("error_description", result)
        raise RuntimeError(f"Failed to acquire delegated token: {error}")
    return result["access_token"]


def _graph_drive_prefix(auth_mode: str, drive_id: str | None) -> str:
    if auth_mode == "delegated":
        return f"{GRAPH_BASE}/me/drive"
    if drive_id:
        return f"{GRAPH_BASE}/drives/{drive_id}"
    raise ValueError("DRIVE_ID is required in app mode.")


def iter_children(token: str, folder_path: str, drive_prefix: str) -> Iterator[Dict[str, Any]]:
    normalized = folder_path.strip("/")
    item_path = f"root:/{normalized}:" if normalized else "root"
    url = f"{drive_prefix}/{item_path}/children"

    headers = {"Authorization": f"Bearer {token}"}
    params: Dict[str, Any] = {"$select": "id,name,webUrl,lastModifiedDateTime,size,file,folder"}

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            if response.status_code == 404 and "itemNotFound" in response.text:
                raise MonthFolderMissingError(folder_path)
            raise RuntimeError(f"Graph API error {response.status_code}: {response.text}")

        payload = response.json()
        for item in payload.get("value", []):
            yield item

        url = payload.get("@odata.nextLink")
        params = {}


def month_folder_names(reference_month: str | None) -> List[str]:
    if reference_month:
        if len(reference_month) != 6 or not reference_month.isdigit():
            raise ValueError("--reference-month must be in YYYYMM format.")
        year = int(reference_month[:4])
        month = int(reference_month[4:6])
        if month < 1 or month > 12:
            raise ValueError("--reference-month month must be between 01 and 12.")
    else:
        now = datetime.now(timezone.utc)
        year = now.year
        month = now.month

    current = f"{year:04d}{month:02d}"
    if month == 1:
        prev = f"{year - 1:04d}12"
    else:
        prev = f"{year:04d}{month - 1:02d}"

    return [prev, current]


def download_drive_item_content(token: str, drive_prefix: str, item_id: str) -> bytes:
    url = f"{drive_prefix}/items/{item_id}/content"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to download item {item_id}: {response.status_code} {response.text}")
    return response.content


def upload_text_to_folder(
    token: str,
    drive_prefix: str,
    folder_path: str,
    file_name: str,
    content: str,
    content_type: str,
) -> None:
    folder_path = folder_path.strip("/")
    if folder_path:
        item_path = f"root:/{folder_path}/{file_name}:"
    else:
        item_path = f"root:/{file_name}:"

    url = f"{drive_prefix}/{item_path}/content"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }
    response = requests.put(url, headers=headers, data=content.encode("utf-8"), timeout=60)
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Failed to upload {file_name}: {response.status_code} {response.text}")


def pdf_bytes_to_markdown(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    chunks: List[str] = []

    for idx, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        chunks.append(f"## Page {idx}\n\n{text}\n")

    return "\n".join(chunks).strip() + "\n"


def upload_markdown_to_folder(
    token: str,
    drive_prefix: str,
    folder_path: str,
    markdown_file_name: str,
    markdown_content: str,
) -> None:
    upload_text_to_folder(
        token=token,
        drive_prefix=drive_prefix,
        folder_path=folder_path,
        file_name=markdown_file_name,
        content=markdown_content,
        content_type="text/markdown; charset=utf-8",
    )


def _clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def extract_structured_tables(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    tables_out: List[Dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_idx, page in enumerate(pdf.pages, start=1):
            raw_tables = page.extract_tables() or []
            page_tables: List[List[List[str]]] = []
            for table in raw_tables:
                cleaned_rows: List[List[str]] = []
                for row in table or []:
                    if row is None:
                        continue
                    cleaned_rows.append([_clean_cell(cell) for cell in row])
                if cleaned_rows:
                    page_tables.append(cleaned_rows)
            if page_tables:
                tables_out.append({"page": page_idx, "tables": page_tables})
    return tables_out


def _ocr_page_text(page: fitz.Page) -> str:
    matrix = fitz.Matrix(2, 2)
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    # Try preferred OCR language sets first (for Baltic diacritics), then fallback.
    langs_raw = os.getenv("OCR_LANGS", "est+eng,eng")
    lang_candidates = [x.strip() for x in langs_raw.split(",") if x.strip()]
    if not lang_candidates:
        lang_candidates = ["eng"]

    for lang in lang_candidates:
        try:
            text = pytesseract.image_to_string(img, lang=lang, config="--oem 3 --psm 6")
            if text:
                return text.strip()
        except pytesseract.TesseractError as err:
            # Missing language data should not abort OCR; try next candidate.
            if "Failed loading language" in str(err):
                continue
            raise

    return (pytesseract.image_to_string(img, config="--oem 3 --psm 6") or "").strip()


def _text_quality_score(text: str) -> float:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return -1.0

    total = len(lines)
    avg_len = sum(len(ln) for ln in lines) / total
    short_ratio = sum(1 for ln in lines if len(ln) <= 3) / total
    alpha_ratio = sum(1 for ln in lines if re.search(r"[A-Za-z]", ln)) / total

    # Favor readable, sentence-like lines over highly fragmented output.
    return (avg_len / 24.0) + (alpha_ratio * 0.8) - (short_ratio * 1.2)


def _clean_extracted_text(text: str) -> str:
    cleaned_lines: List[str] = []
    blank_pending = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            blank_pending = True
            continue

        # Drop lines that contain no letters/digits (pure separators/noise).
        if not re.search(r"[A-Za-z0-9]", line):
            continue

        if blank_pending and cleaned_lines:
            cleaned_lines.append("")
        cleaned_lines.append(line)
        blank_pending = False

    return "\n".join(cleaned_lines).strip()


def _looks_fragmented_layout(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False

    total = len(lines)
    avg_len = sum(len(ln) for ln in lines) / total
    short_ratio = sum(1 for ln in lines if len(ln) <= 4) / total

    # Field-like extraction: too many lines with very short average line length.
    if total >= 70 and avg_len <= 16:
        return True

    # Backup rule for other fragmented layouts.
    return total >= 65 and avg_len <= 20 and short_ratio >= 0.22


def _extract_layout_blocks(page: fitz.Page) -> List[Dict[str, Any]]:
    blocks_out: List[Dict[str, Any]] = []
    raw_blocks = page.get_text("blocks") or []

    for block in raw_blocks:
        if len(block) < 5:
            continue
        x0, y0, x1, y1, text = block[:5]
        cleaned = _clean_extracted_text(str(text or ""))
        if not cleaned:
            continue
        blocks_out.append(
            {
                "x0": round(float(x0), 2),
                "y0": round(float(y0), 2),
                "x1": round(float(x1), 2),
                "y1": round(float(y1), 2),
                "text": cleaned,
            }
        )

    # Approximate reading order: top-to-bottom, then left-to-right.
    blocks_out.sort(key=lambda b: (b["y0"], b["x0"]))
    return blocks_out


def _render_layout_blocks_markdown(blocks: List[Dict[str, Any]]) -> str:
    if not blocks:
        return ""

    lines: List[str] = ["### Layout Blocks", ""]
    for idx, block in enumerate(blocks, start=1):
        coords = f"({block['x0']:.2f}, {block['y0']:.2f})-({block['x1']:.2f}, {block['y1']:.2f})"
        block_text = str(block["text"]).replace("\n", " | ")
        lines.append(f"{idx}. [{coords}] {block_text}")
    lines.append("")
    return "\n".join(lines)


def extract_pdf_markdown_with_fallback(pdf_bytes: bytes) -> Dict[str, Any]:
    chunks: List[str] = []
    ocr_used = False

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for idx, page in enumerate(doc, start=1):
            layout_blocks = _extract_layout_blocks(page)

            # Keep markdown focused on layout blocks; OCR is only a fallback when
            # PDF-native text blocks are unavailable (e.g., image-only scanned pages).
            if not layout_blocks:
                ocr_text = _clean_extracted_text(_ocr_page_text(page))
                if ocr_text:
                    layout_blocks = [
                        {
                            "x0": 0.0,
                            "y0": 0.0,
                            "x1": 0.0,
                            "y1": 0.0,
                            "text": ocr_text,
                        }
                    ]
                    ocr_used = True

            layout_md = _render_layout_blocks_markdown(layout_blocks)

            chunks.append(f"## Page {idx}\n\n{layout_md}")
        doc.close()
    except Exception:
        # Fallback extraction path if PyMuPDF fails for any reason.
        reader = PdfReader(io.BytesIO(pdf_bytes))
        for idx, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            chunks.append(f"## Page {idx}\n\n{text}\n")

    markdown = "\n".join(chunks).strip() + "\n"

    try:
        tables = extract_structured_tables(pdf_bytes)
    except Exception:
        tables = []

    return {"markdown": markdown, "tables": tables, "ocr_used": ocr_used}


def parse_json_from_ollama_response(text: str) -> Dict[str, Any] | None:
    candidate = text.strip()
    if not candidate:
        return None

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return None

    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None
    return None


def is_complete_invoice_data(data: Dict[str, Any]) -> bool:
    required = [
        "invoice_number",
        "invoice_sender",
        "invoice_date",
        "lines",
        "total_without_vat",
        "total_with_vat",
    ]
    for key in required:
        if key not in data:
            return False
    if not isinstance(data["lines"], list) or len(data["lines"]) == 0:
        return False
    return True


def _ascii_clean(value: str) -> str:
    # Keep printable Unicode (including Estonian/Latin-extended letters like ä ö ü Ü Ä Ö).
    # Only strip actual control characters (0x00-0x1f except tab/newline) and the replacement char.
    cleaned = "".join(
        ch for ch in value
        if ch == "\t" or ch == "\n" or (ord(ch) >= 0x20 and ch != "\ufffd")
    )
    return " ".join(cleaned.split()).strip()


def _extract_first_number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "")
    text = text.replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def _parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    text = str(value or "")
    text = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _extract_source_numbers(markdown_text: str, table_data: List[Dict[str, Any]]) -> List[Decimal]:
    # Focus on monetary-looking values with decimals to avoid invoice number/date noise.
    pattern = r"-?\d+(?:[\.,]\d{2,4})"
    candidates: List[str] = re.findall(pattern, markdown_text)

    for page in table_data:
        for table in page.get("tables", []):
            for row in table:
                for cell in row:
                    candidates.extend(re.findall(pattern, str(cell)))

    out: List[Decimal] = []
    seen = set()
    for raw in candidates:
        normalized = raw.replace(",", ".")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            out.append(Decimal(normalized))
        except InvalidOperation:
            continue
    return out


def _snap_to_source_amount(value: Any, source_numbers: List[Decimal]) -> float:
    parsed = _parse_decimal(value)
    if parsed is None:
        return 0.0

    if not source_numbers:
        return float(parsed)

    nearest = min(source_numbers, key=lambda x: abs(x - parsed))
    diff = abs(nearest - parsed)

    # If model output is close to a source number, keep the source number exactly.
    if diff <= Decimal("0.05"):
        return float(nearest)
    return float(parsed)


def _extract_ee_kmkr(markdown_text: str) -> str:
    match = re.search(r"\bEE\d{9}\b", markdown_text, flags=re.IGNORECASE)
    if not match:
        return ""
    return match.group(0).upper()


# Estonian (and common EU) legal entity suffixes in order of specificity.
_COMPANY_SUFFIXES = ["OÜ", "AS", "TÜ", "MTÜ", "SA", "FIE", "UÜ", "LLC", "Ltd", "GmbH", "S.A", "S.r.l"]


def _extract_source_companies(markdown_text: str) -> List[str]:
    """Find company-name candidates in markdown using legal suffix heuristic."""
    suffix_pattern = "|".join(re.escape(s) for s in _COMPANY_SUFFIXES)
    # Match 1-5 words followed by a legal suffix (case-insensitive).
    pattern = rf"(?:[\w\-\.\,]+\s+){{1,5}}(?:{suffix_pattern})\b"
    candidates = re.findall(pattern, markdown_text, flags=re.IGNORECASE | re.UNICODE)
    # Normalise whitespace, deduplicate, keep original casing from source.
    seen: Dict[str, str] = {}
    for c in candidates:
        key = " ".join(c.split()).lower()
        if key not in seen:
            seen[key] = " ".join(c.split())
    return list(seen.values())


def _snap_sender_name(llm_name: str, source_companies: List[str]) -> str:
    """Return the closest source company name if it shares enough characters with llm_name."""
    if not llm_name or not source_companies:
        return llm_name

    llm_lower = llm_name.lower()

    best: str | None = None
    best_score = 0

    for candidate in source_companies:
        cand_lower = candidate.lower()
        # Count shared words (ignore short stop-words).
        llm_words = set(w for w in re.split(r"\W+", llm_lower) if len(w) > 2)
        cand_words = set(w for w in re.split(r"\W+", cand_lower) if len(w) > 2)
        if not cand_words:
            continue
        shared = llm_words & cand_words
        score = len(shared) / max(len(llm_words), len(cand_words))
        if score > best_score:
            best_score = score
            best = candidate

    # Accept snap only if at least one meaningful word matches (score > 0).
    if best is not None and best_score > 0:
        return best
    return llm_name


def normalize_invoice_data(raw: Dict[str, Any], markdown_text: str, table_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    is_amazon = "amazon" in markdown_text.lower()
    kmkr_from_text = _extract_ee_kmkr(markdown_text)
    source_numbers = _extract_source_numbers(markdown_text, table_data)
    source_companies = _extract_source_companies(markdown_text)

    sender_name = _ascii_clean(str(raw.get("invoice_sender") or raw.get("sender_name") or ""))
    sender_reg_code = _ascii_clean(str(raw.get("sender_reg_code") or raw.get("reg_code") or ""))
    sender_kmkr = _ascii_clean(str(raw.get("sender_kmkr_number") or raw.get("kmkr_number") or ""))

    if kmkr_from_text:
        sender_kmkr = kmkr_from_text

    if is_amazon:
        if sender_kmkr.startswith("EE"):
            sender_name = "Amazon Business EU S.a r.l"
        else:
            sender_name = "Amazon Vahendus"
    else:
        sender_name = _snap_sender_name(sender_name, source_companies)

    lines_in = raw.get("lines", [])
    lines_out: List[Dict[str, Any]] = []
    if isinstance(lines_in, list):
        for line in lines_in:
            if not isinstance(line, dict):
                continue
            lines_out.append(
                {
                    "description": _ascii_clean(str(line.get("description", ""))),
                    "price_without_vat": _snap_to_source_amount(line.get("price_without_vat"), source_numbers),
                    "price_with_vat": _snap_to_source_amount(line.get("price_with_vat"), source_numbers),
                }
            )

    normalized = {
        "invoice_number": _ascii_clean(str(raw.get("invoice_number", ""))),
        "invoice_sender": sender_name,
        "sender_reg_code": sender_reg_code,
        "sender_kmkr_number": sender_kmkr,
        "invoice_date": _ascii_clean(str(raw.get("invoice_date", ""))),
        "lines": lines_out,
        "total_without_vat": _snap_to_source_amount(raw.get("total_without_vat"), source_numbers),
        "total_with_vat": _snap_to_source_amount(raw.get("total_with_vat"), source_numbers),
    }

    return normalized


def build_invoice_extraction_prompt(markdown_text: str, table_data: List[Dict[str, Any]]) -> str:
    tables_json = json.dumps(table_data, ensure_ascii=True)
    return (
        "Extract invoice data from the markdown below. Return STRICT JSON only (no text outside JSON).\n"
        "Input markdown is layout blocks in format: N. [(x0, y0)-(x1, y1)] text.\n"
        "Ignore block index numbers and all coordinate numbers inside [(x0, y0)-(x1, y1)].\n"
        "Coordinate values are layout metadata, never invoice prices.\n"
        "Copy exact numeric values as they appear in markdown/tables whenever present.\n"
        "Use comma or dot decimals from source, but output as JSON numbers.\n"
        "\n"
        "Line extraction rules (very important):\n"
        "1) Extract invoice lines only from product/service rows (e.g. rows containing item name/code/unit/qty/amount).\n"
        "2) NEVER use amounts from VAT/summary/footer rows as line prices.\n"
        "3) Exclude any row/block text containing these keywords from line extraction:\n"
        "   'kaibemaks', 'vat', 'km-ga', 'kmga', 'kokku', 'total', 'tasuda', 'summa kokku', 'kmkr', 'registrikood'.\n"
        "4) If one block contains both line rows and totals, split logically and keep only line-row amounts for lines.\n"
        "5) In table-like rows where multiple numbers appear (e.g. unit price, line sum, qty), use the row line-sum amount for price_without_vat.\n"
        "6) Keep lines concise and relevant. Do not include address/header/footer rows as invoice lines.\n"
        "7) Return at most 15 line objects in JSON lines array.\n"
        "8) Some rows are descriptive only. Do not create invoice lines from descriptive text rows.\n"
        "9) If a candidate line amount equals total_without_vat or total_with_vat, treat it as subtotal/total context, not a real invoice line.\n"
        "\n"
        "Totals rules:\n"
        "1) Extract totals from dedicated summary rows, not from item description text.\n"
        "2) Build an internal candidate map from summary labels first, then select values by label meaning (not by largest/smallest number).\n"
        "3) Map labels by meaning:\n"
        "   - net/subtotal/before-tax/excl-tax (e.g. subtotal, net total, amount excl tax) -> total_without_vat\n"
        "   - tax/VAT/GST/sales tax amount (often with percent like 20%, 24%) -> tax_amount\n"
        "   - gross/grand total/payable/amount due/incl-tax -> total_with_vat\n"
        "4) If both total_with_vat and tax_amount are present, and net subtotal is missing, set total_without_vat = total_with_vat - tax_amount.\n"
        "5) If net subtotal and tax_amount are present but gross total is missing, set total_with_vat = total_without_vat + tax_amount.\n"
        "6) If tax amount is zero-rated, total_without_vat may equal total_with_vat.\n"
        "7) Never use random numbers from coordinates/headers/IDs as totals.\n"
        "8) Do not use payment rows (payments received, paid amount, outstanding balance) as invoice totals unless explicitly labeled as grand total/payable.\n"
        "\n"
        "Validation rules:\n"
        "1) After extraction, verify that total_with_vat >= total_without_vat when tax is positive.\n"
        "2) If VAT/tax amount exists, verify approximately: total_without_vat + tax_amount ~= total_with_vat.\n"
        "3) If totals look inconsistent, re-check summary/tax rows before finalizing JSON.\n"
        "4) Verify no invoice line has the same amount as total_without_vat or total_with_vat unless source clearly shows it as a true product/service row.\n"
        "5) For single-line invoices, line amount may equal net subtotal; this is valid and should not force totals to 0.\n"
        "\n"
        "All price fields MUST be JSON numbers only (no currency symbols, no unicode signs, no text).\n"
        "Amazon rules:\n"
        "1) If invoice is from Amazon or mediated by Amazon, set invoice_sender='Amazon Vahendus'.\n"
        "2) If Amazon invoice includes EE VAT number, set invoice_sender='Amazon Business EU S.a r.l'\n"
        "   and sender_kmkr_number to that EE VAT number.\n"
        "If not Amazon, identify sender name and sender registration code if possible.\n"
        "Invoice sender can't never be Rohekood. Find real sender, if in doubt find it at footer.\n"
        "Include sender_reg_code and sender_kmkr_number in JSON.\n"
        "JSON schema:\n"
        "{\n"
        '  "invoice_number": "string",\n'
        '  "invoice_sender": "string",\n'
        '  "sender_reg_code": "string",\n'
        '  "sender_kmkr_number": "string",\n'
        '  "invoice_date": "string",\n'
        '  "lines": [\n'
        "    {\n"
        '      "description": "string",\n'
        '      "price_without_vat": 0.0,\n'
        '      "price_with_vat": 0.0\n'
        "    }\n"
        "  ],\n"
        '  "total_without_vat": 0.0,\n'
        '  "total_with_vat": 0.0\n'
        "}\n"
        "If a value is missing, use empty string.\n\n"
        "Return compact JSON only; no markdown, no code fences, no explanations.\n\n"
        "Structured tables extracted from PDF (JSON):\n"
        f"{tables_json}\n\n"
        "Markdown:\n"
        f"{markdown_text}"
    )


def extract_invoice_data_with_ollama(
    markdown_text: str,
    table_data: List[Dict[str, Any]],
    ollama_url: str,
    ollama_model: str,
) -> tuple[Dict[str, Any] | None, str]:
    prompt = build_invoice_extraction_prompt(markdown_text, table_data)

    def _call_ollama(request_payload: Dict[str, Any]) -> tuple[Dict[str, Any] | None, str]:
        try:
            response = requests.post(ollama_url, json=request_payload, timeout=180)
        except requests.exceptions.Timeout:
            return None, "ollama_timeout"
        except requests.exceptions.RequestException as err:
            return None, f"ollama_request_failed: {str(err)[:180]}"
        if response.status_code == 404 and "model" in response.text and ":latest" not in ollama_model:
            request_payload = dict(request_payload)
            request_payload["model"] = f"{ollama_model}:latest"
            try:
                response = requests.post(ollama_url, json=request_payload, timeout=180)
            except requests.exceptions.Timeout:
                return None, "ollama_timeout"
            except requests.exceptions.RequestException as err:
                return None, f"ollama_request_failed: {str(err)[:180]}"

        if response.status_code != 200:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"ollama_http_{response.status_code}: {body[:180]}"

        try:
            return response.json(), ""
        except ValueError:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"ollama_non_json_response: {body[:180]}"

    # Primary attempt: strict JSON mode.
    request_payload = {
        "model": ollama_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "think": False,
    }
    response_payload, call_reason = _call_ollama(request_payload)
    if response_payload is None:
        return None, call_reason

    raw = str(response_payload.get("response", ""))
    done_flag = bool(response_payload.get("done", True))

    # Some reasoning models can emit an incomplete/empty first pass with done=false.
    # Retry once without JSON mode but with stricter output instruction.
    if (not done_flag) or (not raw.strip()) or (raw.strip() == "{}"):
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Return exactly one valid JSON object and nothing else."
            + " Do not include reasoning, markdown, or explanations."
        )
        retry_payload = {
            "model": ollama_model,
            "prompt": retry_prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        }
        response_payload, call_reason = _call_ollama(retry_payload)
        if response_payload is None:
            return None, call_reason
        raw = str(response_payload.get("response", ""))

    parsed = parse_json_from_ollama_response(raw)
    if not parsed:
        return None, "json_parse_failed"
    parsed = normalize_invoice_data(parsed, markdown_text, table_data)
    if not is_complete_invoice_data(parsed):
        return None, "schema_incomplete"
    return parsed, ""


def extract_invoice_data_with_cerebras(
    markdown_text: str,
    table_data: List[Dict[str, Any]],
    cerebras_model: str,
) -> tuple[Dict[str, Any] | None, str]:
    if Cerebras is None:
        return None, "cerebras_sdk_not_installed"

    cerebras_api_key = os.getenv("CEREBRAS_API_KEY", "").strip()
    if not cerebras_api_key:
        return None, "missing_CEREBRAS_API_KEY"

    prompt = build_invoice_extraction_prompt(markdown_text, table_data)
    try:
        client = Cerebras(api_key=cerebras_api_key)
        completion: Any = client.chat.completions.create(
            model=cerebras_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_completion_tokens=4096,
            top_p=0.95,
            stream=False,
        )
    except Exception as err:
        return None, f"cerebras_request_failed: {str(err)[:180]}"

    content = ""
    if completion.choices and completion.choices[0].message:
        content = str(completion.choices[0].message.content or "")

    parsed = parse_json_from_ollama_response(content)
    if not parsed:
        return None, "json_parse_failed"
    parsed = normalize_invoice_data(parsed, markdown_text, table_data)
    if not is_complete_invoice_data(parsed):
        return None, "schema_incomplete"
    return parsed, ""


def extract_invoice_data_with_openrouter(
    markdown_text: str,
    table_data: List[Dict[str, Any]],
    openrouter_model: str,
) -> tuple[Dict[str, Any] | None, str]:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None, "missing_OPENROUTER_API_KEY"

    prompt = build_invoice_extraction_prompt(markdown_text, table_data)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    def _call_openrouter(request_payload: Dict[str, Any]) -> tuple[Dict[str, Any] | None, str]:
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=request_payload,
                timeout=180,
            )
        except requests.exceptions.Timeout:
            return None, "openrouter_timeout"
        except requests.exceptions.RequestException as err:
            return None, f"openrouter_request_failed: {str(err)[:180]}"

        if response.status_code != 200:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"openrouter_http_{response.status_code}: {body[:180]}"

        try:
            return response.json(), ""
        except ValueError:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"openrouter_non_json_response: {body[:180]}"

    def _extract_message_content(resp_json: Dict[str, Any]) -> tuple[str, str]:
        choices = resp_json.get("choices") or []
        if not choices:
            return "", ""

        choice0 = choices[0] or {}
        finish_reason = str(choice0.get("finish_reason") or "")
        message = choice0.get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            return content, finish_reason

        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(x for x in parts if x), finish_reason

        return str(content or ""), finish_reason

    payload = {
        "model": openrouter_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 12000,
        "response_format": {"type": "json_object"},
    }
    resp_json, reason = _call_openrouter(payload)
    if resp_json is None:
        return None, reason

    content, finish_reason = _extract_message_content(resp_json)
    parsed = parse_json_from_ollama_response(content)

    # Retry once with tighter output instructions if JSON is malformed/truncated.
    if not parsed:
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Return exactly one compact JSON object only."
            + " No markdown, no code fences, no commentary."
            + " Keep lines to real invoice item rows only."
        )
        retry_payload = {
            "model": openrouter_model,
            "messages": [{"role": "user", "content": retry_prompt}],
            "temperature": 0,
            "max_tokens": 12000,
            "response_format": {"type": "json_object"},
        }
        resp_json, reason = _call_openrouter(retry_payload)
        if resp_json is None:
            return None, reason
        content, finish_reason = _extract_message_content(resp_json)
        parsed = parse_json_from_ollama_response(content)

    if not parsed:
        suffix = f":{finish_reason}" if finish_reason else ""
        return None, f"json_parse_failed{suffix}"
    parsed = normalize_invoice_data(parsed, markdown_text, table_data)
    if not is_complete_invoice_data(parsed):
        return None, "schema_incomplete"
    return parsed, ""


def extract_invoice_data_with_github_models(
    markdown_text: str,
    table_data: List[Dict[str, Any]],
    github_models_model: str,
) -> tuple[Dict[str, Any] | None, str]:
    api_key = os.getenv("GITHUB_MODELS_API_KEY", "").strip()
    if not api_key:
        return None, "missing_GITHUB_MODELS_API_KEY"

    endpoint = os.getenv(
        "GITHUB_MODELS_URL",
        "https://models.github.ai/inference/chat/completions",
    ).strip()
    if not endpoint:
        return None, "missing_GITHUB_MODELS_URL"

    prompt = build_invoice_extraction_prompt(markdown_text, table_data)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _call_github_models(request_payload: Dict[str, Any]) -> tuple[Dict[str, Any] | None, str]:
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=request_payload,
                timeout=180,
            )
        except requests.exceptions.Timeout:
            return None, "github_models_timeout"
        except requests.exceptions.RequestException as err:
            return None, f"github_models_request_failed: {str(err)[:180]}"

        if response.status_code != 200:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"github_models_http_{response.status_code}: {body[:180]}"

        try:
            return response.json(), ""
        except ValueError:
            body = (response.text or "").strip().replace("\n", " ")
            return None, f"github_models_non_json_response: {body[:180]}"

    def _extract_message_content(resp_json: Dict[str, Any]) -> tuple[str, str]:
        choices = resp_json.get("choices") or []
        if not choices:
            return "", ""

        choice0 = choices[0] or {}
        finish_reason = str(choice0.get("finish_reason") or "")
        message = choice0.get("message") or {}
        content = message.get("content")

        if isinstance(content, str):
            return content, finish_reason

        if isinstance(content, list):
            parts: List[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(x for x in parts if x), finish_reason

        return str(content or ""), finish_reason

    payload = {
        "model": github_models_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 12000,
        "response_format": {"type": "json_object"},
    }
    resp_json, reason = _call_github_models(payload)
    if resp_json is None:
        return None, reason

    content, finish_reason = _extract_message_content(resp_json)
    parsed = parse_json_from_ollama_response(content)

    if not parsed:
        retry_prompt = (
            prompt
            + "\n\nIMPORTANT: Return exactly one compact JSON object only."
            + " No markdown, no code fences, no commentary."
            + " Keep lines to real invoice item rows only."
        )
        retry_payload = {
            "model": github_models_model,
            "messages": [{"role": "user", "content": retry_prompt}],
            "temperature": 0,
            "max_tokens": 12000,
            "response_format": {"type": "json_object"},
        }
        resp_json, reason = _call_github_models(retry_payload)
        if resp_json is None:
            return None, reason
        content, finish_reason = _extract_message_content(resp_json)
        parsed = parse_json_from_ollama_response(content)

    if not parsed:
        suffix = f":{finish_reason}" if finish_reason else ""
        return None, f"json_parse_failed{suffix}"
    parsed = normalize_invoice_data(parsed, markdown_text, table_data)
    if not is_complete_invoice_data(parsed):
        return None, "schema_incomplete"
    return parsed, ""


def extract_invoice_data(
    markdown_text: str,
    table_data: List[Dict[str, Any]],
    llm_provider: str,
    ollama_url: str,
    ollama_model: str,
    cerebras_model: str,
    openrouter_model: str = "",
    github_models_model: str = "",
) -> tuple[Dict[str, Any] | None, str]:
    provider = llm_provider.strip().lower()
    if provider == "cerebras":
        return extract_invoice_data_with_cerebras(markdown_text, table_data, cerebras_model)
    if provider == "ollama":
        return extract_invoice_data_with_ollama(markdown_text, table_data, ollama_url, ollama_model)
    if provider == "openrouter":
        return extract_invoice_data_with_openrouter(markdown_text, table_data, openrouter_model)
    if provider in ("github_models", "github-models", "github"):
        return extract_invoice_data_with_github_models(markdown_text, table_data, github_models_model)
    return None, f"unsupported_llm_provider:{provider}"


def process_month_folder(
    token: str,
    drive_prefix: str,
    folder_path: str,
    llm_provider: str,
    ollama_url: str,
    ollama_model: str,
    cerebras_model: str,
    openrouter_model: str,
    github_models_model: str,
    progress: LiveProgress,
    initial_md_count: int,
) -> Dict[str, int]:
    processed = 0
    skipped = 0
    skipped_existing = 0
    markdown_processed = 0
    markdown_skipped_existing = 0
    markdown_failed = 0

    items = list(iter_children(token, folder_path, drive_prefix))
    process_folder = f"{folder_path}/process"

    # Scan the process subfolder for already-generated files (may not exist yet).
    try:
        process_items = list(iter_children(token, process_folder, drive_prefix))
    except MonthFolderMissingError:
        process_items = []
    process_names = {str(item.get("name", "")) for item in process_items}

    pdf_id_by_stem: Dict[str, str] = {}
    for item in items:
        if "file" not in item:
            continue
        name = str(item.get("name", ""))
        if not name.lower().endswith(".pdf"):
            continue
        item_id = str(item.get("id", ""))
        if not item_id:
            continue
        pdf_id_by_stem[name.rsplit(".", 1)[0]] = item_id
    pdf_candidates = [
        item for item in items
        if "file" in item and str(item.get("name", "")).lower().endswith(".pdf")
    ]

    for item in pdf_candidates:
        name = str(item.get("name", ""))

        item_id = item.get("id")
        if not item_id:
            skipped += 1
            progress.advance(f"{folder_path} pdf skip missing-id")
            continue

        markdown_name = f"{name.rsplit('.', 1)[0]}.md"
        tables_name = f"{name.rsplit('.', 1)[0]}.tables.json"
        if markdown_name in process_names:
            skipped_existing += 1
            progress.advance(f"{folder_path} pdf skip existing-md")
            continue

        pdf_bytes = download_drive_item_content(token, drive_prefix, item_id)
        extracted = extract_pdf_markdown_with_fallback(pdf_bytes)
        markdown_content = str(extracted.get("markdown", ""))
        table_data = extracted.get("tables", [])
        upload_markdown_to_folder(token, drive_prefix, process_folder, markdown_name, markdown_content)
        if table_data:
            upload_text_to_folder(
                token=token,
                drive_prefix=drive_prefix,
                folder_path=process_folder,
                file_name=tables_name,
                content=json.dumps(table_data, ensure_ascii=True, indent=2),
                content_type="application/json; charset=utf-8",
            )
            process_names.add(tables_name)
        process_names.add(markdown_name)
        processed += 1
        progress.advance(f"{folder_path} pdf converted")

    # Reload the process subfolder so newly created md files in this run are included.
    try:
        md_items = list(iter_children(token, process_folder, drive_prefix))
    except MonthFolderMissingError:
        md_items = []
    md_names = {str(item.get("name", "")) for item in md_items}
    md_candidates = [
        item for item in md_items
        if "file" in item
        and str(item.get("name", "")).lower().endswith(".md")
        and not str(item.get("name", "")).lower().endswith(".parsed.json")
    ]

    # Newly converted PDFs may create new markdown files to parse in this same run.
    extra_md_work = len(md_candidates) - initial_md_count
    if extra_md_work > 0:
        progress.add_total(extra_md_work)

    for item in md_candidates:
        md_name = str(item.get("name", ""))

        marker_stem = md_name.rsplit(".", 1)[0]
        processed_marker = f"{marker_stem}.parsed.json"
        tables_name = f"{marker_stem}.tables.json"
        if processed_marker in md_names:
            markdown_skipped_existing += 1
            progress.advance(f"{folder_path} md skip existing-json")
            continue

        item_id = item.get("id")
        if not item_id:
            markdown_failed += 1
            progress.advance(f"{folder_path} md fail missing-id")
            continue

        try:
            md_bytes = download_drive_item_content(token, drive_prefix, str(item_id))
            md_text = md_bytes.decode("utf-8", errors="replace")
            table_data: List[Dict[str, Any]] = []
            table_item_id = None
            for possible in md_items:
                if str(possible.get("name", "")) == tables_name:
                    table_item_id = possible.get("id")
                    break
            if table_item_id:
                raw_tables = download_drive_item_content(token, drive_prefix, str(table_item_id))  # noqa
                try:
                    parsed_tables = json.loads(raw_tables.decode("utf-8", errors="replace"))
                    if isinstance(parsed_tables, list):
                        table_data = parsed_tables
                except json.JSONDecodeError:
                    table_data = []

            invoice_data, parse_reason = extract_invoice_data(
                md_text,
                table_data,
                llm_provider,
                ollama_url,
                ollama_model,
                cerebras_model,
                openrouter_model,
                github_models_model,
            )
            if not invoice_data:
                markdown_failed += 1
                print(f"{folder_path}: markdown parse failed for {md_name} ({parse_reason})")
                progress.advance(f"{folder_path} md fail parse")
                continue

            marker_content = json.dumps(
                {
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "source_markdown": md_name,
                    "invoice_file_id": pdf_id_by_stem.get(marker_stem, ""),
                    "markdown_file_id": str(item_id),
                    "tables_file_name": tables_name if table_data else "",
                    "invoice_data": invoice_data,
                },
                ensure_ascii=False,
                indent=2,
            )
            upload_text_to_folder(
                token=token,
                drive_prefix=drive_prefix,
                folder_path=process_folder,
                file_name=processed_marker,
                content=marker_content,
                content_type="application/json; charset=utf-8",
            )
            markdown_processed += 1
            md_names.add(processed_marker)
            progress.advance(f"{folder_path} md processed")
        except RuntimeError as err:
            markdown_failed += 1
            print(f"{folder_path}: markdown runtime failed for {md_name} ({err})")
            progress.advance(f"{folder_path} md fail runtime")

    return {
        "processed": processed,
        "skipped": skipped,
        "skipped_existing": skipped_existing,
        "markdown_processed": markdown_processed,
        "markdown_skipped_existing": markdown_skipped_existing,
        "markdown_failed": markdown_failed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert PDF files from previous/current month OneDrive folders to Markdown"
    )
    parser.add_argument(
        "--auth-mode",
        choices=["app", "delegated"],
        default="app",
        help="Auth method: app (service principal) or delegated (interactive device login)",
    )
    parser.add_argument(
        "--base-path",
        default=None,
        help="Optional base path before YYYYMM folders, e.g. 'Invoices' (overrides FOLDER_BASE_PATH env)",
    )
    parser.add_argument(
        "--reference-month",
        default=None,
        help="Optional reference month in YYYYMM format (default: current UTC month)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    ensure_ocr_binary_available()

    tenant_id = _require_env("TENANT_ID")
    client_id = _require_env("CLIENT_ID")

    if args.auth_mode == "app":
        client_secret = _require_env("CLIENT_SECRET")
        drive_id = _require_env("DRIVE_ID")
        token = get_app_token(tenant_id, client_id, client_secret)
    else:
        drive_id = None
        token = get_delegated_token(tenant_id, client_id)

    drive_prefix = _graph_drive_prefix(args.auth_mode, drive_id)
    llm_provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
    ollama_model = os.getenv("OLLAMA_MODEL", "llama3.1:latest")
    cerebras_model = os.getenv("CEREBRAS_MODEL", "qwen-3-235b-a22b-instruct-2507")
    openrouter_model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    github_models_model = os.getenv("GITHUB_MODELS_MODEL", "openai/gpt-4.1-mini")

    month_folders = month_folder_names(args.reference_month)
    env_base = os.getenv("FOLDER_BASE_PATH") or os.getenv("BASE_PATH") or ""
    selected_base = args.base_path if args.base_path is not None else env_base
    base = selected_base.strip("/")
    if base.lower().startswith("root/"):
        base = base[5:].lstrip("/")

    print(f"Processing folders: {month_folders[0]}, {month_folders[1]}")
    total_processed = 0
    total_skipped = 0
    total_md_processed = 0
    total_md_skipped = 0
    total_md_failed = 0

    folder_scan: Dict[str, Dict[str, Any]] = {}
    total_work = 0
    for month_folder in month_folders:
        target_folder = f"{base}/{month_folder}" if base else month_folder
        try:
            scan_items = list(iter_children(token, target_folder, drive_prefix))
            pdf_count = len(
                [
                    x
                    for x in scan_items
                    if "file" in x and str(x.get("name", "")).lower().endswith(".pdf")
                ]
            )
            # md files now live in the process/ subfolder.
            try:
                process_scan_items = list(iter_children(token, f"{target_folder}/process", drive_prefix))
            except MonthFolderMissingError:
                process_scan_items = []
            md_count = len(
                [
                    x
                    for x in process_scan_items
                    if "file" in x
                    and str(x.get("name", "")).lower().endswith(".md")
                    and not str(x.get("name", "")).lower().endswith(".parsed.json")
                ]
            )
            folder_scan[target_folder] = {"exists": True, "md_count": md_count}
            total_work += (pdf_count + md_count)
        except MonthFolderMissingError:
            folder_scan[target_folder] = {"exists": False, "md_count": 0}

    progress = LiveProgress("overall", total_work)
    progress.start()

    for month_folder in month_folders:
        target_folder = f"{base}/{month_folder}" if base else month_folder
        scan = folder_scan.get(target_folder, {"exists": True, "md_count": 0})
        if not scan.get("exists", True):
            print(f"{target_folder}: folder missing, skipped")
            continue

        try:
            result = process_month_folder(
                token=token,
                drive_prefix=drive_prefix,
                folder_path=target_folder,
                llm_provider=llm_provider,
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                cerebras_model=cerebras_model,
                openrouter_model=openrouter_model,
                github_models_model=github_models_model,
                progress=progress,
                initial_md_count=int(scan.get("md_count", 0)),
            )
            total_processed += result["processed"]
            total_skipped += result["skipped"]
            total_skipped += result["skipped_existing"]
            total_md_processed += result["markdown_processed"]
            total_md_skipped += result["markdown_skipped_existing"]
            total_md_failed += result["markdown_failed"]
            print(
                f"{target_folder}: converted {result['processed']} PDF files, "
                f"skipped existing {result['skipped_existing']}, "
                f"markdown processed {result['markdown_processed']}, "
                f"markdown skipped {result['markdown_skipped_existing']}, "
                f"markdown failed {result['markdown_failed']}"
            )
        except RuntimeError as err:
            print(f"{target_folder}: {err}", file=sys.stderr)

    progress.finish("done")

    print(
        "Done. "
        f"Converted={total_processed}, Skipped={total_skipped}, "
        f"MdProcessed={total_md_processed}, MdSkipped={total_md_skipped}, MdFailed={total_md_failed}"
    )


if __name__ == "__main__":
    main()

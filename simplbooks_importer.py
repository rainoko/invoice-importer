#!/usr/bin/env python3
"""Create SimplBooks purchase invoices from *.parsed.json files.

Behavior:
- Skips any parsed file that already has a .success.json or .failed.json sibling.
- Only submits one real invoice by default: 260416.eurostauto.parsed.json
- For all other parsed files, writes a .success.json marker without submitting.
- Optional no-submit mode marks all parsed invoices as success without opening SimplBooks.

Environment variables:
- SIMPLBOOKS_USER
- SIMPLBOOKS_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from onedrive_reader import (
    MonthFolderMissingError,
    download_drive_item_content,
    iter_children,
    resolve_onedrive_context,
    upload_text_to_folder,
)


DEFAULT_TEST_FILE = "260416.eurostauto.parsed.json"
DEFAULT_BASE_URL = "https://www.simplbooks.ee"


def _login_url(base_url: str) -> str:
    if "secure.simplbooks.com" in base_url:
        return base_url.rstrip("/")
    return "https://secure.simplbooks.com/accounts/login?locale=et_EE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import parsed invoices into SimplBooks")
    parser.add_argument(
        "--source",
        choices=["onedrive", "local"],
        default="onedrive",
        help="Input/output backend (default: onedrive)",
    )
    parser.add_argument(
        "--input-dir",
        default=".",
        help="Directory containing *.parsed.json files when --source=local",
    )
    parser.add_argument(
        "--auth-mode",
        choices=["app", "delegated"],
        default="app",
        help="OneDrive auth method when --source=onedrive",
    )
    parser.add_argument(
        "--base-path",
        default=None,
        help="Optional OneDrive base path before YYYYMM folders (overrides env)",
    )
    parser.add_argument(
        "--reference-month",
        default=None,
        help="Optional month in YYYYMM format to process current+previous month from",
    )
    parser.add_argument(
        "--test-file",
        default=DEFAULT_TEST_FILE,
        help="Only this parsed filename will be submitted to SimplBooks",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SIMPLBOOKS_BASE_URL", DEFAULT_BASE_URL),
        help="SimplBooks base URL",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode",
    )
    parser.add_argument(
        "--no-submit",
        action="store_true",
        help="Do not create invoices in SimplBooks; write success markers only",
    )
    return parser.parse_args()


def list_parsed_files(input_dir: Path) -> List[Path]:
    return sorted(p for p in input_dir.glob("*.parsed.json") if p.is_file())


def result_names(parsed_name: str) -> Tuple[str, str]:
    if not parsed_name.endswith(".parsed.json"):
        raise ValueError(f"Unexpected parsed file name: {parsed_name}")
    stem = parsed_name[: -len(".parsed.json")]
    return f"{stem}.parsed.success.json", f"{stem}.parsed.failed.json"


def result_paths(parsed_path: Path) -> Tuple[Path, Path]:
    success_name, failed_name = result_names(parsed_path.name)
    return parsed_path.with_name(success_name), parsed_path.with_name(failed_name)


def has_result(parsed_path: Path) -> bool:
    success_path, failed_path = result_paths(parsed_path)
    return success_path.exists() or failed_path.exists()


def write_result(parsed_path: Path, ok: bool, payload: Dict[str, Any]) -> None:
    success_path, failed_path = result_paths(parsed_path)
    target = success_path if ok else failed_path
    payload = dict(payload)
    payload["written_at"] = datetime.now(timezone.utc).isoformat()
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_parsed_invoice(parsed_path: Path) -> Dict[str, Any]:
    raw = parsed_path.read_text(encoding="utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Parsed file is not a JSON object")
    return data


def load_parsed_invoice_bytes(raw: bytes) -> Dict[str, Any]:
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("Parsed file is not a JSON object")
    return data


def _resolve_onedrive_context(args: argparse.Namespace) -> Tuple[str, str, List[str]]:
    token, drive_prefix, target_folders = resolve_onedrive_context(
        auth_mode=args.auth_mode,
        base_path=args.base_path,
        reference_month=args.reference_month,
    )
    return token, drive_prefix, target_folders


def list_onedrive_parsed_jobs(token: str, drive_prefix: str, target_folders: List[str]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for folder in target_folders:
        process_folder = f"{folder}/process"
        try:
            items = list(iter_children(token, process_folder, drive_prefix))
        except MonthFolderMissingError:
            print(f"{process_folder}: missing, skipped")
            continue

        file_items = [x for x in items if "file" in x]
        names = {str(x.get("name", "")) for x in file_items}

        for item in file_items:
            name = str(item.get("name", ""))
            item_id = str(item.get("id", ""))
            if not name.endswith(".parsed.json") or not item_id:
                continue

            success_name, failed_name = result_names(name)
            if success_name in names or failed_name in names:
                print(f"SKIP existing result: {process_folder}/{name}")
                continue

            jobs.append({
                "parsed_name": name,
                "item_id": item_id,
                "process_folder": process_folder,
            })

    return sorted(jobs, key=lambda x: f"{x['process_folder']}/{x['parsed_name']}")


def write_onedrive_result(
    token: str,
    drive_prefix: str,
    process_folder: str,
    parsed_name: str,
    ok: bool,
    payload: Dict[str, Any],
) -> None:
    success_name, failed_name = result_names(parsed_name)
    target_name = success_name if ok else failed_name
    payload = dict(payload)
    payload["written_at"] = datetime.now(timezone.utc).isoformat()

    upload_text_to_folder(
        token=token,
        drive_prefix=drive_prefix,
        folder_path=process_folder,
        file_name=target_name,
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        content_type="application/json",
    )


def _try_fill(page: Page, selectors: List[str], value: str) -> bool:
    if not value:
        return False
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            loc.fill(value, timeout=2500)
            return True
        except Exception:
            continue
    return False


def _try_click(page: Page, selectors: List[str]) -> bool:
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _fill(page: Page, selector: str, value: str) -> None:
    page.locator(selector).first.fill(value, timeout=5000)


def _format_decimal(value: Any) -> str:
    if value in (None, ""):
        return "0,00"
    return f"{float(value):.2f}".replace(".", ",")


def _parse_decimal(value: Any) -> float:
    text = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not text:
        return 0.0
    return float(text)


def _money_round(value: float) -> float:
    return round(value + 1e-9, 2)


def _fill_supplier(page: Page, supplier_name: str) -> None:
    input_selector = "#client-select-ts-control"
    page.locator(input_selector).fill(supplier_name, timeout=5000)
    page.locator(input_selector).press("Enter")
    page.wait_for_timeout(500)


def _fill_purchase_row(page: Page, row_index: int, line: Dict[str, Any]) -> None:
    description = str(line.get("description") or "")
    amount = _format_decimal(line.get("price_without_vat"))

    name_fields = page.locator('input[name*="[PurchaseRow][name]"]')
    quantity_fields = page.locator('input[name*="[PurchaseRow][amount]"]')
    unit_fields = page.locator('input[name*="[PurchaseRow][unit]"]')
    sum_fields = page.locator('input[name*="[PurchaseRow][sum]"]')

    if name_fields.count() <= row_index:
        raise RuntimeError(f"Purchase row {row_index} was not created")

    name_fields.nth(row_index).fill(description, timeout=5000)
    quantity_fields.nth(row_index).fill("1", timeout=5000)
    unit_fields.nth(row_index).fill("tk", timeout=5000)
    sum_fields.nth(row_index).fill(amount, timeout=5000)
    sum_fields.nth(row_index).press("Tab")


def _add_purchase_row(page: Page) -> None:
    name_fields = page.locator('input[name*="[PurchaseRow][name]"]')
    before_count = name_fields.count()

    clicked = _try_click(page, [
        '#add-new-purchase-row',
        'a:has-text("Lisa uus rida")',
        'button:has-text("Lisa uus rida")',
    ])
    if not clicked:
        clicked = bool(page.evaluate("""
            () => {
                const candidates = Array.from(document.querySelectorAll('a, button'));
                const target = candidates.find((el) => (el.textContent || '').includes('Lisa uus rida'));
                if (!target) return false;
                target.click();
                return true;
            }
        """))

    if not clicked:
        raise RuntimeError("Could not click 'Lisa uus rida'")

    page.wait_for_function(
        "prev => document.querySelectorAll('input[name*=\"[PurchaseRow][name]\"]').length > prev",
        arg=before_count,
        timeout=5000,
    )


def _click_save_invoice(page: Page) -> None:
    selectors = [
        '#purchase-submit-btn',
        'button:has-text("Salvesta ostuarve")',
        'button[type="submit"]:has-text("Salvesta")',
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=5000)
            loc.click(timeout=5000)
            return
        except Exception:
            continue

    # Final fallback for cases where overlays block strict interactability.
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=3000, force=True)
            return
        except Exception:
            continue

    raise RuntimeError("Could not find save button on purchase invoice form")


def _read_ui_totals(page: Page) -> Dict[str, float]:
    sum_fields = page.locator('input[name*="[PurchaseRow][sum]"]')
    net_sum = 0.0
    for idx in range(sum_fields.count()):
        net_sum += _parse_decimal(sum_fields.nth(idx).input_value())
    net_sum = _money_round(net_sum)

    vat_value = _parse_decimal(page.locator("#PurchaseTotalsVat").input_value())
    vat_value = _money_round(vat_value)

    total = _money_round(net_sum + vat_value)
    return {
        "net_sum": net_sum,
        "vat": vat_value,
        "total": total,
    }


def _validate_and_adjust_totals(page: Page, invoice: Dict[str, Any]) -> Dict[str, Any]:
    expected_total = _money_round(_parse_decimal(invoice.get("total_with_vat")))
    current = _read_ui_totals(page)

    delta = _money_round(expected_total - current["total"])
    delta_cents = int(round(abs(delta) * 100))

    corrected_vat = False
    if delta_cents in (1, 2):
        new_vat = _money_round(current["vat"] + delta)
        _fill(page, "#PurchaseTotalsVat", _format_decimal(new_vat))
        page.locator("#PurchaseTotalsVat").press("Tab")
        page.wait_for_timeout(200)
        corrected_vat = True
        current = _read_ui_totals(page)
        delta = _money_round(expected_total - current["total"])
        delta_cents = int(round(abs(delta) * 100))

    return {
        "expected_total_with_vat": expected_total,
        "ui_total_without_vat": current["net_sum"],
        "ui_vat": current["vat"],
        "ui_total_with_vat": current["total"],
        "difference": delta,
        "difference_cents": delta_cents,
        "vat_corrected": corrected_vat,
        "within_tolerance": delta_cents <= 2,
    }


def login(page: Page, base_url: str, user: str, password: str) -> None:
    page.goto(_login_url(base_url), wait_until="domcontentloaded")

    filled_user = False
    try:
        page.get_by_label("E-post").fill(user, timeout=5000)
        filled_user = True
    except Exception:
        filled_user = _try_fill(page, [
            'input[name="username"]',
            'input[name="email"]',
            'input[type="email"]',
            'input[placeholder="E-post"]',
            '#username',
        ], user)
    if not filled_user:
        raise RuntimeError("Could not find username field on SimplBooks login page")

    filled_password = False
    try:
        page.get_by_label("Salasõna").fill(password, timeout=5000)
        filled_password = True
    except Exception:
        filled_password = _try_fill(page, [
            'input[name="password"]',
            'input[type="password"]',
            'input[placeholder="Salasõna"]',
            '#password',
        ], password)
    if not filled_password:
        raise RuntimeError("Could not find password field on SimplBooks login page")

    if not _try_click(page, [
        'button:has-text("Logi sisse")',
        'button:has-text("Logi")',
        'button:has-text("Login")',
        'button:has-text("Sign in")',
        'button[type="submit"]',
        'input[type="submit"]',
    ]):
        raise RuntimeError("Could not find login submit button")

    # Dashboard/login completion signal.
    page.wait_for_load_state("networkidle", timeout=15000)


def navigate_to_purchase_invoices(page: Page) -> None:
    if not _try_click(page, [
        'a:has-text("Tehingud")',
        'button:has-text("Tehingud")',
    ]):
        raise RuntimeError("Could not open Tehingud menu")

    if not _try_click(page, [
        'a[href$="/purchases"]',
        'a:has-text("Ostuarved")',
        'button:has-text("Ostuarved")',
    ]):
        raise RuntimeError("Could not open Ostuarved page")

    page.wait_for_load_state("networkidle", timeout=15000)


def create_invoice(page: Page, parsed: Dict[str, Any]) -> Dict[str, Any]:
    invoice = parsed.get("invoice_data") or {}
    sender = str(invoice.get("invoice_sender") or "")
    invoice_number = str(invoice.get("invoice_number") or "")
    invoice_date = str(invoice.get("invoice_date") or "")
    lines = invoice.get("lines") or []

    if not _try_click(page, [
        'a[href$="/purchases/add"]',
        'button:has-text("Uus ostuarve")',
        'a:has-text("Uus ostuarve")',
    ]):
        raise RuntimeError("Could not open new purchase invoice form")

    page.wait_for_load_state("domcontentloaded", timeout=10000)

    _fill_supplier(page, sender)
    _fill(page, "#PurchaseNumber", invoice_number)
    _fill(page, "#PurchaseCreated", invoice_date)
    _fill(page, "#PurchaseTransactionDate", invoice_date)
    _fill(page, "#PurchaseDue", invoice_date)

    if not isinstance(lines, list) or not lines:
        raise RuntimeError("No purchase rows available in parsed invoice")

    for row_index, line in enumerate(lines):
        if row_index > 0:
            _add_purchase_row(page)
            page.wait_for_timeout(300)
        _fill_purchase_row(page, row_index, line)

    page.wait_for_timeout(300)
    totals_check = _validate_and_adjust_totals(page, invoice)

    # Final save.
    _click_save_invoice(page)

    page.wait_for_load_state("networkidle", timeout=15000)
    return totals_check


def process_single_invoice(
    parsed_name: str,
    parsed: Dict[str, Any],
    args: argparse.Namespace,
    user: str,
    password: str,
    write_result_fn: Callable[[bool, Dict[str, Any]], None],
) -> None:
    if getattr(args, "no_submit", False) is True:
        write_result_fn(
            True,
            {
                "status": "success",
                "mode": "dry-run-no-submit",
                "parsed_file": parsed_name,
                "message": "Skipped SimplBooks creation due to --no-submit mode.",
            },
        )
        print(f"SUCCESS dry-run: {parsed_name}")
        return

    if parsed_name != args.test_file:
        write_result_fn(
            True,
            {
                "status": "success",
                "mode": "placeholder-no-submit",
                "parsed_file": parsed_name,
                "message": "Skipped actual SimplBooks creation for non-test file.",
            },
        )
        print(f"SUCCESS placeholder: {parsed_name}")
        return

    if not user or not password:
        write_result_fn(
            False,
            {
                "status": "failed",
                "parsed_file": parsed_name,
                "error": "Missing SIMPLBOOKS_USER or SIMPLBOOKS_PASSWORD",
            },
        )
        print(f"FAILED {parsed_name}: missing credentials")
        return

    try:
        totals_check: Dict[str, Any] = {}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context()
            page = context.new_page()
            login(page, args.base_url, user, password)
            navigate_to_purchase_invoices(page)
            totals_check = create_invoice(page, parsed)
            context.close()
            browser.close()

        if totals_check.get("difference_cents", 0) > 2:
            write_result_fn(
                False,
                {
                    "status": "failed",
                    "mode": "submitted-total-mismatch",
                    "parsed_file": parsed_name,
                    "invoice_number": ((parsed.get("invoice_data") or {}).get("invoice_number") or ""),
                    "message": "Invoice was saved in SimplBooks, but total mismatch exceeded 2 cents.",
                    "totals_check": totals_check,
                },
            )
            print(f"FAILED submitted with mismatch: {parsed_name}")
        else:
            write_result_fn(
                True,
                {
                    "status": "success",
                    "mode": "submitted",
                    "parsed_file": parsed_name,
                    "invoice_number": ((parsed.get("invoice_data") or {}).get("invoice_number") or ""),
                    "totals_check": totals_check,
                },
            )
            print(f"SUCCESS submitted: {parsed_name}")
    except (RuntimeError, PlaywrightTimeoutError, Exception) as err:
        write_result_fn(
            False,
            {
                "status": "failed",
                "parsed_file": parsed_name,
                "error": str(err),
                "traceback": traceback.format_exc(),
            },
        )
        print(f"FAILED {parsed_name}: {err}")


def main() -> None:
    load_dotenv()
    args = parse_args()

    user = os.getenv("SIMPLBOOKS_USER", "").strip()
    password = os.getenv("SIMPLBOOKS_PASSWORD", "").strip()

    if args.source == "local":
        input_dir = Path(args.input_dir)
        parsed_files = list_parsed_files(input_dir)
        if not parsed_files:
            print("No *.parsed.json files found.")
            return

        for parsed_path in parsed_files:
            if has_result(parsed_path):
                print(f"SKIP existing result: {parsed_path.name}")
                continue

            parsed = load_parsed_invoice(parsed_path)
            process_single_invoice(
                parsed_name=parsed_path.name,
                parsed=parsed,
                args=args,
                user=user,
                password=password,
                write_result_fn=lambda ok, payload, p=parsed_path: write_result(p, ok=ok, payload=payload),
            )
        return

    token, drive_prefix, target_folders = _resolve_onedrive_context(args)
    jobs = list_onedrive_parsed_jobs(token, drive_prefix, target_folders)
    if not jobs:
        print("No OneDrive *.parsed.json files to process.")
        return

    for job in jobs:
        parsed_name = str(job["parsed_name"])
        process_folder = str(job["process_folder"])
        item_id = str(job["item_id"])

        raw = download_drive_item_content(token, drive_prefix, item_id)
        parsed = load_parsed_invoice_bytes(raw)

        process_single_invoice(
            parsed_name=parsed_name,
            parsed=parsed,
            args=args,
            user=user,
            password=password,
            write_result_fn=lambda ok, payload, folder=process_folder, name=parsed_name: write_onedrive_result(
                token,
                drive_prefix,
                folder,
                name,
                ok,
                payload,
            ),
        )


if __name__ == "__main__":
    main()

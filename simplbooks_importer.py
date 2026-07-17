#!/usr/bin/env python3
"""Create SimplBooks purchase invoices from *.parsed.json files.

Behavior:
- Skips any parsed file that already has a .success.json or .failed.json sibling.
- Submits all parsed invoices by default.
- If --test-file is provided, only that parsed invoice will be submitted.
- Optional no-submit mode marks all parsed invoices as success without opening SimplBooks.

Environment variables:
- SIMPLBOOKS_USER
- SIMPLBOOKS_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
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
        default=None,
        help="If set, only this parsed filename will be submitted to SimplBooks",
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


def _close_calendar_popups(page: Page) -> None:
    # Close datepicker overlays without Escape, because Escape can revert values
    # back to widget defaults on this form.
    try:
        page.evaluate(
            """
            () => {
              const active = document.activeElement;
              if (active && typeof active.blur === 'function') active.blur();
            }
            """
        )
    except Exception:
        pass

    try:
        page.mouse.click(5, 5)
    except Exception:
        pass


# Month names across the locales we receive invoices in (Estonian, German,
# English), plus common abbreviations, mapped to their month number.
_MONTH_NAMES = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # German
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6,
    "juli": 7, "oktober": 10, "dezember": 12,
    # Estonian
    "jaanuar": 1, "veebruar": 2, "märts": 3, "marts": 3, "aprill": 4,
    "juuni": 6, "juuli": 7, "oktoober": 10, "detsember": 12,
}


def _format_ddmmyyyy(day: int, month: int, year: int) -> str:
    if year < 100:
        year += 2000
    return f"{day:02d}.{month:02d}.{year:04d}"


def _normalize_date_value(value: str) -> str:
    """Normalize an invoice date to SimplBooks' dd.mm.yyyy format.

    Handles values already in dd.mm.yyyy, ISO yyyy-mm-dd, and "13 Juni 2026"
    style dates with localized month names. Raises ValueError when the value is
    present but cannot be parsed, so a bad date fails loudly instead of silently
    clearing the date field on save.
    """
    text = str(value or "").strip()
    if not text:
        return text

    numeric = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", text)
    if numeric:
        day, month, year = (int(g) for g in numeric.groups())
        return _format_ddmmyyyy(day, month, year)

    iso = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if iso:
        year, month, day = (int(g) for g in iso.groups())
        return _format_ddmmyyyy(day, month, year)

    # Day + localized month name + year, in any order (e.g. "13 Juni 2026",
    # "13. Juni 2026", "June 13, 2026").
    tokens = re.findall(r"[^\W\d_]+|\d+", text, flags=re.UNICODE)
    month = next((_MONTH_NAMES[t.lower()] for t in tokens if t.lower() in _MONTH_NAMES), None)
    numbers = [int(t) for t in tokens if t.isdigit()]
    year = next((n for n in numbers if n > 31), None)
    day = next((n for n in numbers if 1 <= n <= 31 and n != year), None)
    if month and day and year:
        return _format_ddmmyyyy(day, month, year)

    raise ValueError(f"Unrecognized invoice date format: {text!r}")


def _set_date_field(page: Page, selector: str, value: str) -> None:
    wanted = str(value or "").strip()
    updated = bool(
        page.evaluate(
            """
            ({ sel, val }) => {
              const el = document.querySelector(sel);
              if (!el) return false;
              el.value = String(val || '');
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              return String(el.value || '').trim() === String(val || '').trim();
            }
            """,
            {"sel": selector, "val": wanted},
        )
    )
    if not updated:
        raise RuntimeError(f"Could not set date field: {selector}")


def _fill_purchase_dates(page: Page, invoice_date: str) -> None:
    try:
        date_value = _normalize_date_value(invoice_date)
    except ValueError as err:
        raise RuntimeError(str(err)) from err
    _set_date_field(page, "#PurchaseTransactionDate", date_value)
    _close_calendar_popups(page)
    _set_date_field(page, "#PurchaseDue", date_value)
    _close_calendar_popups(page)
    _set_date_field(page, "#PurchaseCreated", date_value)


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


def _parse_boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _invoice_eu_buy(invoice: Dict[str, Any]) -> bool:
    """Whether the purchase is an intra-EU acquisition (reverse charge).

    A supplier VAT number that isn't Estonian is always an EU acquisition,
    regardless of the flag the reader stored. An EE number — or no VAT number at
    all — falls back to the stored ``eu_buy`` flag (a domestic purchase by default).
    """
    kmkr = str(invoice.get("sender_kmkr_number") or "").strip().upper()
    if kmkr:
        return not kmkr.startswith("EE")
    return _parse_boolish(invoice.get("eu_buy", False))


def _resolve_purchase_settings(
    invoice: Dict[str, Any], line: Dict[str, Any] | None = None
) -> Dict[str, str]:
    """Resolve the account code and VAT profile for a purchase row.

    When ``line`` is given, car-expense and EU goods/service are decided per row;
    without it (legacy/whole-invoice callers) car-expense is any-line and EU
    defaults to goods.
    """
    # EU acquisitions take precedence over everything (incl. car expense): they are
    # always a 0% reverse charge, split by row into services vs goods. Account and
    # VAT profile are always paired.
    if _invoice_eu_buy(invoice):
        is_service = _parse_boolish(line.get("is_service", False)) if line is not None else False
        if is_service:
            return {
                "account_code": _required_env("SIMPLBOOKS_ACCOUNT_CODE_EU_SERVICE"),
                "vat_profile": _required_env("SIMPLBOOKS_VAT_PROFILE_EU_SERVICE"),
            }
        return {
            "account_code": _required_env("SIMPLBOOKS_ACCOUNT_CODE_EU_BUY"),
            "vat_profile": _required_env("SIMPLBOOKS_VAT_PROFILE_EU_BUY"),
        }

    if line is not None:
        has_car_expense = _parse_boolish(line.get("is_car_expense", False))
    else:
        lines = invoice.get("lines")
        has_car_expense = isinstance(lines, list) and any(
            isinstance(row, dict) and _parse_boolish(row.get("is_car_expense", False))
            for row in lines
        )

    if has_car_expense:
        return {
            "account_code": _required_env("SIMPLBOOKS_ACCOUNT_CODE_CAR_EXPENSE"),
            "vat_profile": _required_env("SIMPLBOOKS_VAT_PROFILE_CAR_EXPENSE"),
        }

    return {
        "account_code": _required_env("SIMPLBOOKS_ACCOUNT_CODE_DEFAULT"),
        "vat_profile": _required_env("SIMPLBOOKS_VAT_PROFILE_DEFAULT"),
    }


def _try_supplier_refresh_fallback(page: Page, reg_code: str, name_token: str) -> bool:
    """Recover from a blocked Äriregister refresh by selecting a saved client.

    When the registration code is not found in Äriregister (e.g. a foreign
    supplier), SimplBooks shows a "Saan aru" overlay inside the supplier
    offcanvas. This dismisses that overlay, then looks the supplier up among the
    company's saved clients via the `/clients/autocomplete` endpoint — whose
    response carries each client's `reg_no` even though the rendered dropdown
    only shows names — and selects the client whose registration code matches in
    the "Nimetus" TomSelect, populating the form. Returns True when a matching
    saved client was selected, False otherwise.
    """
    wanted = str(reg_code or "").strip()
    if not wanted:
        return False

    # Dismiss the "Saan aru" overlay inside the visible supplier offcanvas (best
    # effort — the lookup below works via JS regardless of the overlay). Its
    # button shares a class with a hidden inline-form button elsewhere on the
    # page, so scope the selector to the offcanvas.
    try:
        page.locator("#client-form-offcanvas .client-overlay-hide-btn").first.click(timeout=2000)
        page.wait_for_timeout(200)
    except Exception:
        pass

    # Find the saved client by registration code and select it. Query by a name
    # token first (precise and avoids the endpoint's result cap), then fall back
    # to the full client list ("[ALL]") if needed.
    try:
        selected = page.evaluate(
            """
            async ({ wanted, nameToken }) => {
                const base = location.pathname.split('/').slice(0, 2).join('/');
                const fetchClients = async (q) => {
                    const resp = await fetch(
                        `${base}/clients/autocomplete?query=${encodeURIComponent(q)}`,
                        { headers: { 'X-Requested-With': 'XMLHttpRequest' } }
                    );
                    if (!resp.ok) return [];
                    const json = await resp.json();
                    return Object.values((json && json.data) || {});
                };
                const matchReg = (list) =>
                    list.find((c) => String(c.reg_no || '').trim() === wanted) || null;

                let match = nameToken ? matchReg(await fetchClients(nameToken)) : null;
                if (!match) match = matchReg(await fetchClients('[ALL]'));
                if (!match) return null;

                const sel = document.querySelector('#client-select-offcanvas');
                const ts = sel && sel.tomselect;
                if (!ts) return null;
                ts.addOption({ id: match.id, text: match.name, name: match.name });
                ts.setValue(String(match.id));
                return { id: match.id, name: match.name };
            }
            """,
            {"wanted": wanted, "nameToken": str(name_token or "").strip()},
        )
    except Exception:
        return False

    if not selected:
        return False

    # Confirm the selection linked a client and populated the registration code.
    try:
        page.wait_for_function(
            """
            (wantedRegCode) => {
                const sel = document.querySelector('#client-select-offcanvas');
                const regEl = document.querySelector('#PurchaseClientRegNo');
                const chosen = String(sel?.value || '').trim();
                const reg = String(regEl?.value || '').trim();
                const wanted = String(wantedRegCode || '').trim();
                return !!chosen && reg.includes(wanted);
            }
            """,
            arg=wanted,
            timeout=10000,
        )
    except Exception:
        return False
    return True


def _predefined_supplier_client_id(invoice: Dict[str, Any]) -> str:
    """Return a predefined SimplBooks client id for suppliers Äriregister can't
    resolve, or "" if the normal registration-code flow should be used.

    Amazon is not in the Estonian business register, so it maps to two saved
    clients depending on how the purchase is taxed:
      - eu_buy (no EE VAT)      -> 'Amazon Vahendus'     (SIMPLBOOKS_CLIENT_ID_AMAZON_VAHENDUS)
      - has EE VAT (not eu_buy) -> 'Amazon EU S.a.r.L'   (SIMPLBOOKS_CLIENT_ID_AMAZON_EU)
    """
    name = str(invoice.get("invoice_sender") or "").lower()
    is_amazon = _parse_boolish(invoice.get("is_amazon", False)) or "amazon" in name
    if not is_amazon:
        return ""
    if _invoice_eu_buy(invoice):
        return os.getenv("SIMPLBOOKS_CLIENT_ID_AMAZON_VAHENDUS", "241").strip()
    return os.getenv("SIMPLBOOKS_CLIENT_ID_AMAZON_EU", "9").strip()


def _open_supplier_offcanvas(page: Page) -> None:
    try:
        page.locator('button[data-bs-target="#client-form-offcanvas"]').first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not click 'Muuda tarbija andmeid'") from err
    try:
        page.locator("#client-form-offcanvas").first.wait_for(state="visible", timeout=5000)
    except Exception as err:
        raise RuntimeError("Supplier modal did not open") from err
    # Dismiss the "Saan aru" overlay if present (best effort).
    try:
        page.locator("#client-form-offcanvas .client-overlay-hide-btn").first.click(timeout=1500)
        page.wait_for_timeout(150)
    except Exception:
        pass


def _submit_supplier_offcanvas(page: Page) -> None:
    try:
        page.locator("#client-form-offcanvas-submit").first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not click 'Korras' in supplier modal") from err
    page.wait_for_timeout(500)


def _client_search_token(name: str) -> str:
    """A distinctive word from the client name to type into the supplier search."""
    words = re.findall(r"[A-Za-z]{3,}", str(name or ""))
    words.sort(key=len, reverse=True)
    return words[0] if words else str(name or "").strip()


def _select_existing_client_by_id(page: Page, client_id: str, query: str) -> None:
    """Attach an existing saved client by selecting it in the Nimetus dropdown.

    The Nimetus TomSelect has no onChange handler, so a programmatic setValue does
    NOT trigger SimplBooks' client-load — the supplier fields stay empty and the
    save silently hangs. We must drive the real interaction: type a query, then
    click the dropdown option whose data-value is the client id.
    """
    cid = str(client_id or "").strip()
    if not cid:
        raise RuntimeError("Client id is missing")

    ctrl = page.locator("#client-select-offcanvas-ts-control")
    try:
        ctrl.click(timeout=5000)
        ctrl.fill(str(query or "").strip(), timeout=3000)
    except Exception as err:
        raise RuntimeError("Could not open supplier name dropdown") from err

    option = page.locator(f'.ts-dropdown .option[data-value="{cid}"]')
    try:
        option.first.wait_for(state="visible", timeout=8000)
        option.first.click(timeout=3000)
    except Exception as err:
        raise RuntimeError(f"Saved client {cid} not found in supplier dropdown") from err

    # Wait for the selection to register; SimplBooks then loads the client's data.
    try:
        page.wait_for_function(
            "(id) => document.querySelector('#client-select-offcanvas')?.tomselect?.getValue() === String(id)",
            arg=cid,
            timeout=8000,
        )
    except Exception:
        pass
    page.wait_for_timeout(400)


def _select_predefined_client(page: Page, client_id: str, name_query: str = "") -> None:
    """Attach a supplier by selecting a predefined saved client by its id
    (Amazon, which is not in Äriregister)."""
    cid = str(client_id or "").strip()
    if not cid:
        raise RuntimeError("Predefined supplier client id is missing")
    _open_supplier_offcanvas(page)
    _select_existing_client_by_id(page, cid, name_query or "Amazon")
    _submit_supplier_offcanvas(page)


def _is_foreign_supplier(invoice: Dict[str, Any]) -> bool:
    """A non-Estonian supplier that Äriregister can't resolve."""
    country = str(invoice.get("sender_country") or "").strip().upper()
    return bool(country) and country != "EE"


def _find_saved_client_id(page: Page, name: str, vat: str, reg: str) -> str:
    """Return the id of a saved client matching this supplier's VAT/reg, else ""."""
    want = [v for v in (str(vat or "").strip(), str(reg or "").strip()) if v]
    if not want:
        return ""
    try:
        found = page.evaluate(
            """
            async ({ query, want }) => {
                const base = location.pathname.split('/').slice(0, 2).join('/');
                const resp = await fetch(
                    `${base}/clients/autocomplete?query=${encodeURIComponent(query || '[ALL]')}`,
                    { headers: { 'X-Requested-With': 'XMLHttpRequest' } }
                );
                if (!resp.ok) return '';
                const list = Object.values(((await resp.json()) || {}).data || {});
                const m = list.find((c) =>
                    want.includes(String(c.vat_no || '').trim()) ||
                    want.includes(String(c.reg_no || '').trim())
                );
                return m ? String(m.id) : '';
            }
            """,
            {"query": _client_search_token(name), "want": want},
        )
    except Exception:
        return ""
    return str(found or "")


def _fill_foreign_supplier(page: Page, invoice: Dict[str, Any]) -> None:
    """Attach a foreign supplier: reuse a matching saved client (selected via the
    real dropdown), otherwise create a new one with minimal data.

    New clients get set synchronously (the create()-backed Nimetus TomSelect, plain
    reg/VAT/street inputs, and the ISO-code country TomSelect); existing clients
    must be selected by clicking the dropdown option so SimplBooks loads them.
    """
    name = str(invoice.get("invoice_sender") or "").strip()
    if not name:
        raise RuntimeError("Foreign supplier name is missing")
    reg = str(invoice.get("sender_reg_code") or "").strip()
    vat = str(invoice.get("sender_kmkr_number") or "").strip()
    country = str(invoice.get("sender_country") or "").strip().upper()
    address = str(invoice.get("sender_address") or "").strip()

    _open_supplier_offcanvas(page)

    existing_id = _find_saved_client_id(page, name, vat, reg)
    if existing_id:
        _select_existing_client_by_id(page, existing_id, _client_search_token(name))
        _submit_supplier_offcanvas(page)
        return

    outcome = page.evaluate(
        """
        ({ name, reg, vat, country, address }) => {
            const nts = document.querySelector('#client-select-offcanvas')?.tomselect;
            if (!nts) return { ok: false, reason: 'no name select' };
            const item = nts.settings.create ? nts.settings.create.call(nts, String(name || '')) : null;
            if (!item) return { ok: false, reason: 'name create failed' };
            nts.addOption(item);
            nts.setValue(item.id);
            const setVal = (sel, v) => {
                const el = document.querySelector(sel);
                if (el && v != null && v !== '') {
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            };
            setVal('#PurchaseClientRegNo', reg);
            setVal('#PurchaseClientVatNo', vat);
            setVal('#PurchaseClientAddressStreet', address);
            const cc = String(country || '').trim().toUpperCase();
            if (cc) {
                const cts = document.querySelector('#PurchaseClientAddressCountry')?.tomselect;
                if (cts) { cts.addOption({ id: cc, text: cc }); cts.setValue(cc); }
            }
            return { ok: true };
        }
        """,
        {"name": name, "reg": reg, "vat": vat, "country": country, "address": address},
    )
    if not outcome or not outcome.get("ok"):
        reason = (outcome or {}).get("reason", "unknown")
        raise RuntimeError(f"Could not attach foreign supplier '{name}' ({reason})")

    _submit_supplier_offcanvas(page)


def _fill_supplier(page: Page, supplier_reg_code: str, sender_name: str = "") -> None:
    reg_code = str(supplier_reg_code or "").strip()
    if not reg_code:
        raise RuntimeError("Supplier registration code is missing")

    # First whitespace-delimited word of the supplier name, used to narrow the
    # saved-client lookup if the Äriregister refresh fails.
    name_parts = str(sender_name or "").strip().split()
    name_token = name_parts[0] if name_parts else ""

    try:
        page.locator('button[data-bs-target="#client-form-offcanvas"]').first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not click 'Muuda tarbija andmeid'") from err

    try:
        page.locator("#client-form-offcanvas").first.wait_for(state="visible", timeout=5000)
    except Exception as err:
        raise RuntimeError("Supplier modal did not open") from err

    reg_fields = page.locator("#PurchaseClientRegNo")
    if reg_fields.count() == 0:
        raise RuntimeError("Could not find supplier registration code field in modal")

    # Fill only registration code; do not type into name field.
    reg_field = reg_fields.first
    reg_field.fill(reg_code, timeout=3000)
    reg_field.press("Tab")
    page.wait_for_timeout(200)

    def _wait_for_refresh_outcome(timeout: int) -> str:
        # Race the two possible results of the Äriregister refresh so we react
        # immediately: 'ok' once the form is populated, or 'error' as soon as the
        # "Saan aru" overlay shows (rather than waiting out the full timeout).
        handle = page.wait_for_function(
            """
            (wantedRegCode) => {
                const errBtn = document.querySelector('#client-form-offcanvas .client-overlay-hide-btn');
                if (errBtn && errBtn.offsetParent !== null) return 'error';
                const regEl = document.querySelector('#PurchaseClientRegNo');
                const nameEl = document.querySelector('#PurchaseClientName');
                const sel = document.querySelector('#client-select-offcanvas');
                const reg = String(regEl?.value || '').trim();
                const name = String(nameEl?.value || '').trim();
                const chosen = String(sel?.value || '').trim();
                const wanted = String(wantedRegCode || '').trim();
                const populated = !!reg && reg.includes(wanted) && ((!!name && name !== wanted) || !!chosen);
                return populated ? 'ok' : false;
            }
            """,
            arg=reg_code,
            timeout=timeout,
        )
        return str(handle.json_value())

    used_saved_client = False
    refresh_button = page.locator("#refresh-data-from-register-link")
    if refresh_button.count() > 0 and refresh_button.first.is_visible():
        try:
            refresh_button.first.click(timeout=5000)
        except Exception as err:
            raise RuntimeError("Could not click 'Värskenda andmeid Äriregistrist'") from err

        try:
            outcome = _wait_for_refresh_outcome(15000)
        except Exception:
            outcome = "error"

        if outcome != "ok":
            # Äriregister has no data for this code (e.g. a foreign supplier) and
            # shows a "Saan aru" overlay. Fall back to selecting the supplier
            # from the company's saved clients, matched by registration code.
            if not _try_supplier_refresh_fallback(page, reg_code, name_token):
                raise RuntimeError(
                    "Supplier data was not refreshed and populated from Äriregister"
                )
            used_saved_client = True

    # The Äriregister path keeps only the registration code and lets SimplBooks
    # re-derive the rest on save. When we instead selected a saved client, that
    # client's populated fields must be preserved, so skip the clearing step.
    if not used_saved_client:
        page.evaluate(
            """
        () => {
            const root = document.querySelector('#client-form-offcanvas') || document;
            const reg = root.querySelector('#PurchaseClientRegNo');
            const keepId = reg ? reg.id : 'PurchaseClientRegNo';

            const clearable = root.querySelectorAll('input, textarea');
            for (const field of clearable) {
                const tag = (field.tagName || '').toLowerCase();
                const type = String(field.getAttribute('type') || '').toLowerCase();
                const id = String(field.id || '');

                if (id === keepId) continue;
                if (tag === 'textarea' || ['text', 'email', 'tel', 'url', 'search', 'number', ''].includes(type)) {
                    field.value = '';
                    field.dispatchEvent(new Event('input', { bubbles: true }));
                    field.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
        }
        """
        )

    try:
        page.locator("#client-form-offcanvas-submit").first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not click 'Korras' in supplier modal") from err

    page.wait_for_timeout(500)


def _attach_invoice_pdf(page: Page, pdf_path: Path) -> None:
    if not pdf_path.exists():
        raise RuntimeError(f"Attachment file does not exist: {pdf_path}")

    upload_path = str(pdf_path)

    # Verified live control on SimplBooks purchase form.
    for selector in [
        "#PurchaseCopy",
        'input[name="data[Purchase][copy]"]',
    ]:
        try:
            fields = page.locator(selector)
            if fields.count() == 0:
                continue
            fields.nth(0).set_input_files(upload_path, timeout=5000)
            return
        except Exception:
            continue

    # Fallback: verified attachment trigger controls on the same form.
    for selector in [
        ".choose-attachment-btn",
        "#PurchaseCopyFileinput .fileinput-select",
    ]:
        try:
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                page.locator(selector).first.click(timeout=5000)
            chooser_info.value.set_files(upload_path)
            return
        except Exception:
            continue

    raise RuntimeError("Could not attach invoice PDF on purchase invoice form")


def _fill_row_choice(page: Page, row_index: int, selectors: List[str], value: str) -> bool:
    for selector in selectors:
        try:
            fields = page.locator(selector)
            if fields.count() <= row_index:
                continue
            field = fields.nth(row_index)
            try:
                field.select_option(label=value, timeout=3000)
                return True
            except Exception:
                pass
            try:
                field.fill(value, timeout=3000)
                field.press("Enter")
                return True
            except Exception:
                continue
        except Exception:
            continue
    return False


def _set_row_tomselect_value(page: Page, row_index: int, id_prefix: str, value: str) -> bool:
    controls = page.locator(f'input[id^="{id_prefix}"][id$="-ts-control"]')
    try:
        control_count = int(controls.count())
    except Exception:
        return False
    if control_count <= row_index:
        return False

    control = controls.nth(row_index)
    token = str(value or "").strip().split(" ", 1)[0]
    numeric_token = token.isdigit()

    def _selected_text() -> str:
        try:
            text = control.evaluate(
                """
                (el) => {
                  const wrapper = el.closest('.ts-wrapper');
                  const item = wrapper ? wrapper.querySelector('.item') : null;
                  return (item?.textContent || '').trim();
                }
                """
            )
            return str(text or "")
        except Exception:
            return ""

    try:
        control.click(timeout=3000)
        control.fill(value, timeout=3000)
        control.press("Enter")
        page.wait_for_timeout(150)
    except Exception:
        return False

    selected = _selected_text().lower()
    value_lower = value.lower()
    token_lower = token.lower()
    if value_lower in selected:
        return True

    # Fallback: explicitly click dropdown option by full label or code/token.
    queries: List[str] = [value]
    if numeric_token:
        queries.append(token)

    for query in queries:
        if not query:
            continue
        for selector in [
            f'.ts-dropdown .option:has-text("{query}")',
            f'.ts-dropdown-content .option:has-text("{query}")',
        ]:
            try:
                control.click(timeout=2000)
                control.fill(query, timeout=2000)
                page.wait_for_timeout(120)
                page.locator(selector).first.click(timeout=2000)
                page.wait_for_timeout(120)
                selected = _selected_text().lower()
                if value_lower in selected:
                    return True
                if numeric_token and token_lower and token_lower in selected:
                    return True
            except Exception:
                continue

    return False


def _set_row_tomselect_by_text(
        page: Page,
        row_index: int,
        selector: str,
        target_text: str,
        *,
        allow_token_match: bool,
        silent: bool = True,
) -> bool:
    fields = page.locator(selector)
    try:
        count = int(fields.count())
    except Exception:
        return False
    if count <= row_index:
        return False

    field = fields.nth(row_index)
    target = str(target_text or "").strip().lower()
    if not target:
        return False
    token = target.split(" ", 1)[0]

    try:
        result = field.evaluate(
            """
            (el, args) => {
                const ts = el && el.tomselect ? el.tomselect : null;
                if (!ts) {
                    return { ok: false, selectedText: '' };
                }

                const target = String(args.target || '').trim().toLowerCase();
                const token = String(args.token || '').trim().toLowerCase();
                const allowToken = !!args.allowToken;

                let chosenKey = null;
                for (const [key, opt] of Object.entries(ts.options || {})) {
                    const text = String(opt?.text || opt?.label || '').trim().toLowerCase();
                    if (!text) continue;
                    if (target && text.includes(target)) {
                        chosenKey = key;
                        break;
                    }
                    if (chosenKey === null && allowToken && token && text.includes(token)) {
                        chosenKey = key;
                    }
                }

                if (chosenKey === null) {
                    return { ok: false, selectedText: '' };
                }

                // silent=false fires TomSelect's change event, which updates the
                // original element and lets the app react (e.g. recompute VAT for
                // the chosen VAT type). Account selection stays silent.
                ts.setValue(String(chosenKey), args.silent);

                const selectedValue = ts.getValue();
                const valueKey = Array.isArray(selectedValue) ? selectedValue[0] : selectedValue;
                const selected = ts.options?.[String(valueKey)] || null;
                const selectedText = String(selected?.text || selected?.label || '').trim();

                const selectedLower = selectedText.toLowerCase();
                const ok = (target && selectedLower.includes(target))
                    || (allowToken && token && selectedLower.includes(token));
                return { ok, selectedText };
            }
            """,
            {
                "target": target,
                "token": token,
                "allowToken": allow_token_match,
                "silent": silent,
            },
        )
    except Exception:
        return False

    return bool(result.get("ok", False)) if isinstance(result, dict) else False


def _set_row_select_by_text(
    page: Page,
    row_index: int,
    selector: str,
    target_text: str,
    *,
    allow_token_match: bool,
) -> bool:
    selects = page.locator(selector)
    try:
        count = int(selects.count())
    except Exception:
        return False
    if count <= row_index:
        return False

    select = selects.nth(row_index)
    target = str(target_text or "").strip().lower()
    if not target:
        return False
    token = target.split(" ", 1)[0]

    try:
        options: List[Dict[str, str]] = select.evaluate(
            """
            (el) => Array.from(el.options || []).map((opt) => ({
              value: String(opt.value || ''),
              text: String((opt.textContent || '').trim()),
            }))
            """
        )
    except Exception:
        return False

    chosen_value = ""
    for opt in options:
        text = str(opt.get("text") or "").strip().lower()
        if target in text:
            chosen_value = str(opt.get("value") or "")
            break
    if not chosen_value and allow_token_match and token:
        for opt in options:
            text = str(opt.get("text") or "").strip().lower()
            if token in text:
                chosen_value = str(opt.get("value") or "")
                break

    if not chosen_value:
        return False

    try:
        select.select_option(value=chosen_value, timeout=3000)
    except Exception:
        return False

    try:
        selected_text = select.evaluate(
            """
            (el) => {
              const idx = el.selectedIndex;
              if (idx < 0 || !el.options || !el.options[idx]) return '';
              return String((el.options[idx].textContent || '').trim());
            }
            """
        )
    except Exception:
        return False

    selected = str(selected_text or "").strip().lower()
    if target in selected:
        return True
    return allow_token_match and bool(token) and token in selected


def _fill_purchase_row(page: Page, row_index: int, line: Dict[str, Any], purchase_settings: Dict[str, str] | None = None) -> None:
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

    settings = purchase_settings or {}
    account_code = str(settings.get("account_code") or "").strip()
    vat_profile = str(settings.get("vat_profile") or "").strip()

    if account_code:
        account_set = _set_row_tomselect_by_text(
            page,
            row_index,
            'select[name*="[PurchaseRow][expense_account_id]"]',
            account_code,
            allow_token_match=True,
        )
        if not account_set:
            account_set = _set_row_select_by_text(
                page,
                row_index,
                'select[name*="[PurchaseRow][expense_account_id]"]',
                account_code,
                allow_token_match=True,
            )
        if not account_set:
            raise RuntimeError(f"Could not set konto '{account_code}' for purchase row {row_index}")

    if vat_profile:
        vat_set = _set_row_tomselect_by_text(
            page,
            row_index,
            'input[name*="[PurchaseRow][vat_type_id]"]',
            vat_profile,
            allow_token_match=False,
            silent=False,
        )
        if not vat_set:
            vat_set = _set_row_select_by_text(
                page,
                row_index,
                'select[name*="[PurchaseRow][vat_type_id]"]',
                vat_profile,
                allow_token_match=False,
            )
        if not vat_set:
            raise RuntimeError(f"Could not set KM '{vat_profile}' for purchase row {row_index}")


def _parsed_stem(parsed_name: str) -> str:
    if parsed_name.endswith(".parsed.json"):
        return parsed_name[: -len(".parsed.json")]
    return Path(parsed_name).stem


def _identify_local_pdf_path(parsed_path: Path) -> Path | None:
    stem = _parsed_stem(parsed_path.name)
    for candidate in [
        parsed_path.with_name(f"{stem}.pdf"),
        parsed_path.with_name(f"{stem}.PDF"),
    ]:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


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


def _verify_invoice_saved(page: Page) -> None:
    """Confirm the invoice actually persisted, rather than silently failing.

    A blocked save leaves the browser on the edit form (…/purchases/add), yet the
    code would otherwise report success. A real save redirects to …/purchases/view/<id>.
    """
    try:
        page.wait_for_url("**/purchases/view/**", timeout=15000)
    except Exception as err:
        raise RuntimeError(
            "Invoice was not saved in SimplBooks (still on the edit form after clicking save)"
        ) from err


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
    try:
        page.locator("#account-email-input").fill(user, timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not find username field on SimplBooks login page") from err

    try:
        page.locator("#account-password-input").fill(password, timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not find password field on SimplBooks login page") from err

    try:
        page.locator("#form-submit-btn").click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not find login submit button") from err

    page.wait_for_load_state("networkidle", timeout=15000)


def navigate_to_purchase_invoices(page: Page) -> None:
    try:
        page.locator("#operations").click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not open Tehingud menu") from err

    try:
        page.locator('a[href$="/purchases"]').first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not open Ostuarved page") from err

    page.wait_for_load_state("networkidle", timeout=15000)


def create_invoice(page: Page, parsed: Dict[str, Any], attachment_pdf_path: Path | None = None) -> Dict[str, Any]:
    invoice = parsed.get("invoice_data") or {}
    sender_reg_code = str(invoice.get("sender_reg_code") or "")
    sender_name = str(invoice.get("invoice_sender") or "")
    invoice_number = str(invoice.get("invoice_number") or "")
    invoice_date = str(invoice.get("invoice_date") or "")
    lines = invoice.get("lines") or []

    try:
        page.locator('a[href$="/purchases/add"]').first.click(timeout=5000)
    except Exception as err:
        raise RuntimeError("Could not open new purchase invoice form") from err

    page.wait_for_load_state("domcontentloaded", timeout=10000)

    predefined_client_id = _predefined_supplier_client_id(invoice)
    if predefined_client_id:
        _select_predefined_client(page, predefined_client_id, "Amazon")
    elif _is_foreign_supplier(invoice):
        _fill_foreign_supplier(page, invoice)
    else:
        _fill_supplier(page, sender_reg_code, sender_name)
    _fill(page, "#PurchaseNumber", invoice_number)
    _fill_purchase_dates(page, invoice_date)

    if not isinstance(lines, list) or not lines:
        raise RuntimeError("No purchase rows available in parsed invoice")

    for row_index, line in enumerate(lines):
        if row_index > 0:
            _add_purchase_row(page)
            page.wait_for_timeout(300)
        row_settings = _resolve_purchase_settings(invoice, line)
        _fill_purchase_row(page, row_index, line, row_settings)

    if attachment_pdf_path is not None:
        _attach_invoice_pdf(page, attachment_pdf_path)

    page.wait_for_timeout(300)
    totals_check = _validate_and_adjust_totals(page, invoice)

    _click_save_invoice(page)
    _verify_invoice_saved(page)
    return totals_check


def process_single_invoice(
    parsed_name: str,
    parsed: Dict[str, Any],
    args: argparse.Namespace,
    user: str,
    password: str,
    write_result_fn: Callable[[bool, Dict[str, Any]], None],
    attachment_pdf_bytes: bytes | None = None,
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

    test_file = str(getattr(args, "test_file", "") or "").strip()
    if test_file and parsed_name != test_file:
        write_result_fn(
            True,
            {
                "status": "success",
                "mode": "placeholder-no-submit",
                "parsed_file": parsed_name,
                "message": "Skipped actual SimplBooks creation for non-test file filter (--test-file).",
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
        attachment_tmp: Path | None = None
        if attachment_pdf_bytes:
            with tempfile.NamedTemporaryFile(prefix="simplbooks-attachment-", suffix=".pdf", delete=False) as tmp:
                tmp.write(attachment_pdf_bytes)
                attachment_tmp = Path(tmp.name)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context()
            page = context.new_page()
            login(page, args.base_url, user, password)
            navigate_to_purchase_invoices(page)
            totals_check = create_invoice(page, parsed, attachment_pdf_path=attachment_tmp)
            context.close()
            browser.close()

        if attachment_tmp is not None:
            try:
                attachment_tmp.unlink(missing_ok=True)
            except Exception:
                pass

        if totals_check.get("difference_cents", 0) > 2:
            write_result_fn(
                False,
                {
                    "status": "failed",
                    "mode": "submitted-total-mismatch",
                    "saved_in_simplbooks": True,
                    "parsed_file": parsed_name,
                    "invoice_number": ((parsed.get("invoice_data") or {}).get("invoice_number") or ""),
                    "message": "Invoice was submitted in SimplBooks, but total mismatch exceeded 2 cents.",
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
        # Best-effort cleanup if temp file was created but flow failed before normal cleanup.
        try:
            if "attachment_tmp" in locals() and attachment_tmp is not None:
                attachment_tmp.unlink(missing_ok=True)
        except Exception:
            pass
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


def local_process(args: argparse.Namespace, user: str, password: str) -> None:
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
        local_pdf_path = _identify_local_pdf_path(parsed_path)
        attachment_pdf_bytes = local_pdf_path.read_bytes() if local_pdf_path is not None else None
        process_single_invoice(
            parsed_name=parsed_path.name,
            parsed=parsed,
            args=args,
            user=user,
            password=password,
            write_result_fn=lambda ok, payload, p=parsed_path: write_result(p, ok=ok, payload=payload),
            attachment_pdf_bytes=attachment_pdf_bytes,
        )


def remote_process(args: argparse.Namespace, user: str, password: str) -> None:
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
        invoice_file_id = str(parsed.get("invoice_file_id") or "").strip()
        attachment_pdf_bytes: bytes | None = None
        if invoice_file_id:
            try:
                attachment_pdf_bytes = download_drive_item_content(token, drive_prefix, invoice_file_id)
            except RuntimeError as err:
                print(f"{process_folder}/{parsed_name}: attachment pdf download failed ({err})")

        process_single_invoice(
            parsed_name=parsed_name,
            parsed=parsed,
            args=args,
            user=user,
            password=password,
            attachment_pdf_bytes=attachment_pdf_bytes,
            write_result_fn=lambda ok, payload, folder=process_folder, name=parsed_name: write_onedrive_result(
                token,
                drive_prefix,
                folder,
                name,
                ok,
                payload,
            ),
        )


def main() -> None:
    load_dotenv()
    args = parse_args()

    user = os.getenv("SIMPLBOOKS_USER", "").strip()
    password = os.getenv("SIMPLBOOKS_PASSWORD", "").strip()

    if args.source == "local":
        local_process(args, user, password)
        return

    remote_process(args, user, password)


if __name__ == "__main__":
    main()

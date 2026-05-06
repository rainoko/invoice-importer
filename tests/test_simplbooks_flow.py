#!/usr/bin/env python3
"""Additional flow tests for simplbooks_importer.py coverage."""
# pylint: disable=protected-access
# pyright: reportPrivateUsage=false

import json
from unittest.mock import MagicMock, patch

import pytest

import simplbooks_importer as sbi


class TestOneDriveHelpers:
    def test_load_parsed_invoice_bytes_variants(self):
        data = sbi.load_parsed_invoice_bytes(b'{"a": 1}')
        assert data == {"a": 1}

        with pytest.raises(ValueError, match="not a JSON object"):
            sbi.load_parsed_invoice_bytes(b'[1,2,3]')

    def test_resolve_onedrive_context_wrapper(self):
        args = MagicMock(auth_mode="app", base_path="x", reference_month="202401")
        with patch("simplbooks_importer.resolve_onedrive_context", return_value=("t", "d", ["202401", "202402"])) as fn:
            token, prefix, folders = sbi._resolve_onedrive_context(args)
        assert (token, prefix, folders) == ("t", "d", ["202401", "202402"])
        fn.assert_called_once()

    def test_list_onedrive_parsed_jobs_and_skip_existing(self):
        files = [
            {"id": "1", "name": "a.parsed.json", "file": {}},
            {"id": "2", "name": "a.parsed.success.json", "file": {}},
            {"id": "3", "name": "b.parsed.json", "file": {}},
            {"id": "", "name": "c.parsed.json", "file": {}},
        ]
        with patch("simplbooks_importer.iter_children", return_value=iter(files)):
            jobs = sbi.list_onedrive_parsed_jobs("t", "d", ["202401"])
        assert len(jobs) == 1
        assert jobs[0]["parsed_name"] == "b.parsed.json"

    def test_list_onedrive_parsed_jobs_missing_month(self):
        with patch("simplbooks_importer.iter_children", side_effect=sbi.MonthFolderMissingError("m")):
            jobs = sbi.list_onedrive_parsed_jobs("t", "d", ["202401"])
        assert jobs == []

    def test_write_onedrive_result_uploads_json(self):
        with patch("simplbooks_importer.upload_text_to_folder") as upload:
            sbi.write_onedrive_result("t", "d", "202401/process", "a.parsed.json", True, {"status": "ok"})
        kwargs = upload.call_args.kwargs
        assert kwargs["file_name"].endswith(".success.json")
        payload = json.loads(kwargs["content"])
        assert payload["status"] == "ok"
        assert "written_at" in payload


class TestLocalResultFileHelpers:
    def test_result_paths_and_has_result(self, tmp_path):
        parsed = tmp_path / "invoice.parsed.json"
        success, failed = sbi.result_paths(parsed)
        assert success.name == "invoice.parsed.success.json"
        assert failed.name == "invoice.parsed.failed.json"

        assert sbi.has_result(parsed) is False
        success.write_text("{}", encoding="utf-8")
        assert sbi.has_result(parsed) is True

    def test_write_result_success_and_failure(self, tmp_path):
        parsed = tmp_path / "invoice.parsed.json"
        sbi.write_result(parsed, True, {"status": "ok"})
        sbi.write_result(parsed, False, {"error": "boom"})

        success, failed = sbi.result_paths(parsed)
        ok_payload = json.loads(success.read_text(encoding="utf-8"))
        fail_payload = json.loads(failed.read_text(encoding="utf-8"))

        assert ok_payload["status"] == "ok"
        assert fail_payload["error"] == "boom"
        assert "written_at" in ok_payload
        assert "written_at" in fail_payload

    def test_load_parsed_invoice_file_paths(self, tmp_path):
        parsed = tmp_path / "invoice.parsed.json"
        parsed.write_text('{"invoice_data": {}}', encoding="utf-8")
        assert sbi.load_parsed_invoice(parsed) == {"invoice_data": {}}

        parsed.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="not a JSON object"):
            sbi.load_parsed_invoice(parsed)


class TestNavigationAndCreateInvoice:
    def test_login_submit_button_missing(self):
        page = MagicMock()
        user_field = MagicMock()
        pass_field = MagicMock()
        page.get_by_label.side_effect = [user_field, pass_field]

        with patch("simplbooks_importer._try_click", return_value=False):
            with pytest.raises(RuntimeError, match="login submit button"):
                sbi.login(page, "https://www.simplbooks.ee", "u", "p")

    def test_navigate_to_purchase_invoices_success(self):
        page = MagicMock()
        with patch("simplbooks_importer._try_click", side_effect=[True, True]):
            sbi.navigate_to_purchase_invoices(page)
        page.wait_for_load_state.assert_called_with("networkidle", timeout=15000)

    def test_navigate_to_purchase_invoices_fail_first_menu(self):
        page = MagicMock()
        with patch("simplbooks_importer._try_click", return_value=False):
            with pytest.raises(RuntimeError, match="Could not open Tehingud"):
                sbi.navigate_to_purchase_invoices(page)

    def test_navigate_to_purchase_invoices_fail_second_menu(self):
        page = MagicMock()
        with patch("simplbooks_importer._try_click", side_effect=[True, False]):
            with pytest.raises(RuntimeError, match="Could not open Ostuarved"):
                sbi.navigate_to_purchase_invoices(page)

    def test_create_invoice_requires_rows(self):
        page = MagicMock()
        parsed = {
            "invoice_data": {
                "invoice_sender": "S",
                "invoice_number": "INV1",
                "invoice_date": "2024-01-01",
                "lines": [],
            }
        }
        with patch("simplbooks_importer._try_click", return_value=True), \
            patch("simplbooks_importer._fill_supplier"), \
            patch("simplbooks_importer._fill"):
            with pytest.raises(RuntimeError, match="No purchase rows"):
                sbi.create_invoice(page, parsed)

    def test_create_invoice_open_form_failure(self):
        page = MagicMock()
        parsed = {"invoice_data": {"lines": [{"description": "x", "price_without_vat": 1}]}}
        with patch("simplbooks_importer._try_click", return_value=False):
            with pytest.raises(RuntimeError, match="Could not open new purchase invoice form"):
                sbi.create_invoice(page, parsed)

    def test_create_invoice_happy_path_two_rows(self):
        page = MagicMock()
        parsed = {
            "invoice_data": {
                "invoice_sender": "Supplier",
                "invoice_number": "INV2",
                "invoice_date": "2024-01-02",
                "lines": [
                    {"description": "A", "price_without_vat": 1},
                    {"description": "B", "price_without_vat": 2},
                ],
            }
        }
        with patch("simplbooks_importer._try_click", return_value=True), \
            patch("simplbooks_importer._fill_supplier") as fill_supplier, \
            patch("simplbooks_importer._fill") as fill, \
            patch("simplbooks_importer._fill_purchase_row") as fill_row, \
            patch("simplbooks_importer._add_purchase_row") as add_row, \
            patch("simplbooks_importer._validate_and_adjust_totals", return_value={"difference_cents": 0}) as totals, \
            patch("simplbooks_importer._click_save_invoice"):
            out = sbi.create_invoice(page, parsed)

        assert out["difference_cents"] == 0
        fill_supplier.assert_called_once()
        assert fill.call_count >= 4
        assert fill_row.call_count == 2
        add_row.assert_called_once()
        totals.assert_called_once()


class TestProcessSingleInvoiceAndMain:
    def test_process_single_invoice_no_submit_mode_marks_success(self):
        args = MagicMock(
            no_submit=True,
            test_file="target.parsed.json",
            headless=True,
            base_url="https://www.simplbooks.ee",
        )
        recorder = []

        sbi.process_single_invoice(
            parsed_name="target.parsed.json",
            parsed={},
            args=args,
            user="",
            password="",
            write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
        )

        assert recorder and recorder[0][0] is True
        assert recorder[0][1]["mode"] == "dry-run-no-submit"

    def test_process_single_invoice_placeholder(self):
        args = MagicMock(test_file="target.parsed.json", headless=True, base_url="https://www.simplbooks.ee")
        recorder = []

        sbi.process_single_invoice(
            parsed_name="other.parsed.json",
            parsed={},
            args=args,
            user="u",
            password="p",
            write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
        )

        assert recorder and recorder[0][0] is True
        assert recorder[0][1]["mode"] == "placeholder-no-submit"

    def test_process_single_invoice_missing_credentials(self):
        args = MagicMock(test_file="target.parsed.json", headless=True, base_url="https://www.simplbooks.ee")
        recorder = []

        sbi.process_single_invoice(
            parsed_name="target.parsed.json",
            parsed={},
            args=args,
            user="",
            password="",
            write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
        )

        assert recorder and recorder[0][0] is False
        assert "Missing SIMPLBOOKS_USER" in recorder[0][1]["error"]

    def test_process_single_invoice_submitted_mismatch(self):
        args = MagicMock(test_file="target.parsed.json", headless=True, base_url="https://www.simplbooks.ee")
        parsed = {"invoice_data": {"invoice_number": "INV-1"}}
        recorder = []

        play = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        play.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        cm = MagicMock()
        cm.__enter__.return_value = play
        cm.__exit__.return_value = False

        with patch("simplbooks_importer.sync_playwright", return_value=cm), \
            patch("simplbooks_importer.login"), \
            patch("simplbooks_importer.navigate_to_purchase_invoices"), \
            patch("simplbooks_importer.create_invoice", return_value={"difference_cents": 5}):
            sbi.process_single_invoice(
                parsed_name="target.parsed.json",
                parsed=parsed,
                args=args,
                user="u",
                password="p",
                write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
            )

        assert recorder and recorder[0][0] is False
        assert recorder[0][1]["mode"] == "submitted-total-mismatch"

    def test_process_single_invoice_success(self):
        args = MagicMock(test_file="target.parsed.json", headless=True, base_url="https://www.simplbooks.ee")
        parsed = {"invoice_data": {"invoice_number": "INV-2"}}
        recorder = []

        play = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        play.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        cm = MagicMock()
        cm.__enter__.return_value = play
        cm.__exit__.return_value = False

        with patch("simplbooks_importer.sync_playwright", return_value=cm), \
            patch("simplbooks_importer.login"), \
            patch("simplbooks_importer.navigate_to_purchase_invoices"), \
            patch("simplbooks_importer.create_invoice", return_value={"difference_cents": 1}):
            sbi.process_single_invoice(
                parsed_name="target.parsed.json",
                parsed=parsed,
                args=args,
                user="u",
                password="p",
                write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
            )

        assert recorder and recorder[0][0] is True
        assert recorder[0][1]["mode"] == "submitted"

    def test_process_single_invoice_exception_path(self):
        args = MagicMock(test_file="target.parsed.json", headless=True, base_url="https://www.simplbooks.ee")
        recorder = []

        cm = MagicMock()
        cm.__enter__.side_effect = RuntimeError("browser fail")
        cm.__exit__.return_value = False

        with patch("simplbooks_importer.sync_playwright", return_value=cm):
            sbi.process_single_invoice(
                parsed_name="target.parsed.json",
                parsed={},
                args=args,
                user="u",
                password="p",
                write_result_fn=lambda ok, payload: recorder.append((ok, payload)),
            )

        assert recorder and recorder[0][0] is False
        assert "browser fail" in recorder[0][1]["error"]

    def test_main_local_processes_unhandled_files(self, tmp_path):
        parsed_path = tmp_path / "x.parsed.json"
        parsed_path.write_text("{}", encoding="utf-8")

        args = MagicMock(
            source="local",
            input_dir=str(tmp_path),
            auth_mode="app",
            base_path=None,
            reference_month=None,
            headless=True,
            base_url="https://www.simplbooks.ee",
        )

        with patch("simplbooks_importer.parse_args", return_value=args), \
            patch("simplbooks_importer.load_dotenv"), \
            patch("simplbooks_importer.list_parsed_files", return_value=[parsed_path]), \
            patch("simplbooks_importer.has_result", return_value=False), \
            patch("simplbooks_importer.load_parsed_invoice", return_value={}), \
            patch("simplbooks_importer.process_single_invoice") as proc, \
            patch.dict("os.environ", {"SIMPLBOOKS_USER": "u", "SIMPLBOOKS_PASSWORD": "p"}, clear=True):
            sbi.main()

        proc.assert_called_once()

    def test_main_local_skips_existing_result(self, tmp_path):
        parsed_path = tmp_path / "x.parsed.json"
        parsed_path.write_text("{}", encoding="utf-8")

        args = MagicMock(
            source="local",
            input_dir=str(tmp_path),
            auth_mode="app",
            base_path=None,
            reference_month=None,
            headless=True,
            base_url="https://www.simplbooks.ee",
        )

        with patch("simplbooks_importer.parse_args", return_value=args), \
            patch("simplbooks_importer.load_dotenv"), \
            patch("simplbooks_importer.list_parsed_files", return_value=[parsed_path]), \
            patch("simplbooks_importer.has_result", return_value=True), \
            patch("simplbooks_importer.process_single_invoice") as proc, \
            patch.dict("os.environ", {"SIMPLBOOKS_USER": "u", "SIMPLBOOKS_PASSWORD": "p"}, clear=True):
            sbi.main()

        proc.assert_not_called()

    def test_main_onedrive_path_with_job(self):
        args = MagicMock(
            source="onedrive",
            input_dir=".",
            auth_mode="app",
            base_path=None,
            reference_month=None,
            headless=True,
            base_url="https://www.simplbooks.ee",
        )

        jobs = [{"parsed_name": "x.parsed.json", "process_folder": "202401/process", "item_id": "id1"}]

        with patch("simplbooks_importer.parse_args", return_value=args), \
            patch("simplbooks_importer.load_dotenv"), \
            patch("simplbooks_importer._resolve_onedrive_context", return_value=("t", "d", ["202401", "202402"])), \
            patch("simplbooks_importer.list_onedrive_parsed_jobs", return_value=jobs), \
            patch("simplbooks_importer.download_drive_item_content", return_value=b"{}"), \
            patch("simplbooks_importer.load_parsed_invoice_bytes", return_value={}), \
            patch("simplbooks_importer.process_single_invoice") as proc, \
            patch.dict("os.environ", {"SIMPLBOOKS_USER": "u", "SIMPLBOOKS_PASSWORD": "p"}, clear=True):
            sbi.main()

        proc.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

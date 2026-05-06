#!/usr/bin/env python3
"""Coverage-focused tests for API and orchestration paths in onedrive_reader.py."""
# pylint: disable=protected-access

from unittest.mock import MagicMock, patch

import pytest
import requests

import onedrive_reader as odr


def _resp(status_code=200, text="", json_data=None, content=b""):
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    mock.content = content
    if isinstance(json_data, Exception):
        mock.json.side_effect = json_data
    else:
        mock.json.return_value = json_data if json_data is not None else {}
    return mock


class TestAuthAndGraphHelpers:
    def test_get_app_token_success(self):
        app = MagicMock()
        app.acquire_token_for_client.return_value = {"access_token": "tok"}
        with patch("onedrive_reader.msal.ConfidentialClientApplication", return_value=app):
            token = odr.get_app_token("tenant", "client", "secret")
        assert token == "tok"

    def test_get_app_token_failure(self):
        app = MagicMock()
        app.acquire_token_for_client.return_value = {"error_description": "bad creds"}
        with patch("onedrive_reader.msal.ConfidentialClientApplication", return_value=app):
            with pytest.raises(RuntimeError, match="Failed to acquire app token"):
                odr.get_app_token("tenant", "client", "secret")

    def test_get_delegated_token_device_flow_missing_code(self):
        app = MagicMock()
        app.initiate_device_flow.return_value = {"message": "x"}
        with patch("onedrive_reader.msal.PublicClientApplication", return_value=app):
            with pytest.raises(RuntimeError, match="Failed to start device flow"):
                odr.get_delegated_token("tenant", "client")

    def test_get_delegated_token_success(self):
        app = MagicMock()
        app.initiate_device_flow.return_value = {"user_code": "abc", "message": "sign in"}
        app.acquire_token_by_device_flow.return_value = {"access_token": "tok2"}
        with patch("onedrive_reader.msal.PublicClientApplication", return_value=app):
            token = odr.get_delegated_token("tenant", "client")
        assert token == "tok2"

    def test_iter_children_pagination(self):
        first = _resp(
            json_data={
                "value": [{"id": "1", "name": "a.pdf", "file": {}}],
                "@odata.nextLink": "https://next.page",
            }
        )
        second = _resp(json_data={"value": [{"id": "2", "name": "b.pdf", "file": {}}]})
        with patch("onedrive_reader.requests.get", side_effect=[first, second]):
            items = list(odr.iter_children("t", "202401", "https://graph"))
        assert [x["id"] for x in items] == ["1", "2"]

    def test_iter_children_missing_folder(self):
        not_found = _resp(status_code=404, text="itemNotFound")
        with patch("onedrive_reader.requests.get", return_value=not_found):
            with pytest.raises(odr.MonthFolderMissingError):
                list(odr.iter_children("t", "missing", "https://graph"))

    def test_iter_children_http_error(self):
        bad = _resp(status_code=500, text="boom")
        with patch("onedrive_reader.requests.get", return_value=bad):
            with pytest.raises(RuntimeError, match="Graph API error 500"):
                list(odr.iter_children("t", "folder", "https://graph"))

    def test_download_drive_item_content_success(self):
        ok = _resp(status_code=200, content=b"abc")
        with patch("onedrive_reader.requests.get", return_value=ok):
            data = odr.download_drive_item_content("t", "https://graph", "item-id")
        assert data == b"abc"

    def test_download_drive_item_content_failure(self):
        bad = _resp(status_code=403, text="forbidden")
        with patch("onedrive_reader.requests.get", return_value=bad):
            with pytest.raises(RuntimeError, match="Failed to download item"):
                odr.download_drive_item_content("t", "https://graph", "item-id")

    def test_upload_text_to_folder_success_and_root_path(self):
        ok = _resp(status_code=201)
        with patch("onedrive_reader.requests.put", return_value=ok) as put_mock:
            odr.upload_text_to_folder("t", "https://graph", "", "f.txt", "x", "text/plain")
        called_url = put_mock.call_args[0][0]
        assert "root:/f.txt:" in called_url

    def test_upload_text_to_folder_failure(self):
        bad = _resp(status_code=500, text="err")
        with patch("onedrive_reader.requests.put", return_value=bad):
            with pytest.raises(RuntimeError, match="Failed to upload"):
                odr.upload_text_to_folder("t", "https://graph", "proc", "f.txt", "x", "text/plain")


class TestChatApiHelpers:
    def test_call_chat_api_success(self):
        ok = _resp(status_code=200, json_data={"choices": []})
        with patch("onedrive_reader.requests.post", return_value=ok):
            data, reason = odr._call_chat_completions_api("u", "k", {}, "prov")
        assert data == {"choices": []}
        assert reason == ""

    def test_call_chat_api_timeout(self):
        with patch("onedrive_reader.requests.post", side_effect=requests.exceptions.Timeout()):
            data, reason = odr._call_chat_completions_api("u", "k", {}, "prov")
        assert data is None
        assert reason == "prov_timeout"

    def test_call_chat_api_http_error(self):
        bad = _resp(status_code=429, text="rate")
        with patch("onedrive_reader.requests.post", return_value=bad):
            data, reason = odr._call_chat_completions_api("u", "k", {}, "prov")
        assert data is None
        assert "prov_http_429" in reason

    def test_extract_message_content_list_parts(self):
        content, finish = odr._extract_message_content(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": [
                                {"type": "text", "text": "one"},
                                "two",
                            ]
                        },
                    }
                ]
            }
        )
        assert "one" in content and "two" in content
        assert finish == "stop"

    def test_extract_invoice_data_with_chat_api_retry_then_success(self):
        first = {"choices": [{"finish_reason": "length", "message": {"content": "not json"}}]}
        second = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": '{"invoice_number":"1","invoice_sender":"S","invoice_date":"2024-01-01","lines":[{"description":"d","price_without_vat":1,"price_with_vat":1}],"total_without_vat":1,"total_with_vat":1}'
                    },
                }
            ]
        }
        with patch("onedrive_reader._call_chat_completions_api", side_effect=[(first, ""), (second, "")]):
            parsed, reason = odr._extract_invoice_data_with_chat_api("md", [], "m", "k", "u", "prov")
        assert reason == ""
        assert parsed is not None
        assert parsed["invoice_number"] == "1"

    def test_extract_invoice_data_with_chat_api_parse_failure(self):
        bad = {"choices": [{"finish_reason": "length", "message": {"content": "still bad"}}]}
        with patch("onedrive_reader._call_chat_completions_api", side_effect=[(bad, ""), (bad, "")]):
            parsed, reason = odr._extract_invoice_data_with_chat_api("md", [], "m", "k", "u", "prov")
        assert parsed is None
        assert reason.startswith("json_parse_failed")

    def test_openrouter_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            parsed, reason = odr.extract_invoice_data_with_openrouter("md", [], "model")
        assert parsed is None
        assert reason == "missing_OPENROUTER_API_KEY"

    def test_github_models_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            parsed, reason = odr.extract_invoice_data_with_github_models("md", [], "model")
        assert parsed is None
        assert reason == "missing_GITHUB_MODELS_API_KEY"


class TestLlmDispatchAndTableLoader:
    def test_extract_invoice_data_dispatch_unsupported(self):
        parsed, reason = odr.extract_invoice_data("md", [], "unknown", "u", "m", "c")
        assert parsed is None
        assert reason == "unsupported_llm_provider:unknown"

    def test_extract_invoice_data_dispatch_openrouter(self):
        with patch("onedrive_reader.extract_invoice_data_with_openrouter", return_value=({"ok": 1}, "")) as fn:
            parsed, reason = odr.extract_invoice_data("md", [], "openrouter", "u", "m", "c", openrouter_model="or")
        assert reason == ""
        assert parsed == {"ok": 1}
        fn.assert_called_once()

    def test_load_table_data_for_markdown_success(self):
        md_items = [{"name": "x.tables.json", "id": "tbl1"}]
        with patch("onedrive_reader.download_drive_item_content", return_value=b'[{"page":1}]'):
            out = odr._load_table_data_for_markdown("t", "d", md_items, "x.tables.json")
        assert out == [{"page": 1}]

    def test_load_table_data_for_markdown_non_json(self):
        md_items = [{"name": "x.tables.json", "id": "tbl1"}]
        with patch("onedrive_reader.download_drive_item_content", return_value=b"not-json"):
            out = odr._load_table_data_for_markdown("t", "d", md_items, "x.tables.json")
        assert out == []


class TestProcessMonthFolderAndMain:
    def test_process_month_folder_happy_path(self):
        progress = MagicMock()

        root_items = [{"id": "pdf1", "name": "inv1.pdf", "file": {}}]
        process_scan_initial = []
        process_scan_after = [
            {"id": "md1", "name": "inv1.md", "file": {}},
            {"id": "tbl1", "name": "inv1.tables.json", "file": {}},
        ]
        seen_process_scan = {"seen": False}

        def iter_side_effect(_token, folder_path, _drive_prefix):
            if folder_path.endswith("/process"):
                if not seen_process_scan["seen"]:
                    seen_process_scan["seen"] = True
                    return iter(process_scan_initial)
                return iter(process_scan_after)
            return iter(root_items)

        with patch("onedrive_reader.iter_children", side_effect=iter_side_effect), \
            patch("onedrive_reader.download_drive_item_content", side_effect=[b"%PDF", b"# md", b"[{\"page\":1}]"]), \
            patch("onedrive_reader.extract_pdf_markdown_with_fallback", return_value={"markdown": "# md", "tables": [{"t": 1}], "ocr_used": False}), \
            patch("onedrive_reader.upload_markdown_to_folder"), \
            patch("onedrive_reader.upload_text_to_folder"), \
            patch("onedrive_reader.extract_invoice_data", return_value=({
                "invoice_number": "1",
                "invoice_sender": "S",
                "invoice_date": "2024-01-01",
                "lines": [{"description": "d", "price_without_vat": 1, "price_with_vat": 1}],
                "total_without_vat": 1,
                "total_with_vat": 1,
            }, "")):
            result = odr.process_month_folder(
                token="t",
                drive_prefix="d",
                folder_path="202401",
                llm_provider="ollama",
                ollama_url="u",
                ollama_model="m",
                cerebras_model="c",
                openrouter_model="or",
                github_models_model="gh",
                progress=progress,
                initial_md_count=0,
            )

        assert result["processed"] == 1
        assert result["markdown_processed"] == 1

    def test_main_month_folder_missing_and_runtime_error_paths(self):
        args = MagicMock(auth_mode="app", base_path=None, reference_month=None)

        with patch("onedrive_reader.parse_args", return_value=args), \
            patch("onedrive_reader.ensure_ocr_binary_available"), \
            patch("onedrive_reader.resolve_onedrive_context", return_value=("t", "d", ["202401", "202402"])), \
            patch("onedrive_reader.iter_children", side_effect=[
                odr.MonthFolderMissingError("202401"),
                iter([{"id": "pdf", "name": "a.pdf", "file": {}}]),
                iter([]),
            ]), \
            patch("onedrive_reader.LiveProgress") as lp_cls, \
            patch("onedrive_reader.process_month_folder", side_effect=[RuntimeError("boom")]):
            lp = MagicMock()
            lp_cls.return_value = lp
            odr.main()

        lp.start.assert_called_once()
        lp.finish.assert_called_once()

    def test_process_month_folder_markdown_failure_paths(self):
        progress = MagicMock()

        root_items = [{"id": "pdf1", "name": "inv1.pdf", "file": {}}]
        process_scan = [
            {"id": "", "name": "broken.md", "file": {}},
            {"id": "md_parse", "name": "parse.md", "file": {}},
            {"id": "md_runtime", "name": "runtime.md", "file": {}},
        ]

        def iter_side_effect(_token, folder_path, _drive_prefix):
            if folder_path.endswith("/process"):
                return iter(process_scan)
            return iter(root_items)

        with patch("onedrive_reader.iter_children", side_effect=iter_side_effect), \
            patch("onedrive_reader.download_drive_item_content", side_effect=[b"%PDF", b"bad-md", b"boom-md"]), \
            patch("onedrive_reader.extract_pdf_markdown_with_fallback", return_value={"markdown": "# md", "tables": [], "ocr_used": False}), \
            patch("onedrive_reader.upload_markdown_to_folder"), \
            patch("onedrive_reader.upload_text_to_folder"), \
            patch("onedrive_reader.extract_invoice_data", side_effect=[(None, "json_parse_failed"), RuntimeError("llm down")]):
            result = odr.process_month_folder(
                token="t",
                drive_prefix="d",
                folder_path="202401",
                llm_provider="ollama",
                ollama_url="u",
                ollama_model="m",
                cerebras_model="c",
                openrouter_model="or",
                github_models_model="gh",
                progress=progress,
                initial_md_count=0,
            )

        assert result["processed"] == 1
        assert result["markdown_failed"] == 3

    def test_main_happy_path_totals(self):
        args = MagicMock(auth_mode="app", base_path=None, reference_month=None)

        with patch("onedrive_reader.parse_args", return_value=args), \
            patch("onedrive_reader.ensure_ocr_binary_available"), \
            patch("onedrive_reader.resolve_onedrive_context", return_value=("t", "d", ["202401", "202402"])), \
            patch("onedrive_reader.iter_children", side_effect=[
                iter([{"id": "p1", "name": "a.pdf", "file": {}}]),
                iter([]),
                iter([{"id": "p2", "name": "b.pdf", "file": {}}]),
                iter([]),
            ]), \
            patch("onedrive_reader.process_month_folder", side_effect=[
                {
                    "processed": 1,
                    "skipped": 0,
                    "skipped_existing": 0,
                    "markdown_processed": 1,
                    "markdown_skipped_existing": 0,
                    "markdown_failed": 0,
                },
                {
                    "processed": 0,
                    "skipped": 1,
                    "skipped_existing": 1,
                    "markdown_processed": 0,
                    "markdown_skipped_existing": 1,
                    "markdown_failed": 1,
                },
            ]), \
            patch("onedrive_reader.LiveProgress") as lp_cls:
            lp = MagicMock()
            lp_cls.return_value = lp
            odr.main()

        lp.start.assert_called_once()
        lp.finish.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

#!/usr/bin/env python3
"""Additional coverage tests for text/PDF/LLM branches in onedrive_reader.py."""
# pylint: disable=protected-access

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import onedrive_reader as odr


class TestEnvAndContextHelpers:
    def test_require_env_missing_exits(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(SystemExit):
                odr._require_env("MISSING")

    def test_ensure_ocr_binary_missing(self):
        with patch("onedrive_reader.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="tesseract"):
                odr.ensure_ocr_binary_available()

    def test_graph_drive_prefix_variants(self):
        assert odr._graph_drive_prefix("delegated", None).endswith("/me/drive")
        assert odr._graph_drive_prefix("app", "drv").endswith("/drives/drv")
        with pytest.raises(ValueError):
            odr._graph_drive_prefix("app", None)

    def test_resolve_onedrive_context_app_base_root_prefix(self):
        with patch("onedrive_reader._require_env", side_effect=["tenant", "client", "secret", "drive"]), \
            patch("onedrive_reader.get_app_token", return_value="tok"), \
            patch("onedrive_reader.month_folder_names", return_value=["202401", "202402"]), \
            patch.dict("os.environ", {"FOLDER_BASE_PATH": "root/Invoices"}, clear=True):
            token, prefix, folders = odr.resolve_onedrive_context("app", None, None)
        assert token == "tok"
        assert prefix.endswith("/drives/drive")
        assert folders == ["Invoices/202401", "Invoices/202402"]


class TestPdfAndTextHelpers:
    def test_pdf_bytes_to_markdown(self):
        p1 = MagicMock()
        p1.extract_text.return_value = "hello"
        p2 = MagicMock()
        p2.extract_text.return_value = "world"
        reader = MagicMock()
        reader.pages = [p1, p2]
        with patch("onedrive_reader.PdfReader", return_value=reader):
            md = odr.pdf_bytes_to_markdown(b"pdf")
        assert "## Page 1" in md and "hello" in md and "world" in md

    def test_clean_cell_and_text_quality(self):
        assert odr._clean_cell(None) == ""
        assert odr._clean_cell(" x ") == "x"
        assert odr._text_quality_score("") == -1.0
        assert odr._text_quality_score("Invoice line one\nInvoice line two") > -1.0

    def test_clean_extracted_text_and_fragmented_layout(self):
        cleaned = odr._clean_extracted_text("\n----\nA1\n\nB2\n")
        assert "A1" in cleaned and "B2" in cleaned
        fragmented = "\n".join(["abc"] * 80)
        assert odr._looks_fragmented_layout(fragmented) is True

    def test_extract_layout_blocks_and_render(self):
        page = MagicMock()
        page.get_text.return_value = [
            (10.0, 20.0, 30.0, 40.0, " First block "),
            (5.0, 10.0, 15.0, 20.0, "Second block"),
            (1.0,),
        ]
        blocks = odr._extract_layout_blocks(page)
        assert len(blocks) == 2
        assert blocks[0]["y0"] <= blocks[1]["y0"]
        rendered = odr._render_layout_blocks_markdown(blocks)
        assert "Layout Blocks" in rendered

    def test_extract_structured_tables(self):
        table_page = MagicMock()
        table_page.extract_tables.return_value = [[ [" a ", None], None, ["b", "c"] ]]
        pdf = MagicMock()
        pdf.pages = [table_page]
        cm = MagicMock()
        cm.__enter__.return_value = pdf
        cm.__exit__.return_value = False
        with patch("onedrive_reader.pdfplumber.open", return_value=cm):
            out = odr.extract_structured_tables(b"pdf")
        assert out and out[0]["page"] == 1

    def test_ocr_page_text_language_fallback(self):
        page = MagicMock()
        pix = SimpleNamespace(width=1, height=1, samples=b"\x00\x00\x00")
        page.get_pixmap.return_value = pix
        t_err = odr.pytesseract.TesseractError(1, "Failed loading language 'est'")
        with patch("onedrive_reader.Image.frombytes", return_value=object()), \
            patch("onedrive_reader.os.getenv", return_value="est,eng"), \
            patch("onedrive_reader.pytesseract.image_to_string", side_effect=[t_err, "ocr text"]):
            text = odr._ocr_page_text(page)
        assert text == "ocr text"

    def test_extract_pdf_markdown_with_fallback_ocr_and_tables(self):
        page = MagicMock()
        doc = MagicMock()
        doc.__iter__.return_value = iter([page])
        doc.close.return_value = None
        with patch("onedrive_reader.fitz.open", return_value=doc), \
            patch("onedrive_reader._extract_layout_blocks", return_value=[]), \
            patch("onedrive_reader._ocr_page_text", return_value="ocr line"), \
            patch("onedrive_reader.extract_structured_tables", return_value=[{"page": 1}]):
            out = odr.extract_pdf_markdown_with_fallback(b"pdf")
        assert out["ocr_used"] is True
        assert out["tables"] == [{"page": 1}]

    def test_extract_pdf_markdown_fitz_exception_uses_pypdf(self):
        p = MagicMock()
        p.extract_text.return_value = "fallback text"
        reader = MagicMock()
        reader.pages = [p]
        with patch("onedrive_reader.fitz.open", side_effect=RuntimeError("bad")), \
            patch("onedrive_reader.PdfReader", return_value=reader), \
            patch("onedrive_reader.extract_structured_tables", side_effect=RuntimeError("no tables")):
            out = odr.extract_pdf_markdown_with_fallback(b"pdf")
        assert "fallback text" in out["markdown"]
        assert not out["tables"]


class TestLlmBranches:
    def test_extract_invoice_data_with_ollama_timeout(self):
        with patch("onedrive_reader.requests.post", side_effect=requests.exceptions.Timeout()):
            parsed, reason = odr.extract_invoice_data_with_ollama("md", [], "http://ollama", "llama")
        assert parsed is None
        assert reason == "ollama_timeout"

    def test_extract_invoice_data_with_ollama_success(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        resp.json.return_value = {
            "response": "{}",
            "done": False,
        }
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.text = ""
        resp2.json.return_value = {
            "response": '{"invoice_number":"1","invoice_sender":"S","invoice_date":"2024-01-01","lines":[{"description":"d","price_without_vat":1,"price_with_vat":1}],"total_without_vat":1,"total_with_vat":1}',
            "done": True,
        }
        with patch("onedrive_reader.requests.post", side_effect=[resp, resp2]):
            parsed, reason = odr.extract_invoice_data_with_ollama("md", [], "http://ollama", "llama")
        assert reason == ""
        assert parsed is not None

    def test_extract_invoice_data_with_cerebras_missing_sdk(self):
        with patch("onedrive_reader.Cerebras", None):
            parsed, reason = odr.extract_invoice_data_with_cerebras("md", [], "model")
        assert parsed is None
        assert reason == "cerebras_sdk_not_installed"

    def test_extract_invoice_data_with_cerebras_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            parsed, reason = odr.extract_invoice_data_with_cerebras("md", [], "model")
        assert parsed is None
        assert reason == "missing_CEREBRAS_API_KEY"

    def test_extract_invoice_data_with_cerebras_request_failure(self):
        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = RuntimeError("api fail")
        with patch.dict("os.environ", {"CEREBRAS_API_KEY": "k"}, clear=True), \
            patch("onedrive_reader.Cerebras", return_value=fake_client):
            parsed, reason = odr.extract_invoice_data_with_cerebras("md", [], "model")
        assert parsed is None
        assert reason.startswith("cerebras_request_failed")

    def test_call_chat_api_request_exception_and_non_json(self):
        with patch("onedrive_reader.requests.post", side_effect=requests.exceptions.RequestException("net")):
            parsed, reason = odr._call_chat_completions_api("u", "k", {}, "prov")
        assert parsed is None and reason.startswith("prov_request_failed")

        bad_json = MagicMock()
        bad_json.status_code = 200
        bad_json.text = "<!doctype html>"
        bad_json.json.side_effect = ValueError("not json")
        with patch("onedrive_reader.requests.post", return_value=bad_json):
            parsed, reason = odr._call_chat_completions_api("u", "k", {}, "prov")
        assert parsed is None and reason.startswith("prov_non_json_response")

    def test_extract_message_content_edge_cases(self):
        content, finish = odr._extract_message_content({"choices": []})
        assert content == "" and finish == ""

        content2, finish2 = odr._extract_message_content({"choices": [{"message": {"content": 123}}]})
        assert content2 == "123" and finish2 == ""

    def test_extract_invoice_data_with_chat_api_failure_variants(self):
        with patch("onedrive_reader._call_chat_completions_api", return_value=(None, "boom")):
            parsed, reason = odr._extract_invoice_data_with_chat_api("md", [], "m", "k", "u", "prov")
        assert parsed is None and reason == "boom"

        first = {"choices": [{"finish_reason": "len", "message": {"content": "bad"}}]}
        with patch("onedrive_reader._call_chat_completions_api", side_effect=[(first, ""), (None, "retryfail")]):
            parsed, reason = odr._extract_invoice_data_with_chat_api("md", [], "m", "k", "u", "prov")
        assert parsed is None and reason == "retryfail"

        good_json = {"choices": [{"message": {"content": '{"invoice_number":"1","invoice_sender":"S","invoice_date":"2024-01-01","lines":[{"description":"d","price_without_vat":1,"price_with_vat":1}],"total_without_vat":1,"total_with_vat":1}'}}]}
        with patch("onedrive_reader._call_chat_completions_api", return_value=(good_json, "")), \
            patch("onedrive_reader.is_complete_invoice_data", return_value=False):
            parsed, reason = odr._extract_invoice_data_with_chat_api("md", [], "m", "k", "u", "prov")
        assert parsed is None and reason == "schema_incomplete"

    def test_github_models_missing_url(self):
        with patch.dict("os.environ", {"GITHUB_MODELS_API_KEY": "k", "GITHUB_MODELS_URL": ""}, clear=True):
            parsed, reason = odr.extract_invoice_data_with_github_models("md", [], "m")
        assert parsed is None
        assert reason == "missing_GITHUB_MODELS_URL"

    def test_load_table_data_missing_id_and_missing_file(self):
        out = odr._load_table_data_for_markdown("t", "d", [{"name": "x.tables.json"}], "x.tables.json")
        assert out == []
        out2 = odr._load_table_data_for_markdown("t", "d", [{"name": "other.tables.json", "id": "1"}], "x.tables.json")
        assert out2 == []

    def test_extract_invoice_data_dispatch_other_providers(self):
        with patch("onedrive_reader.extract_invoice_data_with_cerebras", return_value=({"ok": 1}, "")):
            parsed, reason = odr.extract_invoice_data("md", [], "cerebras", "u", "m", "c")
        assert parsed == {"ok": 1} and reason == ""

        with patch("onedrive_reader.extract_invoice_data_with_ollama", return_value=({"ok": 2}, "")):
            parsed, reason = odr.extract_invoice_data("md", [], "ollama", "u", "m", "c")
        assert parsed == {"ok": 2} and reason == ""

        with patch("onedrive_reader.extract_invoice_data_with_github_models", return_value=({"ok": 3}, "")):
            parsed, reason = odr.extract_invoice_data("md", [], "github", "u", "m", "c", github_models_model="g")
        assert parsed == {"ok": 3} and reason == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

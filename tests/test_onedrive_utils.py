#!/usr/bin/env python3
"""Unit and integration tests for onedrive_reader.py"""
# pylint: disable=protected-access
# pyright: reportPrivateUsage=false

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from onedrive_reader import (
    month_folder_names,
    parse_json_from_ollama_response,
    is_complete_invoice_data,
    _parse_decimal,
    _snap_to_source_amount,
    _extract_ee_kmkr,
    _snap_sender_name,
    normalize_invoice_data,
    _extract_source_numbers,
    _extract_source_companies,
    _ascii_clean,
    LiveProgress,
)


class TestMonthFolderNames:
    """Test month_folder_names() function"""

    def test_with_valid_reference_month(self):
        """Test with valid YYYYMM format"""
        result = month_folder_names("202604")
        assert result == ["202603", "202604"]

    def test_with_january(self):
        """Test January rolls back to previous year"""
        result = month_folder_names("202401")
        assert result == ["202312", "202401"]

    def test_with_december(self):
        """Test December transitions correctly"""
        result = month_folder_names("202412")
        assert result == ["202411", "202412"]

    def test_invalid_format_too_short(self):
        """Test invalid format raises ValueError"""
        with pytest.raises(ValueError, match="YYYYMM format"):
            month_folder_names("2026")

    def test_invalid_format_non_digit(self):
        """Test non-digit raises ValueError"""
        with pytest.raises(ValueError, match="YYYYMM format"):
            month_folder_names("2026ab")

    def test_invalid_month(self):
        """Test month > 12 raises ValueError"""
        with pytest.raises(ValueError, match="month must be between"):
            month_folder_names("202613")

    def test_invalid_month_zero(self):
        """Test month < 1 raises ValueError"""
        with pytest.raises(ValueError, match="month must be between"):
            month_folder_names("202600")

    def test_with_none_returns_current_and_previous(self):
        """Test with None uses current date"""
        result = month_folder_names(None)
        assert len(result) == 2
        # Should be [prev, current]
        assert len(result[0]) == 6
        assert len(result[1]) == 6


class TestParseJsonFromOllamaResponse:
    """Test parse_json_from_ollama_response() function"""

    def test_valid_json_string(self):
        """Test with valid JSON"""
        text = '{"key": "value", "number": 42}'
        result = parse_json_from_ollama_response(text)
        assert result == {"key": "value", "number": 42}

    def test_json_with_whitespace(self):
        """Test JSON with leading/trailing whitespace"""
        text = '  {"key": "value"}  '
        result = parse_json_from_ollama_response(text)
        assert result == {"key": "value"}

    def test_json_embedded_in_text(self):
        """Test JSON embedded in surrounding text"""
        text = 'Here is the result: {"invoice": "INV001", "total": 100} Done.'
        result = parse_json_from_ollama_response(text)
        assert result == {"invoice": "INV001", "total": 100}

    def test_empty_string_returns_none(self):
        """Test empty string returns None"""
        result = parse_json_from_ollama_response("")
        assert result is None

    def test_whitespace_only_returns_none(self):
        """Test whitespace-only string returns None"""
        result = parse_json_from_ollama_response("   ")
        assert result is None

    def test_invalid_json_returns_none(self):
        """Test invalid JSON returns None"""
        result = parse_json_from_ollama_response("not valid json")
        assert result is None

    def test_json_array_returns_none(self):
        """Test JSON array (not dict) returns None"""
        result = parse_json_from_ollama_response("[1, 2, 3]")
        assert result is None

    def test_nested_json(self):
        """Test nested JSON structure"""
        text = '{"invoice": {"number": "INV001", "total": 100}}'
        result = parse_json_from_ollama_response(text)
        assert result == {"invoice": {"number": "INV001", "total": 100}}


class TestIsCompleteInvoiceData:
    """Test is_complete_invoice_data() function"""

    def test_complete_invoice(self):
        """Test valid complete invoice"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": [{"description": "Item", "price": 100}],
            "total_without_vat": 100,
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is True

    def test_missing_invoice_number(self):
        """Test missing invoice_number"""
        data = {
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": [{"description": "Item"}],
            "total_without_vat": 100,
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is False

    def test_missing_lines(self):
        """Test missing lines"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "total_without_vat": 100,
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is False

    def test_empty_lines_array(self):
        """Test empty lines array"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": [],
            "total_without_vat": 100,
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is False

    def test_lines_not_array(self):
        """Test lines not being a list"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": "not a list",
            "total_without_vat": 100,
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is False

    def test_missing_total_without_vat(self):
        """Test missing total_without_vat"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": [{"description": "Item"}],
            "total_with_vat": 120,
        }
        assert is_complete_invoice_data(data) is False

    def test_missing_total_with_vat(self):
        """Test missing total_with_vat"""
        data = {
            "invoice_number": "INV001",
            "invoice_sender": "Company A",
            "invoice_date": "2024-01-01",
            "lines": [{"description": "Item"}],
            "total_without_vat": 100,
        }
        assert is_complete_invoice_data(data) is False


class TestParseDecimal:
    """Test _parse_decimal() function"""

    def test_decimal_input(self):
        """Test with Decimal input"""
        result = _parse_decimal(Decimal("100.50"))
        assert result == Decimal("100.50")

    def test_int_input(self):
        """Test with int input"""
        result = _parse_decimal(100)
        assert result == Decimal("100")

    def test_float_input(self):
        """Test with float input"""
        result = _parse_decimal(100.50)
        assert result == Decimal("100.5")

    def test_string_with_dot(self):
        """Test string with dot separator"""
        result = _parse_decimal("100.50")
        assert result == Decimal("100.50")

    def test_string_with_comma(self):
        """Test string with comma separator (European format)"""
        result = _parse_decimal("100,50")
        assert result == Decimal("100.50")

    def test_string_with_spaces(self):
        """Test string with spaces - spaces are removed"""
        result = _parse_decimal("100 50")
        # Spaces are removed, so "100 50" -> "10050" which matches r"-?\d+(?:\.\d+)?"
        assert result == Decimal("10050")

    def test_string_with_nbsp(self):
        """Test string with non-breaking space - space is removed"""
        result = _parse_decimal("100\u00a050")
        # NBSP is also removed, so "100\u00a050" -> "10050"
        assert result == Decimal("10050")

    def test_negative_number(self):
        """Test negative number"""
        result = _parse_decimal("-100.50")
        assert result == Decimal("-100.50")

    def test_empty_string_returns_none(self):
        """Test empty string returns None"""
        result = _parse_decimal("")
        assert result is None

    def test_none_input_returns_none(self):
        """Test None input returns None"""
        result = _parse_decimal(None)
        assert result is None

    def test_non_numeric_string_returns_none(self):
        """Test non-numeric string returns None"""
        result = _parse_decimal("abc")
        assert result is None


class TestSnapToSourceAmount:
    """Test _snap_to_source_amount() function"""

    def test_exact_match_in_source(self):
        """Test value that exactly matches source"""
        source = [Decimal("100.00"), Decimal("200.00")]
        result = _snap_to_source_amount(Decimal("100.00"), source)
        assert result == 100.0

    def test_close_to_source_within_tolerance(self):
        """Test value close to source (within 0.05)"""
        source = [Decimal("100.00"), Decimal("200.00")]
        result = _snap_to_source_amount("100.02", source)
        assert result == 100.0  # Should snap to nearest

    def test_far_from_source(self):
        """Test value far from source"""
        source = [Decimal("100.00"), Decimal("200.00")]
        result = _snap_to_source_amount("150.50", source)
        assert result == 150.5  # Should keep original

    def test_empty_source_list(self):
        """Test with empty source list"""
        result = _snap_to_source_amount("100.50", [])
        assert result == 100.5

    def test_none_input_returns_zero(self):
        """Test None input returns 0.0"""
        source = [Decimal("100.00")]
        result = _snap_to_source_amount(None, source)
        assert result == 0.0

    def test_invalid_input_returns_zero(self):
        """Test invalid input returns 0.0"""
        source = [Decimal("100.00")]
        result = _snap_to_source_amount("invalid", source)
        assert result == 0.0

    def test_string_numeric_input(self):
        """Test with string numeric input"""
        source = [Decimal("100.00")]
        result = _snap_to_source_amount("100.50", source)
        assert isinstance(result, float)


class TestExtractEeKmkr:
    """Test _extract_ee_kmkr() function"""

    def test_valid_kmkr(self):
        """Test extracting valid EE KMKR"""
        text = "Company info: EE123456789"
        result = _extract_ee_kmkr(text)
        assert result == "EE123456789"

    def test_lowercase_kmkr(self):
        """Test lowercase KMKR is converted to uppercase"""
        text = "Company info: ee123456789"
        result = _extract_ee_kmkr(text)
        assert result == "EE123456789"

    def test_no_kmkr_in_text(self):
        """Test when no KMKR is present"""
        text = "No kmkr here at all"
        result = _extract_ee_kmkr(text)
        assert result == ""

    def test_kmkr_with_word_boundaries(self):
        """Test KMKR with word boundaries"""
        text = "TAX ID: EE123456789 is valid"
        result = _extract_ee_kmkr(text)
        assert result == "EE123456789"

    def test_multiple_kmkr_returns_first(self):
        """Test multiple KMKR returns first match"""
        text = "First: EE123456789 Second: EE987654321"
        result = _extract_ee_kmkr(text)
        assert result == "EE123456789"

    def test_kmkr_with_wrong_digit_count(self):
        """Test KMKR with wrong digit count is not matched"""
        text = "Invalid: EE12345"
        result = _extract_ee_kmkr(text)
        assert result == ""


class TestSnapSenderName:
    """Test _snap_sender_name() function"""

    def test_exact_match(self):
        """Test exact match in source"""
        result = _snap_sender_name("Company OÜ", ["Company OÜ", "Other AS"])
        assert result == "Company OÜ"

    def test_partial_word_match(self):
        """Test partial word match"""
        result = _snap_sender_name("Company Services OÜ", ["Company OÜ"])
        assert result == "Company OÜ"

    def test_no_match(self):
        """Test no match returns original"""
        result = _snap_sender_name("Unknown Something", ["Company OÜ", "Other AS"])
        assert result == "Unknown Something"  # No word > 2 chars shared

    def test_empty_llm_name(self):
        """Test empty LLM name"""
        result = _snap_sender_name("", ["Company OÜ"])
        assert result == ""

    def test_empty_source_list(self):
        """Test empty source list"""
        result = _snap_sender_name("Company OÜ", [])
        assert result == "Company OÜ"

    def test_case_insensitive_match(self):
        """Test case-insensitive matching"""
        result = _snap_sender_name("company oü", ["Company OÜ"])
        assert result == "Company OÜ"

    def test_filters_short_words(self):
        """Test that short words (<=2 chars) are filtered"""
        result = _snap_sender_name("Big Company Solutions OÜ", ["Company Solutions OÜ"])
        assert result == "Company Solutions OÜ"


class TestAsciiClean:
    """Test _ascii_clean() function"""

    def test_normal_text(self):
        """Test normal ASCII text"""
        result = _ascii_clean("Hello World")
        assert result == "Hello World"

    def test_estonian_characters(self):
        """Test Estonian special characters are preserved"""
        result = _ascii_clean("Äär Öö Üü")
        assert result == "Äär Öö Üü"

    def test_control_characters_removed(self):
        """Test control characters are removed"""
        result = _ascii_clean("Hello\x00World\x01Test")
        # Control chars \x00 and \x01 are removed, leaving "HelloWorldTest"
        assert result == "HelloWorldTest"

    def test_tab_and_newline_preserved(self):
        """Test tab and newline are preserved"""
        result = _ascii_clean("Hello\tWorld\nTest")
        assert result == "Hello World Test"  # Normalized to space

    def test_replacement_char_removed(self):
        """Test replacement character is removed"""
        result = _ascii_clean("Hello\ufffdWorld")
        # Replacement char is removed, leaving "HelloWorld"
        assert result == "HelloWorld"

    def test_multiple_spaces_normalized(self):
        """Test multiple spaces are normalized"""
        result = _ascii_clean("Hello    World")
        assert result == "Hello World"

    def test_leading_trailing_spaces_removed(self):
        """Test leading/trailing spaces are removed"""
        result = _ascii_clean("   Hello World   ")
        assert result == "Hello World"


class TestExtractSourceNumbers:
    """Test _extract_source_numbers() function"""

    def test_extract_decimal_numbers(self):
        """Test extracting decimal numbers"""
        markdown = "Price: 100.50, Total: 150.75"
        result = _extract_source_numbers(markdown, [])
        assert Decimal("100.50") in result
        assert Decimal("150.75") in result

    def test_extract_from_table_data(self):
        """Test extracting from table data"""
        markdown = ""
        # table_data structure: [{"tables": [[[cell1, cell2, ...], [cell1, cell2, ...]]]}]
        # page -> tables -> row -> cell
        table_data = [{"tables": [[[("Amount: 100.50", "Total: 200.75")]]]}]
        result = _extract_source_numbers(markdown, table_data)
        # Should find amounts from cells
        assert Decimal("100.50") in result or Decimal("200.75") in result

    def test_deduplication(self):
        """Test that duplicates are removed"""
        markdown = "100.50, 100.50, 200.00"
        result = _extract_source_numbers(markdown, [])
        # Should have unique values
        assert result.count(Decimal("100.50")) == 1

    def test_no_numbers_returns_empty(self):
        """Test with no numbers returns empty"""
        markdown = "No numbers here"
        result = _extract_source_numbers(markdown, [])
        assert not result

    def test_comma_and_dot_separators(self):
        """Test both comma and dot decimal separators"""
        markdown = "100,50 and 100.50"
        result = _extract_source_numbers(markdown, [])
        assert len(result) > 0


class TestExtractSourceCompanies:
    """Test _extract_source_companies() function"""

    def test_extract_company_with_suffix(self):
        """Test extracting company with legal suffix"""
        markdown = "Invoice from Acme OÜ located in Estonia"
        result = _extract_source_companies(markdown)
        assert any("Acme OÜ" in company for company in result)

    def test_extract_multiple_companies(self):
        """Test extracting multiple companies"""
        markdown = "From Acme OÜ and Beta AS to Gamma Ltd"
        result = _extract_source_companies(markdown)
        assert len(result) >= 2

    def test_deduplication_case_insensitive(self):
        """Test deduplication is case-insensitive"""
        markdown = "Company OÜ and company oü"
        result = _extract_source_companies(markdown)
        # Should have only one entry
        unique_lower = set(c.lower() for c in result)
        assert len(unique_lower) == 1

    def test_no_companies_returns_empty(self):
        """Test with no valid companies returns empty"""
        markdown = "No company here"
        result = _extract_source_companies(markdown)
        assert not result

    def test_preserves_original_casing(self):
        """Test that original casing is preserved"""
        markdown = "Invoice from ACME OÜ"
        result = _extract_source_companies(markdown)
        assert any("ACME OÜ" in company for company in result)


class TestNormalizeInvoiceData:
    """Test normalize_invoice_data() function"""

    def test_basic_normalization(self):
        """Test basic invoice normalization"""
        raw = {
            "invoice_number": "INV001",
            "invoice_sender": "Company OÜ",
            "invoice_date": "2024-01-01",
            "lines": [
                {"description": "Item 1", "price_without_vat": 100.00, "price_with_vat": 120.00}
            ],
            "total_without_vat": 100.00,
            "total_with_vat": 120.00,
        }
        markdown = "This is a normal invoice"
        table_data = []

        result = normalize_invoice_data(raw, markdown, table_data)

        assert result["invoice_number"] == "INV001"
        assert result["invoice_sender"] == "Company OÜ"
        assert len(result["lines"]) == 1

    def test_extract_kmkr_from_markdown(self):
        """Test extracting KMKR from markdown"""
        raw = {"invoice_sender": "Company", "lines": [], "total_without_vat": "0", "total_with_vat": "0"}
        markdown = "TAX ID: EE123456789"
        table_data = []

        result = normalize_invoice_data(raw, markdown, table_data)

        assert result["sender_kmkr_number"] == "EE123456789"

    def test_amazon_business_detection(self):
        """Test Amazon Business detection and name replacement"""
        raw = {
            "invoice_number": "INV001",
            "invoice_sender": "Amazon",
            "sender_kmkr_number": "EE123456789",
            "lines": [],
            "total_without_vat": "0",
            "total_with_vat": "0",
        }
        markdown = "From amazon.com invoice"
        table_data = []

        result = normalize_invoice_data(raw, markdown, table_data)

        assert result["invoice_sender"] == "Amazon Business EU S.a r.l"

    def test_amazon_without_ee_kmkr(self):
        """Test Amazon without EE KMKR uses different name"""
        raw = {
            "invoice_number": "INV001",
            "invoice_sender": "Amazon",
            "lines": [],
            "total_without_vat": "0",
            "total_with_vat": "0",
        }
        markdown = "From amazon.com invoice"
        table_data = []

        result = normalize_invoice_data(raw, markdown, table_data)

        assert result["invoice_sender"] == "Amazon Vahendus"

    def test_normalize_lines(self):
        """Test line normalization"""
        raw = {
            "invoice_number": "INV001",
            "invoice_sender": "Company",
            "lines": [
                {
                    "description": "  Item  1  ",
                    "price_without_vat": 100.50,
                    "price_with_vat": 120.60,
                }
            ],
            "total_without_vat": 100.50,
            "total_with_vat": 120.60,
        }
        markdown = "Invoice data"
        table_data = []

        result = normalize_invoice_data(raw, markdown, table_data)

        assert result["lines"][0]["description"] == "Item 1"
        assert result["lines"][0]["price_without_vat"] == 100.5
        assert result["lines"][0]["is_car_expense"] is False

    def test_normalize_lines_parses_car_expense_marker(self):
        raw = {
            "invoice_number": "INV002",
            "invoice_sender": "Company",
            "lines": [
                {
                    "description": "Fuel",
                    "price_without_vat": 50,
                    "price_with_vat": 60,
                    "is_car_expense": "true",
                },
                {
                    "description": "Office paper",
                    "price_without_vat": 10,
                    "price_with_vat": 12,
                    "is_car_expense": 0,
                },
            ],
            "total_without_vat": 60,
            "total_with_vat": 72,
        }
        result = normalize_invoice_data(raw, "Invoice data", [])
        assert result["lines"][0]["is_car_expense"] is True
        assert result["lines"][1]["is_car_expense"] is False


class TestLiveProgress:
    """Test LiveProgress class"""

    def test_initialization(self):
        """Test LiveProgress initialization"""
        progress = LiveProgress("Test", 10)
        assert progress.label == "Test"
        assert progress.total == 10
        assert progress.done == 0

    def test_add_total(self):
        """Test adding to total"""
        progress = LiveProgress("Test", 10)
        progress.add_total(5)
        assert progress.total == 15

    def test_add_total_negative_ignored(self):
        """Test that negative add_total is ignored"""
        progress = LiveProgress("Test", 10)
        progress.add_total(-5)
        assert progress.total == 10

    def test_advance(self):
        """Test advancing progress"""
        progress = LiveProgress("Test", 10)
        progress.advance()
        assert progress.done == 1

    def test_advance_with_note(self):
        """Test advancing with note"""
        progress = LiveProgress("Test", 10)
        progress.advance("processing")
        assert progress.note == "processing"

    def test_set_note(self):
        """Test setting note"""
        progress = LiveProgress("Test", 10)
        progress.set_note("new note")
        assert progress.note == "new note"

    def test_advance_caps_at_total(self):
        """Test that done caps at total"""
        progress = LiveProgress("Test", 10)
        for _ in range(20):
            progress.advance()
        assert progress.done == 10

    def test_add_total_ignores_non_positive(self):
        progress = LiveProgress("Test", 10)
        progress.add_total(0)
        progress.add_total(-2)
        assert progress.total == 10

    def test_render_without_note_uses_empty_suffix(self):
        progress = LiveProgress("Test", 2)
        progress.note = ""
        with patch("builtins.print") as print_mock:
            progress._render()
        assert print_mock.called

    def test_start_run_and_finish_with_mocked_thread(self):
        progress = LiveProgress("Test", 3)
        thread = MagicMock()

        def run_one_cycle():
            progress._running = False

        thread.start.side_effect = run_one_cycle

        with patch("onedrive_reader.threading.Thread", return_value=thread), \
            patch("onedrive_reader.time.sleep"), \
            patch.object(progress, "_render") as render_mock, \
            patch("builtins.print"):
            progress.start()
            progress.finish("done")

        thread.start.assert_called_once()
        thread.join.assert_called_once_with(timeout=1.0)
        assert render_mock.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

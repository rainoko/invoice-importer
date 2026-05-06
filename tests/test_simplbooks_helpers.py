#!/usr/bin/env python3
"""Unit and integration tests for simplbooks_importer.py"""

from unittest.mock import MagicMock, patch

import pytest

from simplbooks_importer import (
    _format_decimal,
    _parse_decimal,
    _money_round,
    _validate_and_adjust_totals,
    _try_fill,
    _try_click,
    _read_ui_totals,
    _fill_supplier,
    _fill_purchase_row,
    _add_purchase_row,
    _click_save_invoice,
    login,
    parse_args,
    list_parsed_files,
    result_names,
    _login_url,
)


class TestFormatDecimal:
    """Test _format_decimal() function"""

    def test_decimal_value(self):
        """Test with decimal value"""
        result = _format_decimal(100.50)
        assert result == "100,50"

    def test_int_value(self):
        """Test with int value"""
        result = _format_decimal(100)
        assert result == "100,00"

    def test_string_value(self):
        """Test with string value"""
        result = _format_decimal("100.50")
        assert result == "100,50"

    def test_zero_value(self):
        """Test with zero value"""
        result = _format_decimal(0)
        assert result == "0,00"

    def test_negative_value(self):
        """Test with negative value"""
        result = _format_decimal(-100.50)
        assert result == "-100,50"

    def test_none_value(self):
        """Test with None returns 0,00"""
        result = _format_decimal(None)
        assert result == "0,00"

    def test_empty_string(self):
        """Test with empty string returns 0,00"""
        result = _format_decimal("")
        assert result == "0,00"

    def test_large_number(self):
        """Test with large number"""
        result = _format_decimal(10000.99)
        assert result == "10000,99"


class TestParseDecimal:
    """Test _parse_decimal() function"""

    def test_float_value(self):
        """Test with float value"""
        result = _parse_decimal(100.50)
        assert result == 100.50

    def test_int_value(self):
        """Test with int value"""
        result = _parse_decimal(100)
        assert result == 100.0

    def test_string_with_dot(self):
        """Test with string using dot separator"""
        result = _parse_decimal("100.50")
        assert result == 100.50

    def test_string_with_comma(self):
        """Test with string using comma separator"""
        result = _parse_decimal("100,50")
        assert result == 100.50

    def test_string_with_spaces(self):
        """Test with string containing spaces - spaces are stripped"""
        result = _parse_decimal("100 50")
        # The function does .replace(" ", "") so "100 50" becomes "10050"
        assert result == 10050.0

    def test_negative_value(self):
        """Test with negative value"""
        result = _parse_decimal("-100.50")
        assert result == -100.50

    def test_zero_value(self):
        """Test with zero"""
        result = _parse_decimal(0)
        assert result == 0.0

    def test_none_value(self):
        """Test with None returns 0.0"""
        result = _parse_decimal(None)
        assert result == 0.0

    def test_empty_string(self):
        """Test with empty string returns 0.0"""
        result = _parse_decimal("")
        assert result == 0.0

    def test_invalid_string(self):
        """Test with invalid string raises ValueError"""
        # _parse_decimal doesn't catch ValueError for invalid strings
        with pytest.raises(ValueError):
            _parse_decimal("invalid")


class TestMoneyRound:
    """Test _money_round() function"""

    def test_round_half_up(self):
        """Test rounding behavior"""
        result = _money_round(100.555)
        assert result == 100.56

    def test_round_down(self):
        """Test rounding down"""
        result = _money_round(100.544)
        assert result == 100.54

    def test_exact_two_decimals(self):
        """Test value already at two decimals"""
        result = _money_round(100.50)
        assert result == 100.50

    def test_zero(self):
        """Test zero"""
        result = _money_round(0.0)
        assert result == 0.0

    def test_negative(self):
        """Test negative value"""
        result = _money_round(-100.555)
        # round(-100.555 + 1e-9, 2) -> -100.55 (rounds towards zero)
        assert result == -100.55

    def test_epsilon_handling(self):
        """Test that small epsilon is added for rounding"""
        # The function adds 1e-9 to handle floating point errors
        result = _money_round(100.119)
        assert isinstance(result, float)


class TestLoginUrl:
    """Test _login_url() function"""

    def test_secure_simplbooks_url(self):
        """Test secure.simplbooks.com base URL"""
        result = _login_url("https://secure.simplbooks.com/accounts/login")
        assert result == "https://secure.simplbooks.com/accounts/login"

    def test_default_url(self):
        """Test default base URL returns standard login"""
        result = _login_url("https://www.simplbooks.ee")
        assert result == "https://secure.simplbooks.com/accounts/login?locale=et_EE"

    def test_trailing_slash_removed(self):
        """Test trailing slash is removed"""
        result = _login_url("https://secure.simplbooks.com/accounts/login/")
        assert not result.endswith("/")


class TestParseArgs:
    """Test parse_args() function"""

    def test_default_args(self):
        """Test default arguments"""
        with patch("sys.argv", ["simplbooks_importer.py"]):
            args = parse_args()
            assert args.source == "onedrive"
            assert args.auth_mode == "app"
            assert args.input_dir == "."

    def test_local_source(self):
        """Test local source argument"""
        with patch("sys.argv", ["simplbooks_importer.py", "--source", "local"]):
            args = parse_args()
            assert args.source == "local"

    def test_delegated_auth_mode(self):
        """Test delegated auth mode"""
        with patch("sys.argv", ["simplbooks_importer.py", "--auth-mode", "delegated"]):
            args = parse_args()
            assert args.auth_mode == "delegated"

    def test_input_dir_arg(self):
        """Test input directory argument"""
        with patch("sys.argv", ["simplbooks_importer.py", "--input-dir", "/tmp/invoices"]):
            args = parse_args()
            assert args.input_dir == "/tmp/invoices"

    def test_headless_flag(self):
        """Test headless flag"""
        with patch("sys.argv", ["simplbooks_importer.py", "--headless"]):
            args = parse_args()
            assert args.headless is True

    def test_no_submit_flag(self):
        """Test no-submit dry-run flag"""
        with patch("sys.argv", ["simplbooks_importer.py", "--no-submit"]):
            args = parse_args()
            assert args.no_submit is True

    def test_all_args(self):
        """Test multiple arguments"""
        with patch(
            "sys.argv",
            [
                "simplbooks_importer.py",
                "--source",
                "local",
                "--input-dir",
                "/tmp",
                "--test-file",
                "invoice.parsed.json",
            ],
        ):
            args = parse_args()
            assert args.source == "local"
            assert args.input_dir == "/tmp"
            assert args.test_file == "invoice.parsed.json"


class TestListParsedFiles:
    """Test list_parsed_files() function"""

    def test_list_parsed_files(self, tmp_path):
        """Test listing parsed JSON files"""
        (tmp_path / "invoice1.parsed.json").write_text("{}")
        (tmp_path / "invoice2.parsed.json").write_text("{}")
        (tmp_path / "other.txt").write_text("not a parsed file")

        result = list_parsed_files(tmp_path)
        assert len(result) == 2
        assert all(f.suffix == ".json" for f in result)

    def test_sorted_results(self, tmp_path):
        """Test results are sorted"""
        (tmp_path / "z.parsed.json").write_text("{}")
        (tmp_path / "a.parsed.json").write_text("{}")
        (tmp_path / "m.parsed.json").write_text("{}")

        result = list_parsed_files(tmp_path)
        names = [f.name for f in result]
        assert names == sorted(names)

    def test_empty_directory(self, tmp_path):
        """Test empty directory"""
        result = list_parsed_files(tmp_path)
        assert result == []


class TestResultNames:
    """Test result_names() function"""

    def test_success_name(self):
        """Test generating success filename"""
        success, failed = result_names("invoice.parsed.json")
        # The function adds ".parsed." before success/failed
        assert success == "invoice.parsed.success.json"
        assert failed == "invoice.parsed.failed.json"

    def test_invalid_filename_raises(self):
        """Test invalid filename raises ValueError"""
        with pytest.raises(ValueError):
            result_names("invoice.txt")

    def test_complex_name(self):
        """Test complex filename"""
        success, failed = result_names("260416.eurostauto.parsed.json")
        assert success == "260416.eurostauto.parsed.success.json"
        assert failed == "260416.eurostauto.parsed.failed.json"


class TestReadUiTotals:
    """Test _read_ui_totals() function"""

    def test_read_ui_totals(self):
        """Test reading UI totals"""
        page = MagicMock()

        # Mock locator for sum fields
        sum_fields = MagicMock()
        sum_fields.count.return_value = 2
        sum_fields.nth.side_effect = [
            MagicMock(input_value=MagicMock(return_value="100,00")),
            MagicMock(input_value=MagicMock(return_value="50,00")),
        ]

        # Mock VAT field
        vat_field = MagicMock()
        vat_field.input_value.return_value = "36,00"

        # Mock locators
        page.locator.side_effect = lambda sel: sum_fields if "[PurchaseRow][sum]" in sel else vat_field

        result = _read_ui_totals(page)

        assert result["net_sum"] == 150.0
        assert result["vat"] == 36.0
        assert result["total"] == 186.0

    def test_read_ui_totals_with_no_rows(self):
        """Test with no purchase rows"""
        page = MagicMock()

        sum_fields = MagicMock()
        sum_fields.count.return_value = 0

        vat_field = MagicMock()
        vat_field.input_value.return_value = "0,00"

        page.locator.side_effect = lambda sel: sum_fields if "[PurchaseRow][sum]" in sel else vat_field

        result = _read_ui_totals(page)

        assert result["net_sum"] == 0.0
        assert result["vat"] == 0.0
        assert result["total"] == 0.0


class TestValidateAndAdjustTotals:
    """Test _validate_and_adjust_totals() function"""

    def test_totals_match(self):
        """Test when totals already match"""
        page = MagicMock()

        # Mock _read_ui_totals
        with patch("simplbooks_importer._read_ui_totals") as mock_read:
            mock_read.return_value = {"net_sum": 100.0, "vat": 20.0, "total": 120.0}

            invoice = {
                "total_without_vat": "100.00",
                "total_with_vat": "120.00",
            }

            result = _validate_and_adjust_totals(page, invoice)

            assert result["expected_total_with_vat"] == 120.0
            assert result["difference"] == 0.0
            assert result["within_tolerance"] is True

    def test_one_cent_difference_corrected(self):
        """Test one cent difference triggers VAT correction"""
        page = MagicMock()

        # First call returns totals with 1 cent difference
        # Second call after correction returns matching totals
        with patch("simplbooks_importer._read_ui_totals") as mock_read:
            mock_read.side_effect = [
                {"net_sum": 100.0, "vat": 19.99, "total": 119.99},
                {"net_sum": 100.0, "vat": 20.0, "total": 120.0},
            ]

            invoice = {
                "total_without_vat": "100.00",
                "total_with_vat": "120.00",
            }

            result = _validate_and_adjust_totals(page, invoice)

            assert result["vat_corrected"] is True
            assert result["difference"] == 0.0

    def test_two_cent_difference_corrected(self):
        """Test two cent difference triggers VAT correction"""
        page = MagicMock()

        with patch("simplbooks_importer._read_ui_totals") as mock_read:
            mock_read.side_effect = [
                {"net_sum": 100.0, "vat": 19.98, "total": 119.98},
                {"net_sum": 100.0, "vat": 20.0, "total": 120.0},
            ]

            invoice = {
                "total_without_vat": "100.00",
                "total_with_vat": "120.00",
            }

            result = _validate_and_adjust_totals(page, invoice)

            assert result["vat_corrected"] is True
            assert result["within_tolerance"] is True

    def test_large_difference_not_corrected(self):
        """Test large difference is not corrected"""
        page = MagicMock()

        with patch("simplbooks_importer._read_ui_totals") as mock_read:
            mock_read.return_value = {"net_sum": 100.0, "vat": 19.50, "total": 119.50}

            invoice = {
                "total_without_vat": "100.00",
                "total_with_vat": "120.00",
            }

            result = _validate_and_adjust_totals(page, invoice)

            assert result["difference"] == 0.50
            assert result["vat_corrected"] is False
            assert result["within_tolerance"] is False


class TestTryFill:
    """Test _try_fill() function"""

    def test_try_fill_success(self):
        """Test _try_fill succeeds with first selector"""
        page = MagicMock()
        locator_first = MagicMock()
        locator = MagicMock()
        locator.first = locator_first
        page.locator.return_value = locator

        result = _try_fill(page, ["selector1", "selector2"], "value")

        assert result is True
        page.locator.assert_called_with("selector1")
        locator_first.fill.assert_called_with("value", timeout=2500)

    def test_try_fill_fallback(self):
        """Test _try_fill falls back to second selector"""
        page = MagicMock()

        locator1 = MagicMock()
        locator1.fill.side_effect = Exception("Not found")

        locator2 = MagicMock()
        locator2.fill.return_value = None

        page.locator.side_effect = [locator1, locator2]

        result = _try_fill(page, ["selector1", "selector2"], "value")

        assert result is True

    def test_try_fill_all_fail(self):
        """Test _try_fill returns False when all selectors fail"""
        page = MagicMock()

        locator_first = MagicMock()
        locator_first.fill.side_effect = Exception("Not found")
        locator = MagicMock()
        locator.first = locator_first
        page.locator.return_value = locator

        result = _try_fill(page, ["selector1", "selector2"], "value")

        assert result is False


class TestTryClick:
    """Test _try_click() function"""

    def test_try_click_success(self):
        """Test _try_click succeeds"""
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value = locator
        locator.click.return_value = None

        result = _try_click(page, ["selector1"])

        assert result is True

    def test_try_click_all_fail(self):
        """Test _try_click fails when all selectors fail"""
        page = MagicMock()
        locator_first = MagicMock()
        locator_first.click.side_effect = Exception("Not found")
        locator = MagicMock()
        locator.first = locator_first
        page.locator.return_value = locator

        result = _try_click(page, ["selector1"])

        assert result is False


class TestFillSupplier:
    """Test _fill_supplier() function"""

    def test_fill_supplier(self):
        """Test filling supplier field"""
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value = locator

        _fill_supplier(page, "Acme OÜ")

        page.locator.assert_called_with("#client-select-ts-control")
        locator.fill.assert_called_with("Acme OÜ", timeout=5000)
        locator.press.assert_called_with("Enter")


class TestFillPurchaseRow:
    """Test _fill_purchase_row() function"""

    def test_fill_purchase_row(self):
        """Test filling a purchase row"""
        page = MagicMock()

        # Mock name fields
        name_fields = MagicMock()
        name_field = MagicMock()
        name_fields.count.return_value = 2
        name_fields.nth.return_value = name_field

        # Mock quantity fields
        quantity_fields = MagicMock()
        quantity_field = MagicMock()
        quantity_fields.nth.return_value = quantity_field

        # Mock unit fields
        unit_fields = MagicMock()
        unit_field = MagicMock()
        unit_fields.nth.return_value = unit_field

        # Mock sum fields
        sum_fields = MagicMock()
        sum_field = MagicMock()
        sum_fields.count.return_value = 2
        sum_fields.nth.return_value = sum_field

        locators = {
            'input[name*="[PurchaseRow][name]"]': name_fields,
            'input[name*="[PurchaseRow][amount]"]': quantity_fields,
            'input[name*="[PurchaseRow][unit]"]': unit_fields,
            'input[name*="[PurchaseRow][sum]"]': sum_fields,
        }

        page.locator.side_effect = lambda sel: locators.get(sel, MagicMock())

        line = {
            "description": "Item 1",
            "price_without_vat": 100.50,
        }

        _fill_purchase_row(page, 0, line)

        name_field.fill.assert_called_with("Item 1", timeout=5000)
        quantity_field.fill.assert_called_with("1", timeout=5000)
        unit_field.fill.assert_called_with("tk", timeout=5000)
        sum_field.fill.assert_called_with("100,50", timeout=5000)


class TestAddPurchaseRow:
    """Test _add_purchase_row() function"""

    def test_add_purchase_row(self):
        """Test adding a purchase row"""
        page = MagicMock()

        name_fields = MagicMock()
        name_fields.count.return_value = 1

        locator = MagicMock()
        locator.click.return_value = None

        page.locator.side_effect = lambda sel: (
            name_fields if "[PurchaseRow][name]" in sel else locator
        )

        page.wait_for_function = MagicMock()

        with patch("simplbooks_importer._try_click", return_value=True):
            _add_purchase_row(page)

        page.wait_for_function.assert_called_once()


class TestClickSaveInvoice:
    """Test _click_save_invoice() function"""

    def test_click_save_invoice(self):
        """Test clicking save invoice button"""
        page = MagicMock()

        locator = MagicMock()
        locator_first = MagicMock()
        locator.first = locator_first
        locator_first.wait_for.return_value = None
        locator_first.click.return_value = None

        page.locator.return_value = locator

        _click_save_invoice(page)

        locator_first.click.assert_called_with(timeout=5000)

    def test_click_save_invoice_fallback(self):
        """Test fallback when locator fails"""
        page = MagicMock()

        locator = MagicMock()
        locator.first = MagicMock()
        locator.first.wait_for.side_effect = Exception("Timeout")
        locator.first.click.side_effect = Exception("Not interactable")

        page.locator.return_value = locator

        with pytest.raises(RuntimeError):
            _click_save_invoice(page)


class TestLogin:
    """Test login() function"""

    def test_login_success(self):
        """Test successful login"""
        page = MagicMock()

        label = MagicMock()
        page.get_by_label.side_effect = [label, label]

        with patch("simplbooks_importer._login_url", return_value="https://secure.simplbooks.com/accounts/login"):
            with patch("simplbooks_importer._try_fill", return_value=False):
                login(page, "https://www.simplbooks.ee", "user@test.com", "password123")

        page.goto.assert_called_once()
        assert page.get_by_label.call_count >= 1

    def test_login_username_fallback(self):
        """Test login with username field fallback"""
        page = MagicMock()

        label = MagicMock()
        label.fill.side_effect = Exception("Not found")

        page.get_by_label.side_effect = [label, label]

        with patch("simplbooks_importer._login_url", return_value="https://secure.simplbooks.com/accounts/login"):
            with patch("simplbooks_importer._try_fill", return_value=True):
                login(page, "https://www.simplbooks.ee", "user@test.com", "password123")

        page.goto.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

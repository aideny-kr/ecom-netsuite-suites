"""Unit tests for Sheets API styling-request builder used by pricing_to_sheets.

Pure-function tests — no mocks of the Sheets API. Verifies the batchUpdate
request payload matches what we want pricing exports to look like:
- Frozen header row
- Formatted header cells (bold, brand background, white text, center)
- Banded data rows
- Currency number format on currency-code columns (USD, GBP, EUR…)
- Text columns (SKU, item name) left alone
- Auto-resize on every column
"""

from __future__ import annotations

from app.services.sheets_service import _build_pricing_styling_requests


def _find_request(requests: list[dict], key: str) -> dict | None:
    """Return the first request whose top-level key matches `key`, or None."""
    for req in requests:
        if key in req:
            return req
    return None


def _find_all_requests(requests: list[dict], key: str) -> list[dict]:
    return [r for r in requests if key in r]


def _build(
    headers: list[str],
    row_count: int = 5,
    sheet_id: int = 0,
    currency_columns: set[str] | None = None,
) -> list[dict]:
    if currency_columns is None:
        # Broad default lets the existing USD/GBP/EUR tests keep passing without
        # touching every call site. Targeted tests override this set.
        currency_columns = {"USD", "GBP", "EUR", "JPY", "IDR", "BHD"}
    return _build_pricing_styling_requests(
        sheet_id=sheet_id,
        headers=headers,
        row_count=row_count,
        currency_columns=currency_columns,
    )


class TestFreezeHeader:
    def test_freezes_first_row(self):
        requests = _build(["SKU", "USD", "GBP"])
        update = _find_request(requests, "updateSheetProperties")
        assert update is not None, "expected an updateSheetProperties request to freeze rows"
        props = update["updateSheetProperties"]
        assert props["properties"]["gridProperties"]["frozenRowCount"] == 1
        assert "frozenRowCount" in props["fields"]


class TestHeaderFormat:
    def test_header_row_is_bolded(self):
        requests = _build(["SKU", "USD"])
        header_fmt = next(
            r
            for r in _find_all_requests(requests, "repeatCell")
            if r["repeatCell"]["range"].get("startRowIndex") == 0 and r["repeatCell"]["range"].get("endRowIndex") == 1
        )
        text_format = header_fmt["repeatCell"]["cell"]["userEnteredFormat"]["textFormat"]
        assert text_format["bold"] is True

    def test_header_row_has_brand_background(self):
        requests = _build(["SKU", "USD"])
        header_fmt = next(
            r
            for r in _find_all_requests(requests, "repeatCell")
            if r["repeatCell"]["range"].get("startRowIndex") == 0 and r["repeatCell"]["range"].get("endRowIndex") == 1
        )
        bg = header_fmt["repeatCell"]["cell"]["userEnteredFormat"]["backgroundColor"]
        # Brand blue ~ #1A73E8 → R≈0.10, G≈0.45, B≈0.91. Don't pin exact floats — check it's a color.
        assert "red" in bg and "green" in bg and "blue" in bg
        # Not pure white/black.
        assert not (bg["red"] == 1.0 and bg["green"] == 1.0 and bg["blue"] == 1.0)

    def test_header_row_text_is_white(self):
        requests = _build(["SKU", "USD"])
        header_fmt = next(
            r
            for r in _find_all_requests(requests, "repeatCell")
            if r["repeatCell"]["range"].get("startRowIndex") == 0 and r["repeatCell"]["range"].get("endRowIndex") == 1
        )
        fg = header_fmt["repeatCell"]["cell"]["userEnteredFormat"]["textFormat"]["foregroundColor"]
        assert fg == {"red": 1.0, "green": 1.0, "blue": 1.0}

    def test_header_row_is_center_aligned(self):
        requests = _build(["SKU", "USD"])
        header_fmt = next(
            r
            for r in _find_all_requests(requests, "repeatCell")
            if r["repeatCell"]["range"].get("startRowIndex") == 0 and r["repeatCell"]["range"].get("endRowIndex") == 1
        )
        assert header_fmt["repeatCell"]["cell"]["userEnteredFormat"]["horizontalAlignment"] == "CENTER"


class TestBanding:
    def test_includes_banded_range_for_data_rows(self):
        requests = _build(["SKU", "USD"], row_count=10)
        banding = _find_request(requests, "addBanding")
        assert banding is not None, "expected an addBanding request for alternating row colors"
        rng = banding["addBanding"]["bandedRange"]["range"]
        # Banding covers header row 0 + 10 data rows = ends at endRowIndex 11.
        assert rng["startRowIndex"] == 0
        assert rng["endRowIndex"] == 11


class TestColumnTypes:
    def test_currency_code_columns_get_currency_format(self):
        requests = _build(["SKU", "USD", "GBP", "EUR"])
        # Find repeatCell requests targeting the data area (startRowIndex == 1).
        data_formats = [
            r for r in _find_all_requests(requests, "repeatCell") if r["repeatCell"]["range"].get("startRowIndex") == 1
        ]
        # We expect one formatting request per currency column (3) plus possibly the SKU col.
        currency_cols = []
        for r in data_formats:
            fmt = r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat")
            if fmt and fmt.get("type") == "CURRENCY":
                currency_cols.append(r["repeatCell"]["range"]["startColumnIndex"])
        # USD = col 1, GBP = col 2, EUR = col 3.
        assert sorted(currency_cols) == [1, 2, 3]

    def test_sku_column_is_not_currency_formatted(self):
        requests = _build(["SKU", "USD", "GBP"])
        assert requests, "expected non-empty request list"
        data_formats = [
            r for r in _find_all_requests(requests, "repeatCell") if r["repeatCell"]["range"].get("startRowIndex") == 1
        ]
        # Sanity: data-row formats must exist (we format USD + GBP as currency).
        assert data_formats, "expected per-column data-row format requests"
        currency_cols = {
            r["repeatCell"]["range"]["startColumnIndex"]
            for r in data_formats
            if (r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat") or {}).get("type") == "CURRENCY"
        }
        assert 0 not in currency_cols, "SKU column (idx 0) must not be currency-formatted"

    def test_item_name_column_is_text(self):
        # The pricing executor sometimes emits an Item Name column — keep it as text.
        requests = _build(["SKU", "Item Name", "USD"])
        assert requests, "expected non-empty request list"
        data_formats = [
            r for r in _find_all_requests(requests, "repeatCell") if r["repeatCell"]["range"].get("startRowIndex") == 1
        ]
        assert data_formats, "expected per-column data-row format requests"
        currency_cols = {
            r["repeatCell"]["range"]["startColumnIndex"]
            for r in data_formats
            if (r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat") or {}).get("type") == "CURRENCY"
        }
        assert 1 not in currency_cols, "Item Name column (idx 1) must not be currency-formatted"


class TestAutoResize:
    def test_auto_resizes_all_columns(self):
        requests = _build(["SKU", "USD", "GBP", "EUR"])
        resizes = _find_all_requests(requests, "autoResizeDimensions")
        assert resizes, "expected at least one autoResizeDimensions request"
        # Combined resize request should span all 4 columns (start 0, end 4).
        combined = resizes[0]["autoResizeDimensions"]["dimensions"]
        assert combined["dimension"] == "COLUMNS"
        assert combined["startIndex"] == 0
        assert combined["endIndex"] == 4


class TestSheetIdRouting:
    def test_uses_provided_sheet_id(self):
        requests = _build(["SKU", "USD"], sheet_id=42)
        assert requests, "expected non-empty request list"
        for r in requests:
            for body in r.values():
                rng = body.get("range") or body.get("dimensions")
                if rng and "sheetId" in rng:
                    assert rng["sheetId"] == 42
                # addBanding nests range under bandedRange.
                bnd = body.get("bandedRange")
                if bnd and "range" in bnd:
                    assert bnd["range"]["sheetId"] == 42


class TestCurrencyColumnsExplicit:
    """Currency formatting must come from the explicit currency_columns set,
    not from header-string heuristics. Codex flagged the heuristic as buggy:
    QTY/UPC got false-positive currency formatting; IDR got false-negative
    (substring 'id' matched a text keyword).
    """

    def _currency_cols(self, requests: list[dict]) -> set[int]:
        return {
            r["repeatCell"]["range"]["startColumnIndex"]
            for r in _find_all_requests(requests, "repeatCell")
            if r["repeatCell"]["range"].get("startRowIndex") == 1
            and (r["repeatCell"]["cell"]["userEnteredFormat"].get("numberFormat") or {}).get("type") == "CURRENCY"
        }

    def test_qty_column_is_not_currency_when_not_in_set(self):
        # QTY is 3 letters but is "quantity", not a currency. Old heuristic
        # marked any 3-letter alpha header as currency — this regression test
        # locks in that the explicit set is now authoritative.
        requests = _build(
            ["SKU", "USD", "QTY"],
            currency_columns={"USD"},
        )
        assert 2 not in self._currency_cols(requests), "QTY (col 2) must not get currency format"

    def test_upc_column_is_not_currency_when_not_in_set(self):
        requests = _build(
            ["SKU", "USD", "UPC"],
            currency_columns={"USD"},
        )
        assert 2 not in self._currency_cols(requests), "UPC (col 2) must not get currency format"

    def test_idr_column_is_currency_when_in_set(self):
        # IDR was incorrectly classified as text by the old heuristic because
        # the lowercase form contains 'id' (a text keyword). The explicit set
        # is unambiguous.
        requests = _build(
            ["SKU", "USD", "IDR"],
            currency_columns={"USD", "IDR"},
        )
        assert 2 in self._currency_cols(requests), "IDR (col 2) must get currency format"

    def test_currency_match_is_case_insensitive(self):
        # Headers may come in upper- or mixed-case from arbitrary templates.
        requests = _build(
            ["SKU", "Usd", "gbp"],
            currency_columns={"USD", "GBP"},
        )
        cols = self._currency_cols(requests)
        assert 1 in cols and 2 in cols, "case mismatch must not block currency formatting"

    def test_empty_currency_columns_means_no_currency_format(self):
        # If a tenant somehow has zero currencies in the export, the styling
        # layer should not invent any.
        requests = _build(
            ["SKU", "USD", "GBP"],
            currency_columns=set(),
        )
        assert self._currency_cols(requests) == set(), "no currency cols → no currency formats"

    def test_unknown_3_letter_uppercase_header_is_not_currency(self):
        # Catch-all for arbitrary 3-letter codes that aren't currencies:
        # SKU is in text keywords, but ABC, EAN, MPN are random uppercase.
        requests = _build(
            ["EAN", "MPN", "USD"],
            currency_columns={"USD"},
        )
        cols = self._currency_cols(requests)
        assert 0 not in cols and 1 not in cols, "EAN/MPN must not be currency"
        assert 2 in cols, "USD must still be currency"

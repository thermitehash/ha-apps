"""Barcode forms: Kroger's check-digit-less UPC vs. printed UPC-A / EAN-13."""

from __future__ import annotations

import pytest

from pantry_hub.barcodes import (
    check_digit,
    from_kroger_upc,
    grocy_barcode,
    has_valid_check,
    kroger_form,
    variants,
)


@pytest.mark.parametrize(
    ("kroger", "printed"),
    [
        # UPC-A: strip zeros, pad to 11, add the check digit.
        ("0004900002891", "049000028911"),  # Coca-Cola 12 oz
        ("0001111042850", "011110428509"),
        ("0000000004011", "000000040112"),
        ("4900002891", "049000028911"),  # padding missing: same result
        # EAN-13: 12 significant digits + check digit.
        ("0400638133393", "4006381333931"),
        ("0590123412345", "5901234123457"),
    ],
)
def test_check_digit_rebuild(kroger: str, printed: str) -> None:
    assert from_kroger_upc(kroger) == printed
    assert has_valid_check(printed)
    assert kroger_form(printed) == kroger.zfill(13)


def test_check_digit_and_edge_cases() -> None:
    assert check_digit("04900002891") == "1"
    assert check_digit("400638133393") == "1"
    assert from_kroger_upc("") is None
    assert from_kroger_upc("0000") is None
    assert from_kroger_upc("12345678901234") is None  # too long
    assert kroger_form("049000028912") is None  # wrong check digit
    assert not has_valid_check("0004900002891")  # Kroger's form


def test_grocy_barcode_prefers_printed() -> None:
    assert grocy_barcode("049000028911", "0004900002891") == "049000028911"
    assert grocy_barcode(None, "0004900002891") == "049000028911"
    assert grocy_barcode("", "0004900002891") == "049000028911"
    assert grocy_barcode(None, None) is None


def test_variants_cover_every_stored_form() -> None:
    printed = variants("049000028911")
    assert printed[0] == "049000028911"
    assert set(printed) == {"049000028911", "0049000028911", "0004900002891"}
    # Kroger's form can pass the check-digit test by chance; the extra forms
    # that adds are harmless misses, the printed ones must be there.
    assert set(printed) <= set(variants("0004900002891"))
    assert set(printed) <= set(variants("0049000028911"))
    assert variants("4006381333931") == ["4006381333931", "0400638133393"]

"""Product barcode forms: printed UPC-A / EAN-13 vs. Kroger's 13-digit UPC.

Kroger (and Kroger Restock's ``upc`` / ``product_id``) use a 13-digit,
zero-padded code **without** the check digit: ``0004900002891``. The code
printed on the package, which a scanner reads and Grocy stores, has the
check digit: UPC-A ``049000028911`` (or the same as EAN-13,
``0049000028911``).
"""

from __future__ import annotations


def digits(code: str) -> str:
    """Return the digits of ``code`` (spaces, dashes and the like dropped)."""
    return "".join(c for c in code if c.isdigit())


def check_digit(body: str) -> str:
    """GS1 mod-10 check digit for ``body`` (the code without its check digit)."""
    total = 0
    for i, c in enumerate(reversed(body)):
        total += int(c) * (3 if i % 2 == 0 else 1)
    return str((10 - total % 10) % 10)


def has_valid_check(code: str) -> bool:
    """Whether the last digit of an 8/12/13/14-digit code is its check digit."""
    d = digits(code)
    return len(d) in (8, 12, 13, 14) and check_digit(d[:-1]) == d[-1]


def from_kroger_upc(upc: str) -> str | None:
    """Kroger's check-digit-less UPC → the printed barcode.

    Leading zeros are stripped; up to 11 digits is a UPC-A body (padded to 11,
    giving a 12-digit UPC-A), 12 digits an EAN-13 body. ``None`` if it can't be
    either (empty, or longer than 12 significant digits).
    """
    body = digits(upc).lstrip("0")
    if not body:
        return None
    if len(body) <= 11:
        body = body.zfill(11)
    elif len(body) != 12:
        return None
    return body + check_digit(body)


def kroger_form(barcode: str) -> str | None:
    """Convert a printed barcode (with check digit) to Kroger's 13-digit form."""
    d = digits(barcode)
    if not has_valid_check(d):
        return None
    body = d[:-1].lstrip("0")
    return body.zfill(13) if len(body) <= 12 else None


def grocy_barcode(barcode: object, upc: object) -> str | None:
    """Barcode to match in Grocy for a Kroger Restock item.

    ``barcode`` (printed, with check digit) when Kroger Restock sent one;
    otherwise ``upc`` with its check digit rebuilt.
    """
    if isinstance(barcode, str | int) and not isinstance(barcode, bool):
        d = digits(str(barcode))
        if d:
            return d
    if isinstance(upc, str | int) and not isinstance(upc, bool):
        return from_kroger_upc(str(upc))
    return None


def variants(code: str) -> list[str]:
    """Forms of the same product code to try in Grocy, ``code`` first.

    For a printed code: itself, the UPC-A / EAN-13 twin (``0`` prefix added or
    dropped) and Kroger's 13-digit zero-padded form. For a code without a check
    digit (Kroger's form), the printed codes rebuilt from it are added.
    """
    out: list[str] = []

    def add(value: str | None) -> None:
        if value and value not in out:
            out.append(value)

    add(code)
    d = digits(code)
    add(d)
    full: list[str] = []
    if has_valid_check(d):
        full.append(d)
    rebuilt = from_kroger_upc(d)
    if rebuilt and (not has_valid_check(d) or (len(d) == 13 and d.startswith("00"))):
        full.append(rebuilt)
    for f in full:
        add(f)
        if len(f) == 12:
            add("0" + f)  # UPC-A as EAN-13
        elif len(f) == 13 and f.startswith("0"):
            add(f[1:])  # EAN-13 that is a UPC-A
        add(kroger_form(f))
    return out

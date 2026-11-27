"""Generate the printable linking-station mode card(s) in docs/.

    pip install qrcode==8.2 pillow
    python script/make_mode_card.py

Writes docs/mode-card-in-stock.png (300 dpi) and docs/mode-card-in-stock.pdf
(the same image on a US Letter page, card at its real size: 85 x 55 mm, a
credit-card format that fits in a wallet or on the wall next to the scanner).
The QR payload must match IN_STOCK_CARD in
esphome/linking-station/include/link_mode.h; a test checks that.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import qrcode
from qrcode.constants import ERROR_CORRECT_M

PAYLOAD = "PANTRY-HUB:IN-STOCK"
DPI = 300
MM = DPI / 25.4
CARD_W, CARD_H = round(85 * MM), round(55 * MM)
ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = ROOT / "docs" / "mode-card-in-stock.png"
OUT_PDF = ROOT / "docs" / "mode-card-in-stock.pdf"
AMBER = (230, 140, 0)
INK = (25, 25, 25)
GREY = (110, 110, 110)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "Arial Bold.ttf" if bold else "Arial.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def card() -> Image.Image:
    img = Image.new("RGB", (CARD_W, CARD_H), "white")
    d = ImageDraw.Draw(img)
    # Cut line and amber band (matches the station's amber pulse).
    d.rectangle([0, 0, CARD_W - 1, CARD_H - 1], outline=(180, 180, 180), width=3)
    band = round(9 * MM)
    d.rectangle([0, 0, CARD_W - 1, band], fill=AMBER)
    d.text((round(4 * MM), band // 2), "PANTRY HUB · MODE CARD", font=_font(42, True), fill="white", anchor="lm")

    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2, box_size=10)
    qr.add_data(PAYLOAD)
    qr.make(fit=True)
    size = round(38 * MM)  # ≥ 30 mm reads easily with the M14
    code = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    code = code.resize((size, size), Image.Resampling.NEAREST)
    top = band + round(4 * MM)
    img.paste(code, (round(3 * MM), top))

    x = round(3 * MM) + size + round(3 * MM)
    y = top + round(1 * MM)
    d.text((x, y), "Already", font=_font(66, True), fill=INK)
    d.text((x, y + 80), "in stock", font=_font(66, True), fill=AMBER)
    lines = [
        "1. Double-click: link mode",
        "2. Scan this card:",
        "   pulse turns amber",
        "3. Link items Grocy",
        "   already counts",
        "Scan again or 60 s idle",
        "to end the session.",
    ]
    f = _font(27)
    for i, line in enumerate(lines):
        d.text((x, y + 185 + i * 36), line, font=f, fill=GREY if i >= 5 else INK)
    d.text((round(3 * MM), CARD_H - round(2.5 * MM)), PAYLOAD, font=_font(24), fill=GREY, anchor="ls")
    return img


def main() -> None:
    img = card()
    img.save(OUT_PNG, dpi=(DPI, DPI))
    # US Letter page, two copies, real size.
    page = Image.new("RGB", (round(8.5 * DPI), round(11 * DPI)), "white")
    margin = round(20 * MM)
    page.paste(img, (margin, margin))
    page.paste(img, (margin, margin + CARD_H + round(10 * MM)))
    ImageDraw.Draw(page).text(
        (margin, margin + 2 * CARD_H + round(16 * MM)),
        "Print at 100% (no 'fit to page'). Cut along the grey lines. "
        "Laminate or tape over it if it lives next to the scanner.",
        font=_font(30),
        fill=GREY,
    )
    page.save(OUT_PDF, "PDF", resolution=DPI)
    print(f"wrote {OUT_PNG.relative_to(ROOT)} and {OUT_PDF.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

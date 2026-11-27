"""Contract between the linking station firmware and the hub (Q8).

The firmware lives in this repo (esphome/linking-station/); these checks
fail if the two sides drift apart: the in-stock card payload, the
"in_stock" field the hub reads, and the topics.
"""

from __future__ import annotations

from pathlib import Path
import re

from pantry_hub.linking import _in_stock

REPO = Path(__file__).resolve().parents[2]
FIRMWARE = REPO / "esphome" / "linking-station"
YAML = (FIRMWARE / "kroger-scanner.yaml").read_text(encoding="utf-8")
HEADER = (FIRMWARE / "include" / "link_mode.h").read_text(encoding="utf-8")
CARD_SCRIPT = (REPO / "script" / "make_mode_card.py").read_text(encoding="utf-8")


def _publish_blocks(topic_suffix: str) -> list[str]:
    pattern = rf"topic: pantry_hub/linking/\$\{{station_id\}}/{topic_suffix}\n(.*?)(?=\n\s*- |\n\S)"
    return re.findall(pattern, YAML, re.S)


def test_card_payload_matches_firmware() -> None:
    header = re.search(r'IN_STOCK_CARD = "([^"]+)"', HEADER)
    script = re.search(r'^PAYLOAD = "([^"]+)"', CARD_SCRIPT, re.M)
    assert header and script
    assert header.group(1) == script.group(1) == "PANTRY-HUB:IN-STOCK"
    assert (REPO / "docs" / "mode-card-in-stock.png").stat().st_size > 10_000
    assert (REPO / "docs" / "mode-card-in-stock.pdf").stat().st_size > 10_000


def test_firmware_sends_in_stock_on_barcode_and_tag_during_session() -> None:
    for suffix in ("barcode", "tag"):
        blocks = _publish_blocks(suffix)
        assert blocks, f"no publish to .../{suffix}"
        assert any(
            'if (link_mode::state().in_stock()) root["in_stock"] = true;' in b
            for b in blocks
        ), f".../{suffix} does not carry in_stock during a session"


def test_hub_reads_the_field_the_firmware_sends() -> None:
    assert _in_stock({"barcode": "1", "in_stock": True})
    assert not _in_stock({"barcode": "1"})
    assert not _in_stock({"barcode": "1", "in_stock": "true"})  # JSON bool only
    assert '"in_stock"' in YAML


def test_session_ends_with_link_mode() -> None:
    exit_block = YAML.split("- id: exit_link_mode", 1)[1].split("\n  - id:", 1)[0]
    assert "link_mode::state().exit_link()" in exit_block
    assert re.search(
        r"void exit_link\(\) \{\s*link_ = false;\s*in_stock_ = false;", HEADER
    )

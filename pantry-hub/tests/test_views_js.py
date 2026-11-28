"""Render views.js functions with Node (skipped where Node isn't installed)."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "src/pantry_hub/web/static/views.js"
NODE = shutil.which("node")


def render(function: str, *args: object) -> str:
    if NODE is None:
        pytest.skip("node not installed")
    script = (
        f"import * as V from {json.dumps(VIEWS.as_uri())};"
        f"const args = {json.dumps(args)}.map((a) => a && a.__ui"
        "  ? {noPicture: new Set(), api: {url: (path) => path}} : a);"
        f"process.stdout.write(V[{json.dumps(function)}](...args));"
    )
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout


def review_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "product_id": 1,
        "name": "Salt",
        "counted": 0,
        "tags": 0,
        "target": 0,
        "grocy": 5,
        "difference": -5,
        "scanned": True,
        "applied": "set",
        "tagged_after": 0,
        "to_tag": 0,
        "shortfall": 3,
        "elsewhere": 2,
    }
    row.update(over)
    return row


def test_review_shows_pantry_shortfall() -> None:
    state = {"stocktake": {"phase": "applied", "how": "set", "review": [review_row()]}}
    html = render("stocktakeCard", state)
    assert "Grocy set · Grocy still shows 2 in other locations" in html


def test_review_without_other_locations_says_nothing_more() -> None:
    state = {
        "stocktake": {
            "phase": "applied",
            "how": "set",
            "review": [review_row(shortfall=0, elsewhere=0)],
        }
    }
    html = render("stocktakeCard", state)
    assert "Grocy set" in html and "other locations" not in html


def test_allowance_list_text() -> None:
    state = {
        "now": 1_800_000_000,
        "pictures": {"versions": {}},
        "allowances": [
            {
                "product_id": 7,
                "name": "Cereal",
                "units": 3,
                "sources": ["stocktake"],
                "expires": 1_800_000_000 + 7 * 86400,
                "text": "3 counted units still to tag",
            }
        ],
    }
    ui = {"__ui": True}  # the page's ui object (built in JS)
    html = render("allowanceList", state, ui)
    assert "Waiting for tags · 1" in html
    assert "3 counted units still to tag" in html

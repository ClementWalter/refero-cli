#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx==0.28.1",
#     "click==8.5.0",
#     "pytest==9.0.3",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Offline tests for refero_cli: flight decoding, matching, and each command.

The HTTP layer is an `httpx.MockTransport` serving hand-built pages, so no
test reaches styles.refero.design.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import refero_cli  # noqa: E402

STYLE_ID = "12345678-1234-1234-1234-123456789abc"
OTHER_ID = "00000000-0000-0000-0000-000000000001"
THIRD_ID = "00000000-0000-0000-0000-000000000002"

DESIGN_MD = (
    "# Example design\n\n"
    "### Tailwind v4\n\n```css\n@theme { --color-ink: #000000; }\n```\n\n"
    "### CSS Custom Properties\n\n```css\n:root { --ink: #000000; }\n```\n"
)


def compact(value) -> str:
    """JSON the way the flight payload writes it: no spaces."""
    return json.dumps(value, separators=(",", ":"))


def card(style_id: str, name: str, url: str) -> dict:
    return {"id": style_id, "siteName": name, "url": url, "colorScheme": "light",
            "createdAt": "2026-01-01 00:00:00", "northStar": f"{name} tagline"}


def page(flight_rows: list[str], body: str = "") -> str:
    """An HTML page whose flight payload is `flight_rows`, one push each."""
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(row)}])</script>" for row in flight_rows
    )
    return f"<html><body>{body}{pushes}</body></html>"


CARDS = [
    card(STYLE_ID, "Example", "https://example.com"),
    card(OTHER_ID, "Example Labs", "https://labs.example.org"),
    card(THIRD_ID, "Other Co", "https://other.example.net"),
]

CUSTOM_TEXT = "Quick reference: ink — #000000"


def style_page() -> str:
    result = {
        "meta": {"url": "https://example.com", "siteName": "Example",
                 "extractedAt": "2026-01-01T00:00:00.000Z"},
        "raw": {"colors": {"tokens": []}},
        "designSystem": {"theme": "dark", "industry": "saas",
                         "customSections": [{"content": "$1e"}]},
        "screenshot": {"url": "https://images.example.com/x.jpg"},
    }
    obj = {"styleId": STYLE_ID, "result": result}
    text_len = format(len(CUSTOM_TEXT.encode()), "x")
    rows = [
        "0:" + compact(["$", "$L1d", None, obj]) + "\n",
        f"1e:T{text_len},",
        CUSTOM_TEXT,
    ]
    escaped = DESIGN_MD.replace("&", "&amp;").replace("<", "&lt;")
    return page(rows, f'<pre class="x"><code class="y">{escaped}</code></pre>')


HOME = page(["5:" + compact(["$", "div", None, {"styles": CARDS[:2]}]) + "\n"])
COLLECTION = page(["7:" + compact({"items": [CARDS[1], CARDS[2]]}) + "\n"])
SITEMAP = (
    "<urlset><url><loc>https://styles.refero.design</loc></url>"
    "<url><loc>https://styles.refero.design/design-styles/minimal</loc></url></urlset>"
)


def handler(request: httpx.Request) -> httpx.Response:
    routes = {
        "/": HOME,
        "/design-styles/minimal": COLLECTION,
        "/sitemaps/collections.xml": SITEMAP,
        f"/style/{STYLE_ID}": style_page(),
    }
    if request.url.path in routes:
        return httpx.Response(200, text=routes[request.url.path])
    return httpx.Response(404, text="not found")


@pytest.fixture(autouse=True)
def stub_http(monkeypatch):
    monkeypatch.setattr(
        refero_cli,
        "_client",
        lambda: httpx.Client(base_url=refero_cli.BASE_URL, transport=httpx.MockTransport(handler)),
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def flight() -> str:
    return refero_cli.flight_payload(style_page())


# ---------- decoding --------------------------------------------------------


def test_flight_payload_unescapes_and_joins():
    assert refero_cli.flight_payload(page(['a\\"b', "c"])) == 'a\\"bc'


def test_style_cards_keep_page_order():
    cards = refero_cli.style_cards(refero_cli.flight_payload(HOME))
    assert [c["id"] for c in cards] == [STYLE_ID, OTHER_ID]


def test_style_cards_skip_objects_without_site_name():
    flight = '{"id":"' + STYLE_ID + '","name":"x"}'
    assert refero_cli.style_cards(flight) == []


def test_style_record_site_name(flight):
    assert refero_cli.style_record(flight, STYLE_ID)["siteName"] == "Example"


def test_style_record_resolves_text_rows(flight):
    record = refero_cli.style_record(flight, STYLE_ID)
    assert record["fullResult"]["designSystem"]["customSections"][0]["content"] == CUSTOM_TEXT


def test_style_record_unknown_id_names_the_fix(flight):
    with pytest.raises(refero_cli.click.ClickException, match="check the id"):
        refero_cli.style_record(flight, OTHER_ID)


def test_design_md_from_page_unescapes():
    assert refero_cli.design_md_from_page(style_page(), STYLE_ID) == DESIGN_MD


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        (r"^###\s+Tailwind\s+v4\b.*$", "@theme { --color-ink: #000000; }"),
        (r"^###\s+CSS\s+Custom\s+Properties\b.*$", ":root { --ink: #000000; }"),
        (r"^###\s+Missing\b.*$", ""),
    ],
)
def test_extract_fenced_block(heading, expected):
    assert refero_cli.extract_fenced_block(DESIGN_MD, heading) == expected


@pytest.mark.parametrize(("name", "slug"), [("Example Labs", "example-labs"), ("!!!", "style")])
def test_slugify(name, slug):
    assert refero_cli.slugify(name) == slug


# ---------- matching --------------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("example", STYLE_ID),
        ("https://example.com/", STYLE_ID),
        ("labs", OTHER_ID),
        ("Other Co", THIRD_ID),
    ],
)
def test_pick(identifier, expected):
    assert refero_cli.pick(CARDS, identifier)["id"] == expected


def test_pick_ambiguous_lists_candidates():
    with pytest.raises(refero_cli.click.ClickException, match="matches 3 styles"):
        refero_cli.pick(CARDS, "example.")


def test_pick_unknown_names_the_fix():
    with pytest.raises(refero_cli.click.ClickException, match="refero list --all -q"):
        refero_cli.pick(CARDS, "nothing-like-it")


def test_resolve_style_id_from_style_url():
    url = f"https://styles.refero.design/style/{STYLE_ID}"
    assert refero_cli.resolve_style_id(None, url) == STYLE_ID


def test_crawl_dedupes_across_pages():
    with refero_cli._client() as client:
        assert len(refero_cli.crawl(client)) == 3


def test_http_error_is_one_line():
    with refero_cli._client() as client, pytest.raises(refero_cli.click.ClickException, match="404"):
        refero_cli._get(client, "/missing")


# ---------- commands --------------------------------------------------------


def test_list_trending_ids(runner):
    result = runner.invoke(refero_cli.cli, ["list", "--format", "ids"])
    assert result.output.split() == [STYLE_ID, OTHER_ID]


def test_list_json_parses(runner):
    result = runner.invoke(refero_cli.cli, ["list", "--json"])
    assert [r["id"] for r in json.loads(result.output)] == [STYLE_ID, OTHER_ID]


def test_list_search_filters_client_side(runner):
    result = runner.invoke(refero_cli.cli, ["list", "-q", "other", "--json"])
    assert [r["id"] for r in json.loads(result.output)] == [THIRD_ID]


def test_list_table_is_tab_separated(runner):
    result = runner.invoke(refero_cli.cli, ["list", "-n", "1"])
    assert result.output == f"Example\thttps://example.com\t{STYLE_ID}\n"


def test_show_json_parses(runner):
    result = runner.invoke(refero_cli.cli, ["show", STYLE_ID])
    assert json.loads(result.output)["url"] == "https://example.com"


def test_show_unknown_exits_non_zero(runner):
    result = runner.invoke(refero_cli.cli, ["show", "nothing-like-it"])
    assert result.exit_code == 1


def test_pull_writes_tailwind(runner, tmp_path):
    runner.invoke(refero_cli.cli, ["pull", STYLE_ID, "-d", str(tmp_path)])
    assert (tmp_path / "example" / "tailwind.css").read_text() == "@theme { --color-ink: #000000; }\n"


def test_pull_metadata_color_scheme(runner, tmp_path):
    runner.invoke(refero_cli.cli, ["pull", STYLE_ID, "-d", str(tmp_path)])
    meta = json.loads((tmp_path / "example" / "metadata.json").read_text())
    assert meta["colorScheme"] == "dark"


def test_pull_keeps_existing_without_force(runner, tmp_path):
    (tmp_path / "example").mkdir()
    (tmp_path / "example" / "DESIGN.md").write_text("kept\n")
    runner.invoke(refero_cli.cli, ["pull", STYLE_ID, "-d", str(tmp_path)])
    assert (tmp_path / "example" / "DESIGN.md").read_text() == "kept\n"


def test_pull_prints_out_dir(runner, tmp_path):
    result = runner.invoke(refero_cli.cli, ["pull", STYLE_ID, "-d", str(tmp_path)])
    assert result.stdout.strip() == str(tmp_path / "example")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

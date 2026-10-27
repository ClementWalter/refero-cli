#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx==0.28.1",
#     "click==8.5.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""CLI for fetching design references from https://styles.refero.design.

Lists curated design systems and downloads any single style as the four
artifacts shown on the website (DESIGN.md, Tailwind v4 CSS, plain CSS custom
properties, design tokens JSON) into a local reference folder so an agent can
use them as style guidance.

The site's JSON API (`/api/styles*`) sits behind Vercel BotID and answers 403
to any non-browser client, so every read goes through the server-rendered
pages instead: style cards and records are decoded from the Next.js flight
payload (`self.__next_f.push`) embedded in the HTML.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
import httpx

BASE_URL = "https://styles.refero.design"
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
# Collection pages are listed in this sitemap; together with the homepage they
# expose a few hundred style cards (the full catalogue is only reachable by id).
COLLECTIONS_SITEMAP = "/sitemaps/collections.xml"
CRAWL_WORKERS = 8

logger = logging.getLogger("refero")


# ---------- HTTP helpers ----------------------------------------------------


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        timeout=30.0,
        headers={"User-Agent": "refero-cli/0.2 (+https://github.com/ClementWalter/refero-cli)"},
        follow_redirects=True,
    )


def _get(client: httpx.Client, path: str) -> httpx.Response:
    """GET `path`, turning transport and HTTP failures into one-line CLI errors."""
    try:
        r = client.get(path)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        hint = "unknown style id" if e.response.status_code == 404 else "retry later"
        raise click.ClickException(f"{e.response.status_code} from {BASE_URL}{path}; {hint}") from e
    except httpx.HTTPError as e:
        raise click.ClickException(f"cannot reach {BASE_URL}: {e}; check the network") from e
    return r


# ---------- Flight payload decoding -----------------------------------------


def flight_payload(page: str) -> str:
    """Concatenate the Next.js flight chunks embedded in a rendered page."""
    chunks = re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', page)
    return "".join(json.loads(f'"{m.group(1)}"') for m in chunks)


def flight_text_row(flight: str, ref: str) -> str | None:
    """Resolve a `$<id>` reference to a `<id>:T<hexlen>,<text>` row.

    The length is in UTF-8 bytes, so the slice is taken on the encoded text.
    """
    m = re.search(rf"(?<![0-9a-zA-Z]){re.escape(ref[1:])}:T([0-9a-f]+),", flight)
    if not m:
        return None
    rest = flight[m.end():].encode()
    return rest[: int(m.group(1), 16)].decode(errors="replace")


def resolve_refs(value, flight: str):
    """Replace `$<id>` text references in a decoded object by their row text."""
    if isinstance(value, dict):
        return {k: resolve_refs(v, flight) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, flight) for v in value]
    if isinstance(value, str) and re.fullmatch(r"\$[0-9a-f]+", value):
        text = flight_text_row(flight, value)
        return value if text is None else text
    return value


def style_cards(flight: str) -> list[dict]:
    """Style summary cards (objects with an `id` and a `siteName`), in page order."""
    dec = json.JSONDecoder()
    cards: dict[str, dict] = {}
    for m in re.finditer(r'\{"id":"[0-9a-f-]{36}"', flight):
        try:
            obj, _ = dec.raw_decode(flight, m.start())
        except json.JSONDecodeError:
            continue
        if "siteName" in obj and obj["id"] not in cards:
            cards[obj["id"]] = obj
    return list(cards.values())


def summarize(card: dict) -> dict:
    """The stable subset of a card printed by `list`."""
    return {
        "id": card["id"],
        "siteName": card.get("siteName") or "",
        "url": card.get("url") or "",
        "colorScheme": card.get("colorScheme"),
        "createdAt": card.get("createdAt"),
    }


def style_record(flight: str, style_id: str) -> dict:
    """The full style record rendered on `/style/<id>`.

    Shaped like the website's former `/api/styles/<id>` answer: `id`,
    `siteName`, `url` and `fullResult.{meta,raw,designSystem}`.
    """
    marker = flight.find(f'{{"styleId":"{style_id}","result":')
    if marker < 0:
        raise click.ClickException(f"no style record on {BASE_URL}/style/{style_id}; check the id")
    obj, _ = json.JSONDecoder().raw_decode(flight, marker)
    result = resolve_refs(obj["result"], flight)
    meta = result.get("meta") or {}
    return {
        "id": style_id,
        "siteName": meta.get("siteName"),
        "url": meta.get("url"),
        "fullResult": {k: result.get(k) for k in ("meta", "raw", "designSystem")},
    }


def design_md_from_page(page: str, style_id: str) -> str:
    """The DESIGN.md the style page server-renders in its single `<pre><code>` block."""
    m = re.search(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", page, re.S)
    if not m:
        raise click.ClickException(f"DESIGN.md block not found on {BASE_URL}/style/{style_id}")
    return html.unescape(m.group(1))


# ---------- Catalogue -------------------------------------------------------


def trending(client: httpx.Client) -> list[dict]:
    """The homepage's server-rendered trending feed (its first page only)."""
    return style_cards(flight_payload(_get(client, "/").text))


def collection_paths(client: httpx.Client) -> list[str]:
    xml = _get(client, COLLECTIONS_SITEMAP).text
    return [httpx.URL(u).path or "/" for u in re.findall(r"<loc>([^<]+)</loc>", xml)]


def crawl(client: httpx.Client) -> list[dict]:
    """Every style card on the homepage and the collection pages, deduplicated."""
    paths = collection_paths(client)
    with ThreadPoolExecutor(CRAWL_WORKERS) as pool:
        pages = list(pool.map(lambda p: _get(client, p).text, paths))
    seen: dict[str, dict] = {}
    for page in pages:
        for card in style_cards(flight_payload(page)):
            seen.setdefault(card["id"], card)
    logger.info("crawled %d pages, %d styles", len(paths), len(seen))
    return sorted(seen.values(), key=lambda c: ((c.get("siteName") or "").lower(), c["id"]))


def matches(card: dict, needle: str) -> bool:
    needle = needle.strip().lower()
    return any(needle in (card.get(k) or "").lower() for k in ("siteName", "url", "northStar"))


def pick(cards: list[dict], identifier: str) -> dict:
    """The one card an identifier names: an exact name/URL, else a unique substring hit."""
    needle = identifier.strip().lower().rstrip("/")
    exact = [
        c for c in cards
        if needle in {(c.get("siteName") or "").lower(), (c.get("url") or "").lower().rstrip("/")}
    ]
    if len(exact) == 1:
        return exact[0]
    hits = exact or [c for c in cards if matches(c, needle)]
    if not hits:
        raise click.ClickException(
            f"no crawled style matches {identifier!r}; browse with `refero list --all -q <keyword>`"
            " or pass the UUID from the style's website URL"
        )
    if len(hits) > 1:
        names = ", ".join(f"{c.get('siteName')} ({c['id']})" for c in hits[:10])
        raise click.ClickException(
            f"{identifier!r} matches {len(hits)} styles: {names}; pass one of the ids"
        )
    return hits[0]


def resolve_style_id(client: httpx.Client, identifier: str) -> str:
    """A style UUID from a UUID, a `/style/<id>` URL, a site URL or a name."""
    m = UUID.search(identifier)
    if m:
        return m.group(0)
    return pick(crawl(client), identifier)["id"]


def fetch_style(client: httpx.Client, style_id: str) -> tuple[dict, str]:
    """The full record and the raw HTML of `/style/<id>`."""
    page = _get(client, f"/style/{style_id}").text
    return style_record(flight_payload(page), style_id), page


# ---------- Markdown slicing -----------------------------------------------


def extract_fenced_block(markdown: str, heading_pattern: str) -> str:
    """Return the first fenced ```...``` block that follows a heading match.

    `heading_pattern` is a regex matched against a single line. The function
    finds the next fenced code block after that line (any language). If no
    block is found, returns an empty string.
    """
    h = re.search(heading_pattern, markdown, re.M)
    if not h:
        return ""
    fence = re.search(r"```[^\n]*\n(.*?)\n```", markdown[h.end():], re.S)
    return fence.group(1) if fence else ""


def design_tokens_from_style(style: dict) -> dict:
    """A Design Tokens JSON document from the record's `fullResult` block.

    The website's "Design Tokens" tab is rendered client-side from this same
    structured payload, so persisting it gives a machine-readable companion to
    DESIGN.md.
    """
    full = style.get("fullResult") or {}
    return {
        "siteName": style.get("siteName"),
        "url": style.get("url"),
        "designSystem": full.get("designSystem"),
        "raw": full.get("raw"),
        "meta": full.get("meta"),
    }


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "style"


# ---------- CLI -------------------------------------------------------------


@click.group(
    epilog="\b\nExamples:\n  refero list -n 5\n  refero list --all -q linear --json\n  refero pull elevenlabs -d reference"
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """Browse and download styles from styles.refero.design."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    # httpx logs every request at INFO, which drowns the CLI's own messages.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


@cli.command(
    "list",
    epilog="\b\nExamples:\n  refero list -n 5\n  refero list --all -q stripe --format ids\n  refero list --all -q linear --json | jq '.[].siteName'",
)
@click.option("--search", "-q", default=None, help="Client-side filter on name, URL and tagline; implies --all.")
@click.option("--all", "crawl_all", is_flag=True, help="Crawl the homepage and every collection page (~50 pages).")
@click.option("--limit", "-n", type=int, default=None, help="Max results to print.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json", "ids"]),
    default="table",
    help="Output format.",
)
@click.option("--json", "as_json", is_flag=True, help="Same as --format json.")
def list_cmd(search: str | None, crawl_all: bool, limit: int | None, fmt: str, as_json: bool) -> None:
    """List styles: the trending feed, or every crawlable style with --all."""
    with _client() as client:
        cards = crawl(client) if (crawl_all or search) else trending(client)
    if search:
        cards = [c for c in cards if matches(c, search)]
    rows = [summarize(c) for c in cards][:limit]
    if as_json or fmt == "json":
        click.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if fmt == "ids":
        for s in rows:
            click.echo(s["id"])
        return
    for s in rows:
        click.echo(f"{s['siteName']}\t{s['url']}\t{s['id']}")
    logger.info("%d style(s)", len(rows))


@cli.command(
    "show",
    epilog="\b\nExamples:\n  refero show linear\n  refero show https://stripe.com | jq .fullResult.designSystem.dos",
)
@click.argument("identifier")
def show_cmd(identifier: str) -> None:
    """Print the JSON record for a single style (id, URL, or name)."""
    with _client() as client:
        style, _ = fetch_style(client, resolve_style_id(client, identifier))
    click.echo(json.dumps(style, indent=2, ensure_ascii=False))


@cli.command(
    "pull",
    epilog="\b\nExamples:\n  refero pull elevenlabs\n  refero pull linear -d docs/styles --slug linear-app --force",
)
@click.argument("identifier")
@click.option(
    "--dest",
    "-d",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("reference"),
    show_default=True,
    help="Reference root directory; a per-style subfolder is created inside it.",
)
@click.option(
    "--slug",
    default=None,
    help="Override the slug (default: derived from siteName).",
)
@click.option(
    "--force/--no-force",
    default=False,
    help="Overwrite existing files.",
)
def pull_cmd(identifier: str, dest: Path, slug: str | None, force: bool) -> None:
    """Download a style as DESIGN.md + tailwind.css + styles.css + tokens.json.

    The four files match the four tabs on the website. They are written to
    `<dest>/<slug>/` so a skill can reference them at a stable path.
    """
    with _client() as client:
        style_id = resolve_style_id(client, identifier)
        style, page = fetch_style(client, style_id)
    site_name = style.get("siteName") or "style"
    out_dir = dest / (slug or slugify(site_name))
    out_dir.mkdir(parents=True, exist_ok=True)

    design_md = design_md_from_page(page, style_id)
    meta = style["fullResult"].get("meta") or {}
    system = style["fullResult"].get("designSystem") or {}
    files = {
        "DESIGN.md": design_md,
        "tailwind.css": extract_fenced_block(design_md, r"^###\s+Tailwind\s+v4\b.*$"),
        "styles.css": extract_fenced_block(design_md, r"^###\s+CSS\s+Custom\s+Properties\b.*$"),
        "tokens.json": json.dumps(design_tokens_from_style(style), indent=2, ensure_ascii=False),
        "metadata.json": json.dumps(
            {
                "id": style_id,
                "siteName": site_name,
                "url": style.get("url"),
                "industry": system.get("industry"),
                "colorScheme": system.get("theme"),
                "extractedAt": meta.get("extractedAt"),
                "source": f"{BASE_URL}/style/{style_id}",
            },
            indent=2,
            ensure_ascii=False,
        ),
    }

    for name, content in files.items():
        path = out_dir / name
        if path.exists() and not force:
            logger.warning("skip %s (exists; use --force)", path)
            continue
        if not content:
            # An empty file makes the missing block visible to consumers.
            logger.warning("%s is empty for %s", name, site_name)
        path.write_text(content if content.endswith("\n") else content + "\n")
        logger.info("wrote %s", path)

    click.echo(str(out_dir))


if __name__ == "__main__":
    cli()

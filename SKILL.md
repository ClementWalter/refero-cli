---
name: refero-cli
description:
  Browse and download curated design references from styles.refero.design
  (DESIGN.md, Tailwind v4, plain CSS, design tokens) into a local reference
  folder so they can be used as design guidance for UI work, via the `refero`
  command. Use when the user asks to "use the X style", "fetch a refero design",
  "pull a design system from refero", "list refero styles", or wants to seed an
  agent with a brand's visual language.
---

# refero-cli

A small CLI for [styles.refero.design](https://styles.refero.design), a curated
library of design references where each entry exposes four artifacts:

- `DESIGN.md`: the canonical, agent-facing markdown brief (colors, typography,
  spacing, components, dos/don'ts, similar brands).
- `tailwind.css`: a Tailwind v4 `@theme { ... }` block.
- `styles.css`: plain `:root { ... }` CSS custom properties.
- `tokens.json`: structured design tokens (colors, raw extraction, full
  design-system JSON).

Invoke it whenever the user wants to **bring a real-world brand's visual
language into the project as a reference**, e.g. "Pull the Linear design", "Add
ElevenLabs as a style reference", "List trending refero styles", "Fetch the
Mercury tokens". Prefer it over generic web-fetching: `refero pull` produces a
stable layout
(`reference/<slug>/{DESIGN.md,tailwind.css,styles.css,tokens.json,metadata.json}`)
that is easy to point at later from CLAUDE.md, prompts, or component scaffolds.

## How to invoke

`refero` is on `$PATH` once `uv run ~/Documents/claudine/scripts/link_clis.py`
has linked `bin/refero` into `~/.local/bin`. Without the link, run
`<skill-dir>/bin/refero`. It is a single PEP 723 script (`refero_cli.py`), so
`uv` resolves its dependencies on first run; there is no install step.

## Authentication

None. It reads the public website only.

## Output conventions

- `list` prints one style per line, `<siteName>\t<url>\t<id>`; `--json` (or
  `--format json`) prints a JSON array of `{id, siteName, url, colorScheme,
  createdAt}`, `--format ids` only the UUIDs.
- `show` prints the style record as JSON: `id`, `siteName`, `url` and
  `fullResult.{meta,raw,designSystem}` (dos/don'ts, components, theme, layout,
  ...).
- `pull` prints the output directory on stdout.
- Counts, progress and warnings go to stderr; `-v` turns on debug logging.

## Commands

```bash
# Read
refero list                          # trending feed (homepage, 20 styles)
refero list -n 5                     # cap rows
refero list --all                    # every style on the homepage + ~50 collection pages (~375)
refero list -q stripe                # client-side search over the --all crawl (name / URL / tagline)
refero list --all --format ids       # just UUIDs
refero list -q linear --json         # JSON summaries
refero show <id-or-url-or-name>      # full JSON record for one style

# Write (local files only)
refero pull <id-or-url-or-name>                 # writes ./reference/<slug>/
refero pull elevenlabs -d docs/styles           # custom destination root
refero pull elevenlabs --slug eleven-labs      # custom slug
refero pull elevenlabs --force                  # overwrite existing files
```

Identifiers accepted, in order of preference:

1. UUID (e.g. `031056ff-7af1-46db-8daa-115f731c5d26`) or a
   `https://styles.refero.design/style/<id>` URL: exact, no crawl.
2. Site URL (e.g. `https://stripe.com`) or brand name (e.g. `linear`): matched
   against the crawl; an exact name or URL wins, otherwise a single substring
   hit. Several hits is an error listing the candidates with their ids.

`pull` writes five files into `<dest>/<slug>/`:

| File            | Source                                                               |
| --------------- | -------------------------------------------------------------------- |
| `DESIGN.md`     | The `<pre><code>` block on `/style/<id>`, verbatim markdown.         |
| `tailwind.css`  | The `### Tailwind v4` fenced block extracted from `DESIGN.md`.       |
| `styles.css`    | The `### CSS Custom Properties` fenced block from `DESIGN.md`.       |
| `tokens.json`   | `fullResult.{designSystem,raw,meta}` from the style page's payload.  |
| `metadata.json` | `id`, `siteName`, `url`, `industry`, `colorScheme`, `extractedAt`, source URL. |

## Recipes

```bash
# "Use the Linear style for this page"
refero pull linear && cat reference/linear/DESIGN.md

# "What does Stripe recommend not doing?"
refero show https://stripe.com | jq .fullResult.designSystem.donts

# "Which dark fintech styles are there?"
refero list --all --json | jq -r '.[] | select(.colorScheme=="dark") | .siteName'
```

Once a style is pulled, point downstream work at it:

- **Tailwind v4 projects:** copy `tailwind.css` into the main stylesheet and
  import Tailwind alongside it; the `@theme` block is plug-and-play.
- **Non-Tailwind projects:** use `styles.css`, the same tokens as plain CSS
  variables under `:root`.
- **Agent prompting / design-decision context:** reference `DESIGN.md`, the
  curated brief written to be read by an LLM.
- **Scripted pipelines** (generating a component library, mapping tokens to
  JS): parse `tokens.json`.

## Gotchas

- The site's JSON API (`/api/styles`, `/api/styles/search`, `/api/styles/<id>`)
  is behind Vercel BotID and answers 403 to any non-browser client (measured
  2026-10-27), so the CLI reads the server-rendered pages instead. Don't try to
  call the API directly.
- Search is client-side over a crawl of the homepage plus every page in
  `/sitemaps/collections.xml`: 52 pages, ~375 unique styles, ~2 s with 8
  parallel fetches (measured 2026-10-27). The catalogue holds ~1,340 styles;
  the rest are reachable only by UUID, taken from the style's website URL.
- `list` without `--all`/`-q` is the homepage's first trending page (20); the
  site renders no further pages server-side.
- Run from the project root so the relative `reference/` path stays
  consistent.
- If `tailwind.css` or `styles.css` come out empty, that style's DESIGN.md is
  missing the corresponding fenced block; derive values from `tokens.json`.
- Don't re-pull on every turn: the artifacts are stable. Re-pull only when the
  user asks for a refresh, with `--force`.

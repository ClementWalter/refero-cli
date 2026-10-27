# refero-cli

**Visibility: public.**

A CLI, and a Claude Code skill, for downloading curated design references from
[styles.refero.design](https://styles.refero.design) into a local
`reference/<slug>/` folder for use as design guidance in agent-driven UI work.
Ships one command, `refero`. No account or key needed.

The repo is itself a skill: `SKILL.md` lives at the root next to the
`refero_cli.py` CLI it documents.

## Install

Requires [`uv`](https://docs.astral.sh/uv/): `refero_cli.py` declares its
exact-pinned dependencies with PEP 723 inline metadata, so `uv` fetches `httpx`
and `click` on first run.

```bash
git clone https://github.com/ClementWalter/refero-cli ~/Documents/refero-cli
ln -sfn ~/Documents/refero-cli/bin/refero ~/.local/bin/refero

# register it as a Claude Code skill
ln -sfn ~/Documents/refero-cli ~/.claude/skills/refero-cli
```

[`npx skills`](https://github.com/vercel-labs/skills) works too:
`npx skills add clementwalter/refero-cli -a claude-code -g`.

## Quick start

```bash
refero list                    # trending styles (homepage feed)
refero list -q linear          # search by brand / keyword / URL
refero pull elevenlabs         # → reference/elevenlabs/{DESIGN.md, tailwind.css, styles.css, tokens.json, metadata.json}
```

`pull` and `show` accept a UUID, a style URL, a site URL (`https://stripe.com`)
or a brand name. See `SKILL.md` for the full command surface and its limits.

## Tests

```bash
uv run --script tests/test_refero_cli.py
```

Offline: the HTTP layer is stubbed with `httpx.MockTransport`.

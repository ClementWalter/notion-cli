---
name: notion-cli
description: >
  Token-efficient Notion access from the terminal using notion_cli.py
  (session-token v3 API — acts as the user, like slack-user-cli). Use for
  reading Notion pages, querying databases with filters and column selection,
  searching the workspace, listing discussions incl. RESOLVED ones, and
  writing — creating pages, updating properties, appending markdown,
  search-and-replace edits, comments. Prefer it over the Notion MCP for READS
  (compact markdown, signed URLs stripped, --select projection); output is
  pipeable through jq/python so only the projection you need enters context.
  Triggers: "read notion page", "query notion database", "notion cli",
  "update notion row", "create notion page".
---

# Notion CLI

Session-token Notion client at `~/.claude/skills/notion-cli/notion_cli.py`,
built to minimize the tokens a read costs an agent: compact line-oriented
output, flattened properties, signed-S3 URLs stripped, `--select` column
projection, and `--json` for programmatic consumers.

It authenticates **as the user with the `token_v2` browser cookie** (the
slack-user-cli model) against the same private API the Notion web client uses
(`notion.so/api/v3`), so it sees everything the user sees and needs no
workspace integration or admin approval. That API also exposes **resolved
comments**, which the official API hides.

## How to invoke

Invoke it as **`notion`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/notion`, so it always runs the current checkout: a `git pull`, or even
an uncommitted edit, takes effect immediately with nothing to reinstall.

```bash
notion search "query"
```

Examples in this doc are written that way. If `notion` is not on `$PATH`, run the
bundled launcher `bin/notion` resolved against this skill's own directory (PEP 723
— `uv` resolves deps inline on first run), or link it once:

```bash
ln -sfn <skill-dir>/bin/notion ~/.local/bin/notion
```

## Authentication

Preferred — automatic extraction from a local logged-in app (decrypts the
Notion-desktop/Chrome/Arc/Brave cookie store via the macOS keychain; stale
sessions are skipped, the first token that validates wins; may pop one
keychain "Allow" dialog per app):

```bash
notion login                # try all known cookie stores
notion login --source arc --space "My Workspace"
```

Manual fallback — grab `token_v2` from a logged-in browser (devtools →
Application → Cookies → `https://www.notion.so` → `token_v2`, value starts
with `v03:`), then:

```bash
notion auth                 # prompts for token_v2, hidden input
notion auth --import        # reuse ~/.config/notion-reader/config.json
notion auth --space "My Workspace"    # disambiguate when the login has several workspaces
```

Auth binds a (user, space) pair and stores it chmod-600 in
`~/.config/notion-cli/config.json`; `$NOTION_TOKEN_V2` overrides. The
**`x-notion-active-user-header` matters**: without the bound user id the API
returns HTTP 200 with permission-filtered EMPTY results — looks like missing
content, is actually missing auth context. `whoami` verifies the binding.
Tokens survive ~1 year unless the user logs out; a `401 — token_v2 expired`
means re-grab the cookie and re-run `auth`.

## Output conventions

- Default output is compact text, one line per row/property — designed to be
  cheap in an agent context. Reference ids/urls are appended tab-separated.
- `--json` = flattened JSON (properties reduced to plain values, body as one
  markdown string). Use for piping to `jq`/python.
- `--raw` = untouched API JSON. Token-expensive; debugging only.
- Big pulls: redirect to a file and filter, don't read the whole thing back
  (`… query DB --json > /tmp/rows.json && jq -r '.[].Status' /tmp/rows.json`).

## Commands

### Read

```bash
# Page: flattened properties + body rendered as compact markdown
notion page <id_or_url>              # props + body
notion page <id_or_url> --props-only # cheapest read
notion page <id_or_url> --no-props   # body only
notion page <id_or_url> --write      # @user(uuid)/@page(uuid) so the body can be written back
notion page <id_or_url> --depth 2    # cap nested-children recursion
notion pages <id1> <id2> <id3> ...   # multiple pages' bodies in ONE call — batches what would
                                             # otherwise be one `page` call per id (content only, no props)

# Database / data source query — accepts db url, data-source id, collection://…
notion query <db_or_ds> --select ID,Title,Status
notion query <db_or_ds> --filter 'Status=Done' --filter 'ID>195'
notion query <db_or_ds> --filter 'Title~vault' --sort 'ID:desc' --limit 20
notion query <db_or_ds> --filter 'Due is_empty' --json
notion query <db_or_ds> --filter 'Status=In progress' --with-body   # + every matched row's
                                             # full page body, in the SAME call — see below
notion query <db_or_ds> --edited-after 2026-07-20 --with-body   # bodies ONLY for rows
                                             # changed since a cutoff — for a repeat pass over the
                                             # same database, don't re-fetch what hasn't changed

# Filter DSL: =  !=  >  >=  <  <=  ~ (contains)  'Prop is_empty'  'Prop is_not_empty'
# Applied client-side on flattened values (numeric-aware; ~ is case-insensitive).

notion schema <db_or_ds>       # property name → type (+ collection/view ids)
notion search "vault launch" --limit 10
notion comments <page_id>      # discussions INCL. RESOLVED (public API can't)
notion comments <page_id> --open-only
notion users [query]           # workspace members (name, email, id)
notion resolve <id> [<id> ...] # id -> name/title, local cache first, one API call max per new id
notion blocks <page_id> --depth 2   # block ids (targets for edit/delete)
notion cache stats                 # rendered-body cache: entries, pages, size
notion cache clear [<page> ...]    # drop cached bodies (all, or just these pages)
```

**Prefer `resolve <id>` over re-running `users "<name>"`** to look up a
single id — `users` re-fetches the *entire* workspace member list every
call, while `resolve` checks the permanent local cache
(`~/.config/notion-cli/cache/id_names.json`, no TTL since Notion ids are
immutable) first and only hits the API for ids it hasn't seen before.
`page`/`query`/`users` all populate this cache automatically as a side
effect of normal reads, so by the time you need to resolve an id you've
already encountered (a mentioned user, a linked/breadcrumb page), it's
usually already cached for free. This also means **the same page never
needs re-fetching within a run just to check a different string in it** —
save it once (`notion page <id> --no-props > page.md`) and grep the
file, don't re-run `page` for every subsequent check.

**Never loop `page <id>` over a query's rows — use `--with-body` or `pages`
instead.** A `page` call per row is the single biggest source of avoidable
tool round-trips in a read-heavy session (measured: 60 separate `page`
calls in one run, one per tracker row — each one a full extra agent turn
resending the whole growing context, not just an extra API hit). If the ids
come from a query you're running right now, add `--with-body` to that same
`query` call — one command instead of N agent turns. If the ids come from
somewhere else (`search`, `resolve`, already known), pass them all to
`pages <id1> <id2> ...` in one call instead of iterating.

**Bodies are cached, so re-reading an unchanged page is free.** Rendered
bodies live in `~/.config/notion-cli/cache/bodies.sqlite3`, keyed by page id
+ `--depth` + `--write` and validated against the page's `last_edited_time`
(which every caller already has in hand, so validating costs no extra API
call). A page that hasn't changed is served from disk with **no**
`loadPageChunk` request — measured on 30 rows: 30 calls / 24.4s cold vs 0
calls / 2.0s warm, identical output. This matters because `loadPageChunk` is
the most rate-limited v3 endpoint — measured ceiling: a burst allowance of
~38 calls, refilling at only ~0.32 calls/s (19/min).

Calls are paced client-side to stay under that ceiling, so a cold pull no
longer stalls on `429`/`Retry-After: 60` — but the quota is the quota, and a
cold `--with-body` pass is bounded by it at roughly **3s per row** (400 rows
≈ 20 minutes). So keep `--limit` modest on a first pass over a big database,
or narrow it with `--filter` / `--edited-after`. Later passes come off the
cache at no API cost. A run that is interrupted still keeps every body it
rendered, so re-running resumes rather than starting over.

The cache cannot serve stale text — the page revision is part of the key —
and writes through this CLI drop the touched blocks' entries. Use
`--no-cache` to force a re-render, `NOTION_CLI_NO_CACHE=1` to disable it
entirely, and `notion cache stats` / `notion cache clear` to inspect or
reclaim it.

**On a repeat pass over the same database (a periodic digest, a daily
sync), add `--edited-after <date>` to `--with-body`** so bodies are fetched
only for rows that actually changed since the last pass, not every row every
time (verified on a real 181-row table: `--edited-after` narrowed it to 18).
It filters on the row's own `last_edited_time`, which Notion propagates up
from any edited descendant block — verified by recursively walking a page's
real children and confirming the row's own timestamp matched its
most-recently-edited descendant exactly — so it reliably catches
body-content edits, not just title/property changes.

### Write

```bash
# Create a row in a database — properties are schema-coerced from strings
notion create --parent <db_or_ds> \
  --prop 'Title=RFQ swap beta' --prop 'Status=Triage' \
  --prop 'Owner=user://<user-uuid>…' --prop 'Parent item=https://notion.so/<id>' \
  --prop 'Due=2026-07-31' --icon 💸 --md body.md

# List a database's templates, then clone one at create time
notion templates <db_or_ds>
notion create --parent <db_or_ds> --prop 'Title=…' --template 'AI new item'
# --template clones the template's body SYNCHRONOUSLY into the create
# transaction (no async placeholder race). Fill the cloned placeholders after
# with `edit`/`append`; --md/--body appends beneath the cloned body.

# Update properties (empty value clears; date ranges as start..end)
notion update <page> --prop 'Status=Done' --prop 'Due='
notion update <page> --archive

# Append markdown (headings, - / 1. lists, - [ ] todos, > quotes,
# > [!💸:blue_bg] callouts, ``` fences, | tables |, --- dividers;
# inline: **bold**, `code`, [label](url), @user(uuid), @page(uuid-or-url),
#         @[label](url) = link-mention chip, @[label](url "Provider") to override)
# A GFM table on a page that already has one table with the same columns is
# merged into it (new rows go above a trailing "Running total" row).
notion append <page> "one liner"
notion append <page> --md notes.md
cat notes.md | notion append <page> --md -
notion append <page> --md - <<'MD'
| 2026-08-25 | 39,000 |  | [Slack](https://...) |
MD

# In-place text replace (preserves formatting; unique match required unless --all).
# Searches every property, including table cells — not just block titles.
# A unique snippet of the table as `page` renders it rewrites rows (insert/delete/update).
# Mentions match as `page` renders them (`@Ada`). Prefer --section over guessing
# the current paragraph; on no match, edit prints a short page preview.
notion edit <page> "1,296,000" "1,335,000"
notion edit <page> --section "1. What" --md what.md
notion edit <page> --section "2. Crew" --md - <<'MD'
| Role | Owner |
|---|---|
| Vault launch lead | @Clement Walter |
MD
notion rewrite <page> --md body.md   # replace the whole page body (keeps properties)

# Many database rows in one call (optional md/body/icon keys; rest = properties)
notion create --parent <db> --jsonl rows.jsonl

notion comment <page> "done — see @page(<id>)"

# Toggle one to-do's checkbox without touching content or recreating the
# block (unlike --section/table-md, which rewrite everything in scope).
# Find the block id via `blocks <page>`.
notion check <block_id>              # tick
notion check <block_id> --uncheck    # clear

notion delete <page>                 # trash only — recoverable
notion delete-block <block_id>
```

## Notion-writes etiquette (project rules)

- Reference Notion pages inside bodies with `@page(<id>)` (renders as a real
  mention card) and people with `@user(<uuid>)` — never plain-text names or
  bare notion.so links.
- Reference anything **outside** Notion with `@[label](url)` — the link-mention
  chip a human sees as `<icon> Provider Label`, not a raw URL. Provider and icon
  are derived from the host (Linear, Google Docs/Sheets/Slides, Drive, Slack,
  GitHub, Figma, Dune, Etherscan); an unknown host keeps the label and drops the
  icon, which renders as Notion's own chain glyph. `@[label](url "Provider")`
  overrides the provider. A Notion URL is the one exception: use `@page(<id>)`,
  which tracks the page's live title and icon.
- New tracker-style rows: TL;DR callout (`> [!💸:blue_bg] …`) + `## Why` +
  `## What` todos + `## Sources`.
- Rewrite, don't stack: prefer `edit --section` (or search-replace) over
  `append` when updating existing content. Do not loop `create` for a
  batch of tracker rows — use `--jsonl`.
- `@user(uuid)` / `@page(id)` always write mentions. After any
  `page`/`query`/`users` call, unique cached display names also work
  (`@Clement Walter`, `--prop 'Owner=Clement Walter'`). `page --write`
  emits the uuid form if you need a guaranteed round-trip — and `@[label](url)`
  for link mentions, which a plain `page` renders as `[label](url)` so a digest
  reads cleanly.

## Gotchas

- **Unofficial API** (`api/v3`, the web client's own): endpoint names drift —
  writes go through `saveTransactionsFanout` today (`submitTransaction` is
  gone). If a write starts 404-ing with an HTML body, probe the sibling
  endpoint names before assuming breakage.
- **Filters/sorts run client-side** over flattened values: `=` exact, `~`
  case-insensitive contains, comparisons numeric when both sides parse as
  numbers (ISO dates compare correctly as strings). Multiple `--filter` AND.
- `query` needs a view on the database (any view); `schema` prints the
  collection + view ids it resolved.
- **Never hard-delete.** Regular delete only (`notion delete` / `delete-block`
  / `alive=false`). That is the user Trash action — the page stays visible
  and restorable. The CLI refuses `deleteBlocks` and any
  `permanentlyDelete` / `permanently_deleted_time` payload.
- Deleted pages still resolve (trash): `page` marks them `deleted: true`.
- `create --template` clones a template's body by deep-copying its blocks in
  the same transaction (templates are just pages with `is_template: true`);
  it does NOT call the API's async template instantiation, so there is no
  placeholder-stacking race. Auto-increment IDs are still assigned lazily by
  the server.
- Formula/rollup values are computed server-side and not stored on the row
  record — they flatten to None.
- Simple-table cells live in `table_row.properties[<column-id>]`, not
  `title`. `edit` searches all properties (so a running-total cell matches)
  and can splice the GFM `page` renders for a table to insert/delete rows.
  `append` of a `| table |` creates a real Notion table; if the page already
  has exactly one table with the same column count, rows are merged into it.
- `page --depth` default is 6; deeper nesting truncates with an explicit
  `[…children truncated]` marker rather than silently.
- The client honors `Retry-After` and backs off on 429/5xx. If Notion
  omits `Retry-After`, wait is 8/16/32/60s (not 1/2/4s).
- `edit` of a GFM table re-parses every cell through `md_to_segments`.
  `@Name` stays a mention when that user is in the id cache; otherwise
  pass `@user(uuid)` or `page --write` first.

## Tests

```bash
uv run --with pytest --with click --with requests -- pytest tests/ -q
```

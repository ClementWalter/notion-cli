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
notion page <id_or_url> --no-cache   # re-render — the check to run right after a block-level edit
notion pages <id1> <id2> <id3> ...   # multiple pages' bodies in ONE call — batches what would
                                             # otherwise be one `page` call per id (content only, no props)

# Database / data source query — accepts db url, data-source id, collection://…
notion query <db_or_ds> --select ID,Title,Status
notion query <db_or_ds> --filter 'Status=Done' --filter 'ID>195'
notion query <db_or_ds> --filter 'Title~vault' --sort 'ID:desc' --limit 20
notion query <db_or_ds> --filter 'Due is_empty' --json
notion query <db_or_ds> --filter 'Status=In progress' --with-body   # + every matched row's
                                             # full page body, in the SAME call — see below
notion query <db_or_ds> --edited-after 2026-07-20 --with-body   # bodies ONLY for rows whose
                                             # last_edited_time moved since a cutoff — narrows a repeat
                                             # pass; not a change detector on its own (see below)

# Filter DSL: =  !=  >  >=  <  <=  ~ (contains)  'Prop is_empty'  'Prop is_not_empty'
# Applied client-side on flattened values (numeric-aware; ~ is case-insensitive).
# Relation cells: text mode prints `#<ID> <Title>` when the target row is in the
# same result set (else its url); --json keeps comma-separated notion.so urls.
# A deleted row is simply absent (no tombstone) — confirm with `page <id> --props-only`.

notion schema <db_or_ds>       # property name → type (+ collection/view ids); types only, no option lists
notion search "vault launch" --limit 10
notion search "Clément" --created-after 2026-08-01   # client-side filter on creation time — the
                                             # closest proxy to a notification inbox (v3 has none);
                                             # catches new pages only, not edits or mentions in old ones
notion comments <page_id>      # every page-level + inline discussion, open AND resolved (public API
                               # hides resolved): [OPEN]/[resolved], anchored block id, author, datetime
notion comments <page_id> --open-only
notion users [query]           # space permission grants (name, email, id) — NOT every member
notion resolve <id> [<id> ...] # id -> name/title, local cache first, one API call max per new id
notion blocks <page_id>        # child block ids (targets for edit/check/delete-block); default depth 1
                               # — keep it there on long pages, see Gotchas
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

The page revision is part of the key, and writes through this CLI drop the
entries of the ids they touch. One window stays open: a write aimed at a
nested block id (`edit <block_id> "old" "new"`, `append <block_id>`) drops
that block's entry, not the containing page's, whose revision Notion bumps
only server-side — so `page` run right after can still render the pre-edit
text. Verify such an edit with `blocks <block_id>` (reads the block record
directly) or `page --no-cache`. `NOTION_CLI_NO_CACHE=1` disables the cache
entirely; `notion cache stats` / `notion cache clear` inspect or reclaim it.

**On a repeat pass over the same database (a periodic digest, a daily
sync), add `--edited-after <date>` to `--with-body`** so bodies are fetched
only for rows whose `last_edited_time` moved since the last pass, not every
row every time (measured on a 181-row table: 18 bodies fetched instead of
181). Edits made in the Notion app propagate from any descendant block up to
the row's own timestamp, so in-app body edits are caught. It is a narrowing
filter, not a change detector: block-level writes made through this CLI
(`edit`, `append`, `edit --section` on a row) can leave the row's timestamp
untouched, and a board-view drag or similar non-content interaction can bump
it with nothing changed. To guarantee no silent change was missed, diff the
properties of a full `query` read against the previous run's.

### Write

```bash
# Create a row in a database — properties are schema-coerced from strings
notion create --parent <db_or_ds> \
  --prop 'Title=RFQ swap beta' --prop 'Status=Triage' \
  --prop 'Owner=user://<user-uuid>…' --prop 'Parent item=https://notion.so/<id>' \
  --prop 'Due=2026-07-31' --icon 💸 --md body.md
# Owner ids are NOTION user ids (a Slack `U…` id fails with an opaque
# `400 incomplete_ancestor_path`); a wrong-but-valid id resolves silently, so
# re-read `--props-only` and check the resolved name.
# A callout's colour (`> [!💸:blue_bg]`) is only settable inside this create
# transaction; --icon is unreliable (silent no-op on many rows). See Gotchas.

# List a database's templates, then clone one at create time
notion templates <db_or_ds>
notion create --parent <db_or_ds> --prop 'Title=…' --template 'AI new item'
# --template clones the template's body SYNCHRONOUSLY into the create
# transaction (no async placeholder race). Fill the cloned placeholders after
# with `edit`/`append`; --md/--body appends beneath the cloned body. The clone
# keeps the template's callout colour, which cannot be changed afterwards.

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
# Anchor on plain text; `--` when old/new start with '-'. See "Editing bodies safely".
notion edit <page> "1,296,000" "1,335,000"
notion edit <page> -- "- [ ] ship it" "- [ ] ship it by Friday"
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
notion comment <page> "answered" --discussion <discussion_id>   # reply into an existing thread

# Close (or reopen) a comment thread. Takes a discussion id as printed by
# `comments <page>` (the `discussion <id> [OPEN|resolved]` line), not a
# comment id.
notion resolve-discussion <discussion_id>
notion resolve-discussion <discussion_id> --reopen

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
  icon, which renders as Notion's own chain glyph — that is the intended
  fallback; pointing at a guessed `/favicon.ico` yields a broken image instead.
  `@[label](url "Provider")` overrides the provider. A Notion URL is the one
  exception: use `@page(<id>)`, which tracks the page's live title and icon.
- A plain `@Name` in markdown is not converted unless that name is in the id
  cache (below) — it silently lands as text. Only `@page(<id>)`, `@user(<id>)`
  and cached unique names become mentions.
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
  reads cleanly. Rewriting a body from plain `page` output therefore downgrades
  every chip to a hyperlink — always round-trip from `--write`.

## Editing bodies safely

`edit <page> "old" "new"` matches raw rich-text runs, and the CLI has no
insert-at-position primitive. Consequences:

- **`old` must be plain text.** Bold markers, `[label](url)` brackets and a
  code span straddling runs are reconstructed from run metadata, not literal
  characters: such an anchor returns `no match` or
  `match spans formatting boundaries`. Anchor on a bare word or date and narrow
  from there.
- **`old`/`new` starting with `-`** (a `- [ ]` todo line) need `--` before them
  (Click option parsing).
- **A plain-text prefix of a bullet that continues into a link matches only
  that prefix.** The bullet's tail is stitched onto `new` with no separator
  while `edit` still reports `replaced in 1 block(s)`. Re-read any edited line
  that carries a link.
- **Link hrefs are never rewritten.** Matching the visible label relabels it
  only; matching the URL fails. To redirect a link, append a corrected one.
- **A heading's own text is a whole-block match.** A multi-paragraph `new`
  anchored on a heading lands inside the heading block, silently. Never anchor
  an insertion on a heading; `append` to the end or use `--section`.
- **`edit --section <heading> --md` rewrites the heading's sibling blocks up
  to the next heading**, never its children: on a toggle heading whose content
  is nested inside it, the new blocks land as siblings and the old children stay,
  so the page shows both. Check `blocks <page> --depth 2` first; when the
  content is indented under the heading, `delete-block` each child id after the
  rewrite. Nested `###` headings are not matchable by `--section` at all. When
  no heading follows the section, trailing non-heading content (dividers,
  footnotes) can be swallowed: re-read the full page after and re-append
  anything lost.
- **Tables:** match a snippet exactly as `page` renders it and pass the same
  snippet plus the new rows as `new`. Inserting before a trailing total row,
  backfilling mid-table and adding several rows in one call all work this way.
- **Checkboxes** are not text: `edit` cannot reach them. Use
  `check <block_id> [--uncheck]` with an id from `blocks <page>` at default
  depth.
- **Verify block-level writes** with `blocks <block_id>` or `page --no-cache`;
  a plain `page` can serve the pre-edit render (see the cache note above).

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
- Deleted pages still resolve (trash): `page` marks them `deleted: true`. A
  deleted row is simply absent from `query` — no tombstone, no status — so
  diff the ID column against a previous read and confirm each dropout with
  `page <id> --props-only`.
- `users [query]` lists the space's permission grants, not every member: an
  empty result is not absence. `Owner=user://<id>` needs a Notion user id (a
  Slack `U…` id fails with `400 incomplete_ancestor_path`, which reads like a
  parent error). Fallback: `page <row> --raw` on a row the person already owns
  and take the `u`-tagged segment. A profile page's `created_by_id` is its
  author, not its subject. A wrong id resolves silently — re-read
  `--props-only` after writing an Owner.
- `create --md`: the TL;DR callout colour is only settable via
  `> [!emoji:color_bg]` inside the create transaction — a later recolour fails
  with `incomplete_ancestor_path`, and omitting `:<color>_bg` renders white.
  `--icon` silently no-ops on many rows. `--md` can no-op on a reported
  success: re-read `--raw` and check `content` is non-empty and the first child
  is a `callout` with `format.block_color`.
- `create --template` clones a template's body by deep-copying its blocks in
  the same transaction (templates are just pages with `is_template: true`);
  it does NOT call the API's async template instantiation, so there is no
  placeholder-stacking race. Auto-increment IDs are still assigned lazily by
  the server. The clone's callout colour is fixed and cannot be changed after.
- `schema` prints types only. `status`/`select` option lists live in the raw
  collection record: `resolve_collection(api, ref)[0]["schema"][pid]["options"]`.
  A row's stored `status` can be a string no longer in that list (after an
  option rename/delete); `query` renders the raw string.
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
  omits `Retry-After`, wait is 8/16/32/60s (not 1/2/4s). Only `loadPageChunk`
  is paced client-side; other reads are single un-throttled v3 calls, so a
  tight loop of `page` calls or a deep `blocks` walk can still exhaust the
  retry budget.
- `blocks <page> --depth 4` on a long page recurses into every linked page to
  resolve titles and triggers sustained `syncRecordValues` 429s that outlast
  the retries — stay at the default depth. A hand-rolled recursion over
  `api.block(id)["content"]` hits the same wall.
- `api.block(id)` (direct use from Python) needs the dashed UUID form; the
  subcommands accept both dashed and bare 32-hex ids.
- `edit` of a GFM table re-parses every cell through `md_to_segments`.
  `@Name` stays a mention when that user is in the id cache; otherwise
  pass `@user(uuid)` or `page --write` first.
- A brand-new discussion (`comment` without `--discussion`) is always created
  `resolved: False` — there's no `create --resolved` shortcut; call
  `resolve-discussion` right after if it should start closed.

## Tests

```bash
uv run --with pytest --with click --with requests -- pytest tests/ -q
```

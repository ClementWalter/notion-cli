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
  "update notion row", "create notion page", "notion agents",
  "export notion agents".
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

Invoke it as **`notion`**, on `$PATH` through `claudine/scripts/link_clis.py`,
which links this repo's `bin/notion` into `~/.local/bin`. The link points at the
checkout, so a `git pull`, or even an uncommitted edit, takes effect at once.

```bash
notion search "query"
```

Examples in this doc are written that way. If `notion` is not on `$PATH`, run
`<skill-dir>/bin/notion` (a symlink to the PEP 723 script; `uv` resolves its
pinned dependencies on first run).

## Authentication

Preferred — automatic extraction from a local signed-in browser (decrypts the
Chrome/Arc/Brave/Edge/Notion-desktop cookie store via the macOS keychain;
stale sessions are skipped, the first token that validates wins; may pop one
keychain "Allow" dialog per app). Any command imports it on first use when no
session is saved, and an expired saved session is swapped once for the
browser's:

```bash
notion connect              # never prompts; binds the bound/first workspace, lists the others
notion connect --space "My Workspace" --json   # switch workspace
notion spaces               # workspaces the session can use (* = bound)
notion login                # same import, asks which workspace when several match
notion login --source arc --space "My Workspace"
```

`$NOTION_SPACE` (workspace name or id) picks the workspace for one run without
rebinding; it is the "Workspace" setting of the Claude Desktop extension.

Manual fallback — grab `token_v2` from a logged-in browser (devtools →
Application → Cookies → `https://app.notion.com` (SSO sessions) or
`https://www.notion.so` → `token_v2`, value starts with `v03:`), then:

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
Tokens survive ~1 year unless the user logs out; "Notion session expired"
means sign in at notion.so in the browser again and run `connect`.

Custom Agents (`/agent/{id}` URLs) use the same `token_v2` session as pages
(`getCustomAgents` + `workflow` records). A PAT is optional and only needed
to read/write `credit_limit`.

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
# `§ [title](url)` = a child page, `§db [name](url)` = a child/inline database.
# Their content is not inlined: read it with `page <url>` / `query <url>`.
# Read-only markers: see "Child pages and databases" (writes etiquette) before writing back.
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
notion search "Ada" --created-after 2026-08-01   # client-side filter on creation time — the
                                             # closest proxy to a notification inbox (v3 has none);
                                             # catches new pages only, not edits or mentions in old ones
notion comments <page_id>      # every page-level + inline discussion, open AND resolved (public API
                               # hides resolved): [OPEN]/[resolved], anchored block id, author, datetime
notion comments <page_id> --open-only
notion users [query]           # space permission grants (name, email, id) — NOT every member; can be empty —
                               # then `resolve` an id already seen in a page, query or prior output
# name -> uuid when `users` misses someone: no command does it. Write `@Full Name`
# (cached unique names become mentions, see etiquette), or read the id cache that
# every page/query/users call fills (a page or People column naming them puts them there):
jq -r '.users | to_entries[] | select(.value | test("ada"; "i")) | "\(.value)\t\(.key)"' \
  ~/.config/notion-cli/cache/id_names.json
# not cached yet: `page <a page that @-mentions them> --write` prints @user(uuid)
notion resolve <id> [<id> ...] # id -> name/title, local cache first, one API call max per new id
notion blocks <page_id>        # child block ids (targets for edit/check/delete-block); default depth 1
                               # — keep it there on long pages, see Gotchas
notion cache stats                 # rendered-body cache: entries, pages, size
notion cache clear [<page> ...]    # drop cached bodies (all, or just these pages)

# Custom Agents — same session cookie as pages. `/agent/{id}` is not a page.
notion agents list
notion agents get https://app.notion.com/agent/<id>
notion agents export agents/          # live → yaml + instruction .md + .state.json
notion agents plan  agents/           # diff only
notion agents apply agents/           # create + name/model/connections/triggers/status/instructions
```

`agents apply` creates missing agents (workflow + instructions page) and
writes **name**, **model**, **connections**, **triggers**, **status**, and
the **instructions page**. `credit_limit` still needs a PAT. Soft-delete
is not done here.

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
# A callout must carry a background (`> [!💸:blue_bg]`); a colourless one is
# refused. --icon is unreliable (silent no-op on many rows). See Gotchas.

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
# a TEXT argument starting with "-" (a bullet line) reads as an unknown
# option to Click ("No such option '- '") unless preceded by --:
notion append <page> -- "- a bullet line"
notion append <page> --md notes.md
# --top inserts above the page's first block instead: the way to put a TL;DR
# on a hub page whose inline databases and child pages a `rewrite` would trash
notion append <page> --top --md top.md
cat notes.md | notion append <page> --md -
notion append <page> --md - <<'MD'
| 2026-08-25 | 39,000 |  | [Slack](https://...) |
MD

# In-place text replace (preserves formatting; unique match required unless --all).
# Searches every property, including table cells — not just block titles.
# A unique snippet of the table as `page --write` renders it rewrites rows (insert/delete/update);
# mention cells (`@page(id)`) survive the rewrite, a plain `page` render does not match.
# Mentions match as `page` renders them (`@Ada`). Prefer --section over guessing
# the current paragraph; on no match, edit prints a short page preview.
# Anchor on plain text; `--` when old/new start with '-'. See "Editing bodies safely".
# An `old` that spans an inline-code span or a mention reports no match, so text
# that must follow a sentence ending in `code` or @user is added with --section.
notion edit <page> "1,296,000" "1,335,000"
notion edit <page> -- "- [ ] ship it" "- [ ] ship it by Friday"
notion edit <page> --section "1. What" --md what.md
# --md is a PATH (or '-' for stdin), never inline markdown: passing the text
# itself fails with `OSError: File name too long`, which reads like a bug and
# is not. `--body` is the inline form of the same payload on `edit` and
# `create`; `append` takes `--md` only.
notion edit <page> --section "2. Crew" --md - <<'MD'
| Role | Owner |
|---|---|
| Vault launch lead | @Ada Lovelace |
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

# Inline linked view of a database on a page: rows stay in the source
# database, the view only filters, sorts and picks columns. --filter takes the
# `query` DSL but Notion evaluates it (select/status: = !=; text/title/url:
# = != ~; number: = != > >= < <=; any type: is_empty / is_not_empty).
# Lands at the end of the page, at the end of a heading's section, or on top.
notion view <page> --db <db_or_ds> --name 'Morpho vaults' \
  --filter 'Vault Infra=Morpho' --sort Priority \
  --show 'Launch,Curator,Stage,Batch' --section 'Vaults in the pipeline'

notion delete <page>                 # trash only — recoverable
notion delete-block <block_id>
```

## Recipes

```bash
# "What changed on the tracker since Monday?" — bodies only for rows edited since
notion query <db_or_ds> --edited-after 2026-10-26 --with-body --select ID,Title,Status

# "Read these five pages" — one call, not one per id
notion pages <id1> <id2> <id3> <id4> <id5>

# "Who is this uuid?" — cache first, one API call at most
notion resolve <uuid>

# "Which comments on this page are still open?"
notion comments <page> --open-only

# "Mark this to-do done" — find the block id, then tick it
notion blocks <page>
notion check <block_id>

# "Put a TL;DR at the top of this hub page" without trashing its subpages
notion append <page> --top --md - <<'MD'
> [!💡:blue_bg] TL;DR — one sentence.
MD

# "Fix a number in this page" — then verify the block, not the cached render
notion edit <page> "1,296,000" "1,335,000"
notion page <page> --no-cache

# "Create these 12 tracker rows" — one transaction per row, one command
notion create --parent <db> --jsonl rows.jsonl

# "Close the discussion I just answered"
notion comment <page> "answered" --discussion <discussion_id>
notion resolve-discussion <discussion_id>

# "Show this database's rows for X on that page" — a live view, never a copied table
notion view <page> --db <db_or_ds> --filter 'Team=Vaults' --section 'Pipeline'

# Big pull: write to a file, filter there
notion query <db_or_ds> --json > rows.json && jq -r '.[].Status' rows.json | sort | uniq -c
```

## Gotchas

- **Unofficial API** (`api/v3`, the web client's own): endpoint names drift —
  writes go through `saveTransactionsFanout` today (`submitTransaction` is
  gone). If a write starts 404-ing with an HTML body, probe the sibling
  endpoint names before assuming breakage.
- **Filters/sorts run client-side** over flattened values: `=` exact, `~`
  case-insensitive contains, comparisons numeric when both sides parse as
  numbers (ISO dates compare correctly as strings). Multiple `--filter` AND.
- `view` filters run server-side, and Notion accepts any operator name: an
  unknown one returns no rows instead of an error. `view` therefore maps the DSL
  to the operators measured against known row counts, sends number values as
  numbers (a string value matches nothing), refuses a select/status value that
  is not an option, and refuses filters on other property types (person,
  relation, date, multi-select): set those in the app. It creates table views
  only. Property names are matched with surrounding spaces ignored, since a
  name like `" Batch"` is legal and invisible in the app. A `rewrite` or
  `edit --section` keeps the view in place.
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
  and take the `u`-tagged segment, or grep `~/.config/notion-cli/cache/id_names.json`
  (`users` bucket) for the name if any prior command already resolved it — no
  extra API call. Where the Notion MCP is connected, `notion-get-users` with a
  `query` returns name, email and id directly and is the one lookup that does
  not depend on a prior read. A profile page's `created_by_id` is its
  author, not its subject. A wrong id resolves silently, and even a correct
  cached uuid (`user://<id>` or bare `<id>`) on `update --prop 'Owner=…'` can
  silently no-op while reporting success — the cached display name
  (`Owner=Ada Lovelace`) went through reliably in the same case. Prefer the
  name over the uuid when both are available, and always re-read
  `--props-only` after writing an Owner.
- Callouts: every markdown callout must carry a background,
  `> [!emoji:<color>_bg]`. `create`, `append`, `edit --section` and `rewrite`
  refuse a callout without one, because a white callout is never wanted. The
  parser also writes `format.callout_version: 2`. Without it, Notion renders a
  callout white even when `block_color` is set, so `page --raw` can show a
  colour the page does not display. `page` renders the colour back
  (`> [!🌳:green_bg]`), so a `--write` round-trip keeps it.
  `--icon` silently no-ops on many rows. `--md` can no-op on a reported
  success: re-read `--raw` and check `content` is non-empty and the first child
  is a `callout` with `format.block_color`. A `status`-type property can also
  come back as the database's default rather than the value passed, on a create
  that reports success and sets every other property: re-read `--props-only`
  and set it again with `update --prop` when it did not stick.
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
  A `table_row` that carries `crdt_format_version` rejects any cell write,
  whether a single `properties.<col>` set or the whole `properties` dict, with
  `400 … Block property value updates must use high-level property
  operations`. Such a table's cells are edited through the Notion MCP
  `notion-update-page` `update_content`, where `old_str` is a unique
  `<td>…</td>` as `notion-fetch` renders it; the table block keeps its id.
  `check` and `update --prop` write through `updateBlockPropertyValue`, the
  op Notion's client uses, so they work on CRDT to-dos and CRDT database rows
  (every Projects row carries `crdt_format_version`) and keep the block id.
- `page --depth` default is 6; deeper nesting truncates with an explicit
  `[…children truncated]` marker rather than silently.
- HTTP reads time out at 60s, so a large `rewrite`/`create --md` can raise
  `ReadTimeout` after the write has already been submitted. The exception says
  nothing about whether it landed: re-read the page before retrying, never
  fire the same write again blind.
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
- A date mention renders in `page` as its bare ISO date, but it is its own
  segment: `edit` never matches text that includes it (`no match`), and a
  match ending next to it leaves the mention and its punctuation behind
  (`( 2026-10-26)`). Remove or rewrite it through the Notion MCP
  `notion-update-page` `update_content`, where `notion-fetch` renders it as
  `<date start="…" />`.
- `append "- item"` fails with `No such option '- '`: text that starts with a
  dash is read as a flag. Pass it through `--md <file>` (or `--md -`).
- `@[label](url)` in `create --md` / `append --md` writes a plain link, not the
  link-preview chip that existing `## Sources` entries carry: `page --write`
  then shows `[label](url)` without the `@`.
- A brand-new discussion (`comment` without `--discussion`) is always created
  `resolved: False` — there's no `create --resolved` shortcut; call
  `resolve-discussion` right after if it should start closed.

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
- A **Slack** message/channel URL, a **Linear** issue/project/document URL or a
  **GitHub** URL (file, PR, issue, repo) in `@[label](url)` becomes a native mention, the same pill as pasting the link
  in Notion and choosing "Mention": a Slack message shows author, text and
  `#channel`; a Linear issue shows key, title and status; a GitHub file shows
  its name and repo. The CLI calls
  Notion's `unfurlUrl` with the user's bot for that integration and writes the
  `external_object_instance` records itself; the label is ignored, the pill
  shows the object. If Notion cannot unfurl it (integration not connected, no
  access), the link falls back to a chip with a warning. `page` renders these
  pills as `@[title](url)` (`--write`) or `[title](url)`, so a round-trip
  recreates them. A plain `[label](url)` hyperlink to Slack, Linear or GitHub is
  refused with an error on every write path (append, create, edit, rewrite,
  tables, comments).
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
  (`@Ada Lovelace`, `--prop 'Owner=Ada Lovelace'`). `page --write`
  emits the uuid form if you need a guaranteed round-trip — and `@[label](url)`
  for link mentions, which a plain `page` renders as `[label](url)` so a digest
  reads cleanly. Rewriting a body from plain `page` output therefore downgrades
  every chip to a hyperlink — always round-trip from `--write`.
- **Child pages and databases** (`§` / `§db` lines, same in `--write`) do not
  round-trip. Written back, a `§` line becomes a plain paragraph with a link: it
  neither creates nor keeps a subpage. `rewrite` and `edit --section` trash every
  child page in scope (recoverable from trash) and keep inline databases in place
  (a `rewrite` body lands after them; `rewrite --force` trashes them too).
  `append` and `edit OLD NEW` never trash them. So drop the `§`/`§db` lines before
  a `rewrite`, and on a page with subpages prefer `edit --section` on a section
  without any, or `append --top`. A new subpage is
  `create --parent <page> --prop 'Title=…' [--md …]`, added at the end of the page.

## Editing bodies safely

`edit <page> "old" "new"` matches raw rich-text runs, and the only
positional inserts are `append` (end of page), `append --top` (start of page)
and `edit --section` (under a heading). Consequences:

- **A multi-line `new` does not create sibling blocks.** It's appended as
  literal newlines inside the one matched block, `## Heading` and `- [ ]` text
  included — there is no markdown parsing on this path. `page` renders those
  embedded newlines as if they were their own heading/bullet/to-do, so a
  `page`-only re-read looks like a clean insert when the underlying block is
  one run of text with `\n`s in it. Verify any edit that adds *new* lines
  (not just changes an existing one) with `blocks <page> --depth 2..4`, not
  `page`. To actually insert new blocks (a new section, new checklist items,
  a new nested bullet), use `edit --section <heading> --md` — reconstruct the
  full section (or, when the insertion point is nested under a non-heading
  list item with no heading above it to target, the smallest enclosing real
  heading) as markdown and pass the whole thing; the replacement is genuinely
  parsed into blocks. `--section` matches only true heading blocks, so a
  nested list item's own "heading-like" text (e.g. a numbered item titled
  "Distribute the rewards") is not targetable — target its parent heading
  instead and include every sibling in the `--md` payload.
- **`new` may carry inline markdown.** A partial in-segment match splices
  the parsed replacement in, so `@page(id)`, `@user(id)` and `[l](url)` land
  as real mentions/links and inherit the matched run's formatting (bold,
  italic). Verify with a direct `api.block(id)` read: a mention segment is
  `["‣", [["p", id], ...]]`, never the literal `@page(` text.
- **`old` must be plain text.** Bold markers, `[label](url)` brackets and a
  code span straddling runs are reconstructed from run metadata, not literal
  characters: such an anchor returns `no match` or
  `match spans formatting boundaries`. Anchor on a bare word or date and narrow
  from there.
- **Mention segments are not searchable text.** A page mention (`@page(id)`,
  rendered `[Title](url)`) and a link-mention chip's label (`@[label](url)`,
  stored as the `lm` title) never match `old` in any form. To repoint a page
  mention, use the Notion MCP `notion-update-page` `update_content` with
  `old_str` = the `<mention-page url="…"/>` tag as `notion-fetch` renders it.
  To relabel a chip, `delete-block` its bullet and `append` a new
  `@[label](url)` line.
- **`old`/`new` starting with `-`** (a `- [ ]` todo line) need `--` before them
  (Click option parsing).
- **A plain-text prefix of a bullet that continues into a link matches only
  that prefix.** The bullet's tail is stitched onto `new` with no separator
  while `edit` still reports `replaced in 1 block(s)`. The tail's own
  punctuation stays too, so `new` has to leave it balanced — a rewrite that
  drops the opening parenthesis of `(following [link])` leaves a stray `)`.
  Re-read any edited line that carries a link.
- **A plain segment between two links is unreachable when it repeats.** A
  separator such as `, ` between link runs is its own segment, so `old` cannot
  be made unique by including the neighbouring links. Use the Notion MCP
  `notion-update-page` `update_content` with an `old_str` that spans the
  rendered `[label](url)` links on both sides.
- **Link hrefs are never rewritten.** Matching the visible label relabels it
  only; matching the URL fails. To redirect a link, append a corrected one.
- **A heading's own text is a whole-block match.** A multi-paragraph `new`
  anchored on a heading lands inside the heading block, silently. Never anchor
  an insertion on a heading; `append` to the end or use `--section`.
- **`edit --section <heading> --md` keeps the heading block itself**: the
  payload is the section's content only. A payload that starts with the same
  `## Heading` lands as a second heading under the kept one, and the page shows
  it twice; `delete-block` the duplicate from `blocks <page>`.
- **`edit --section <heading> --md` rewrites the heading's sibling blocks up
  to the next heading**, never its children: on a toggle heading whose content
  is nested inside it, the new blocks land as siblings and the old children stay,
  so the page shows both. Check `blocks <page> --depth 2` first; when the
  content is indented under the heading, replace it with the Notion MCP
  `notion-update-page` `update_content` on the tab-indented lines under
  `# Heading {toggle="true"}` as `notion-fetch` renders them, which keeps the
  new blocks inside the toggle. A `###` heading *is* matchable by `--section` and replaces only its
  own siblings up to the next heading of any level, so a `### Slack channels`
  list under a `## See also` is editable on its own. A `## Heading` rewrite, by
  contrast, runs **through** any `###` sub-sections below it up to the next
  `##`: they are deleted with the rest, silently. So to touch one sub-list
  under a `##`, target its `###`; only when the whole `##` must change does
  `rewrite` of the full body (from a `--write` render) beat `--section`. When
  no heading follows the section, trailing non-heading content (dividers,
  footnotes) can be swallowed: re-read the full page after and re-append
  anything lost.
- **Tables:** match a snippet exactly as `page --write` renders it (a mention
  cell is `@page(id)`, not `[page](url)`) and pass the same snippet plus the new
  rows as `new`. Inserting before a trailing total row,
  backfilling mid-table and adding several rows in one call all work this way.
  Changing a cell of a row that carries `crdt_format_version` fails with
  `400 … Block property value updates must use high-level property
  operations`, whatever the snippet, so don't retry `edit` on it. Edit the cell
  with the Notion MCP `notion-update-page` `update_content`, with `old_str` set
  to a unique `<td>…</td>` exactly as `notion-fetch` renders it (see the
  Simple-table entry in Gotchas).
- **Checkboxes** are not text: `edit` cannot reach them. Use
  `check <block_id> [--uncheck]` with an id from `blocks <page>` at default
  depth.
- **Verify block-level writes** with `blocks <block_id>` or `page --no-cache`;
  a plain `page` can serve the pre-edit render (see the cache note above).

## Tests

Each test file is a PEP 723 script with pinned dependencies:

```bash
for f in tests/test_*.py; do uv run --script "$f" -q; done
```

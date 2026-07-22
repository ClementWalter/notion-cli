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

## Running

```bash
uv run ~/.claude/skills/notion-cli/notion_cli.py <command> [options]
```

## Authentication

Preferred — automatic extraction from a local logged-in app (decrypts the
Notion-desktop/Chrome/Arc/Brave cookie store via the macOS keychain; stale
sessions are skipped, the first token that validates wins; may pop one
keychain "Allow" dialog per app):

```bash
notion_cli.py login                # try all known cookie stores
notion_cli.py login --source arc --space "My Workspace"
```

Manual fallback — grab `token_v2` from a logged-in browser (devtools →
Application → Cookies → `https://www.notion.so` → `token_v2`, value starts
with `v03:`), then:

```bash
notion_cli.py auth                 # prompts for token_v2, hidden input
notion_cli.py auth --import        # reuse ~/.config/notion-reader/config.json
notion_cli.py auth --space "My Workspace"    # disambiguate when the login has several workspaces
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
notion_cli.py page <id_or_url>              # props + body
notion_cli.py page <id_or_url> --props-only # cheapest read
notion_cli.py page <id_or_url> --no-props   # body only
notion_cli.py page <id_or_url> --depth 2    # cap nested-children recursion

# Database / data source query — accepts db url, data-source id, collection://…
notion_cli.py query <db_or_ds> --select ID,Title,Status
notion_cli.py query <db_or_ds> --filter 'Status=Done' --filter 'ID>195'
notion_cli.py query <db_or_ds> --filter 'Title~vault' --sort 'ID:desc' --limit 20
notion_cli.py query <db_or_ds> --filter 'Due is_empty' --json

# Filter DSL: =  !=  >  >=  <  <=  ~ (contains)  'Prop is_empty'  'Prop is_not_empty'
# Applied client-side on flattened values (numeric-aware; ~ is case-insensitive).

notion_cli.py schema <db_or_ds>       # property name → type (+ collection/view ids)
notion_cli.py search "vault launch" --limit 10
notion_cli.py comments <page_id>      # discussions INCL. RESOLVED (public API can't)
notion_cli.py comments <page_id> --open-only
notion_cli.py users [query]           # workspace members (name, email, id)
notion_cli.py resolve <id> [<id> ...] # id -> name/title, local cache first, one API call max per new id
notion_cli.py blocks <page_id> --depth 2   # block ids (targets for edit/delete)
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
save it once (`notion_cli.py page <id> --no-props > page.md`) and grep the
file, don't re-run `page` for every subsequent check.

### Write

```bash
# Create a row in a database — properties are schema-coerced from strings
notion_cli.py create --parent <db_or_ds> \
  --prop 'Title=RFQ swap beta' --prop 'Status=Triage' \
  --prop 'Owner=user://<user-uuid>…' --prop 'Parent item=https://notion.so/<id>' \
  --prop 'Due=2026-07-31' --icon 💸 --md body.md

# List a database's templates, then clone one at create time
notion_cli.py templates <db_or_ds>
notion_cli.py create --parent <db_or_ds> --prop 'Title=…' --template 'AI new item'
# --template clones the template's body SYNCHRONOUSLY into the create
# transaction (no async placeholder race). Fill the cloned placeholders after
# with `edit`/`append`; --md/--body appends beneath the cloned body.

# Update properties (empty value clears; date ranges as start..end)
notion_cli.py update <page> --prop 'Status=Done' --prop 'Due='
notion_cli.py update <page> --archive

# Append markdown (headings, - / 1. lists, - [ ] todos, > quotes,
# > [!💸:blue_bg] callouts, ``` fences, | tables |, --- dividers;
# inline: **bold**, `code`, [label](url), @user(uuid), @page(uuid-or-url))
notion_cli.py append <page> "one liner"
notion_cli.py append <page> --md notes.md
cat notes.md | notion_cli.py append <page> --md -

# In-place text replace (preserves formatting; unique match required unless --all)
notion_cli.py edit <page> "1,296,000" "1,335,000"

notion_cli.py comment <page> "done — see @page(<id>)"
notion_cli.py delete-block <block_id>
```

## Notion-writes etiquette (project rules)

- Reference Notion pages inside bodies with `@page(<id>)` (renders as a real
  mention card) and people with `@user(<uuid>)` — never plain-text names or
  bare notion.so links.
- New tracker-style rows: TL;DR callout (`> [!💸:blue_bg] …`) + `## Why` +
  `## What` todos + `## Sources`.
- Rewrite, don't stack: prefer `edit` (search-replace) over `append` when
  updating existing content.

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
- Deleted pages still resolve (trash): `page` marks them `deleted: true`.
- `create --template` clones a template's body by deep-copying its blocks in
  the same transaction (templates are just pages with `is_template: true`);
  it does NOT call the API's async template instantiation, so there is no
  placeholder-stacking race. Auto-increment IDs are still assigned lazily by
  the server.
- Formula/rollup values are computed server-side and not stored on the row
  record — they flatten to None.
- `page --depth` default is 6; deeper nesting truncates with an explicit
  `[…children truncated]` marker rather than silently.
- The client honors `Retry-After` and backs off on 429/5xx.

## Tests

```bash
uv run --with pytest --with click --with requests -- pytest tests/ -q
```

# notion-cli

Terminal access to Notion using your browser session cookie (`token_v2`). No
workspace integration, no OAuth flow, no admin approval — it reuses the login
already on your machine (Notion desktop app or any Chromium browser) and talks
to the same private API the Notion web client uses. You see exactly what your
account sees, including things the official API hides (resolved comments).

Built for two audiences at once:

- **humans** get a compact, pipeable CLI (`grep`-able lines, `--json`
  everywhere);
- **agents** get token-efficient Notion access — pages render as lean
  markdown with the ~2KB signed-image URLs stripped, database queries project
  only the columns you `--select`, and every output can be filtered through
  `jq`/`python` *before* it enters a model's context. Reading a 170-row
  database costs ~18KB instead of the ~50KB of raw API JSON.

The tool is exposed both as a standalone CLI and as a
[Claude Code](https://claude.com/claude-code) skill (see
[`SKILL.md`](SKILL.md)).

## Prerequisites

[`uv`](https://docs.astral.sh/uv/) runs the script and resolves its Python
dependencies on demand:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# or via Homebrew
brew install uv
```

The `npx skills` install path additionally requires Node.js (for `npx`).

## Install as a Claude Code skill

Recommended path. Uses [`npx skills`](https://github.com/vercel-labs/skills)
to drop the skill into `~/.claude/skills/notion-cli/`:

```bash
npx skills add ClementWalter/notion-cli
```

After install, Claude Code picks it up automatically — see
[`SKILL.md`](SKILL.md) for what the skill exposes.

To use it from any directory, put the launcher on `$PATH` — the symlink points at
the checkout, so a `git pull` is all an upgrade takes:

```bash
ln -sfn ~/.claude/skills/notion-cli/bin/notion ~/.local/bin/notion
```

## Install as a standalone CLI

The CLI is a single-file Python script with
[PEP 723](https://peps.python.org/pep-0723/) inline metadata, so
[`uv`](https://docs.astral.sh/uv/) handles dependencies on the fly:

```bash
notion --help
```

To use it from any directory, put the launcher on `$PATH` — the symlink points at
the checkout, so a `git pull` is all an upgrade takes:

```bash
ln -sfn /path/to/notion-cli/bin/notion ~/.local/bin/notion
```

## Authentication

Credentials are stored chmod-600 in `~/.config/notion-cli/config.json`.

```bash
# Auto-extract from the Notion desktop app / Chrome / Arc / Brave
# (decrypts the cookie store via the macOS Keychain; stale sessions are
# skipped — the first token that validates against the API wins)
notion login

# Pick the store and workspace explicitly
notion login --source arc --space "My Workspace"

# Paste token_v2 manually (browser devtools → Application → Cookies →
# notion.so → token_v2)
notion auth
```

`login`/`auth` bind a (user, workspace) pair — required because a session that
knows several accounts gets empty results without the right active-user
header. `notion whoami` shows the current binding. Session tokens live ~1 year
unless you log out; on `401 — token_v2 expired`, just `login` again.

## Usage

```bash
# Read
notion page <url-or-id>                    # properties + body as compact markdown
notion page <url-or-id> --write            # @user(uuid)/@page(uuid) so the body can be written back
notion page <url-or-id> --props-only       # cheapest possible read
notion pages <id> <id> <id> ...             # multiple pages' bodies in ONE call, not one `page` call each
notion query <db> --select ID,Status,Title --filter 'Status=In progress' --sort ID
notion query <db> --filter 'Due<2026-08-01' --filter 'Status!=Done'   # ANDed
notion query <db> --filter 'Status=Done' --with-body   # + every matched row's full page body, still one call
notion query <db> --edited-after 2026-07-20 --with-body  # bodies only for rows changed since a cutoff — not all of them
notion cache stats                         # rendered-body cache: entries, pages, size
notion cache clear [<page> ...]            # drop cached bodies (all, or just these pages)
notion schema <db>                         # property name → type
notion search "quarterly launch plan"
notion comments <page>                     # discussions INCL. resolved ones
notion users [query]
notion resolve <id> [<id> ...]              # id -> name/title, cached locally, no full listing

# Write
notion create --parent <db> --prop 'Title=New row' --prop 'Status=Triage' \
  --prop 'Owner=user://<uuid>' --icon 🚀 --md body.md
notion create --parent <db> --jsonl rows.jsonl   # many rows, one command
notion templates <db>                      # list templates
notion create --parent <db> --prop 'Title=…' --template 'AI new item'   # clone one
notion update <page> --prop 'Status=Done' --prop 'Due='   # empty value clears
notion append <page> --md notes.md         # markdown incl. callouts, todos, @user()/@page() mentions
notion edit <page> "old text" "new text"   # in-place replace; prints a preview on no match
notion edit <page> --section "1. What" --md what.md   # replace a heading's body
notion rewrite <page> --md body.md         # replace the whole page body
notion comment <page> "ping @user(<uuid>)"
notion check <block-id>                    # tick a to-do's checkbox (find id via `blocks <page>`)
notion check <block-id> --uncheck          # clear it
notion delete <page>                       # trash (recoverable). never hard-deletes
notion delete-block <block-id>
```

Filters run client-side over flattened values (`=`, `!=`, `>`, `>=`, `<`,
`<=`, `~` contains, `Prop is_empty`) — numeric-aware, so `--filter 'ID>195'`
does what you mean. Relation cells pointing at rows of the same query render
as `#<ID> <Title>` so parent/sub-item hierarchy stays visible in flat output.

Every command supports `--json` for structured output; `--raw` dumps the
untouched API records when you need to debug.

`page`/`query`/`users` all persist every id→name/title pair they discover to
a local cache (`~/.config/notion-cli/cache/id_names.json`, no TTL — Notion
ids are immutable and never reused). `resolve <id>` reads that cache first,
so naming an id you've already seen (a user, or a page) costs nothing — only
a genuinely new id triggers one batched API call, never a full listing. This
is what to reach for instead of re-running `users "<name>"` (a full
workspace-member fetch every time) just to look up one id.

Rendered page bodies are cached too, in
`~/.config/notion-cli/cache/bodies.sqlite3`, keyed by page id + `--depth` +
`--write` and validated against the page's `last_edited_time`. Every body
caller already holds that timestamp (`page`/`pages` from the record fetch,
`query` from the query's own recordMap), so a hit costs **zero** extra API
calls, and a stale body can't be served — the revision is part of the key,
not a TTL guess. This matters because `loadPageChunk`, the endpoint that
fetches a body, is the most aggressively rate-limited one on the v3 API: a
few dozen back-to-back calls earn a `429` with `Retry-After: ~60`, so a
large `--with-body` pull that re-reads unchanged pages stalls for a minute
at a time. Re-reading 30 rows measured 30 `loadPageChunk` calls / 24.4s cold
vs 0 calls / 2.0s warm, byte-identical output.

Pass `--no-cache` to force a re-render, `NOTION_CLI_NO_CACHE=1` to disable
the cache process-wide, and `notion cache stats` / `notion cache clear
[<page> ...]` to inspect or reclaim it. Writes drop the cached bodies of
every block they touch, so an edit made through this CLI is visible
immediately.

Calls to `loadPageChunk` are also paced client-side, so a cold pull settles
to a rate Notion tolerates instead of walking into that 60s penalty. The
limit was measured rather than guessed: bursting until it 429'd at two
different rates (43 calls before the wall at 16s, 59 before it at 67s) fits
a token bucket of capacity ~38 refilling at ~0.32 calls/s, and the bucket
here is sized just under that. It starts full, so a one-off read never
waits — only a run longer than the burst allowance pays, and only what the
quota costs anyway. Measured on 60 uncached pages: 84s with zero 429s,
against 87s and a 60s stall without pacing. A positive `Retry-After` is now
honored exactly too, instead of being rounded up to an 8/16/32/60 floor.

**Avoid one `page` call per row.** Fetching a database's rows and then each
row's body separately is the single biggest source of avoidable round-trips
in a typical read-heavy session (measured: 60 separate `page` calls in one
run, one per tracker row). `query --with-body` folds all of it into one
command — the query itself is one call, then one `loadPageChunk` per matched
row (cached, so repeat passes cost nothing); `pages <id> <id> ...` does the
same for any other already-known set of ids.

**Don't re-fetch bodies for rows that haven't changed.** If this is a
repeat pass over the same database (a daily digest, a periodic sync),
`--edited-after <date>` narrows `--with-body` to only the rows whose
`last_edited_time` is at/after that cutoff (verified: 18 of 181 rows on a
real table) instead of paying for every row's body every time. Notion
propagates a body-content edit's timestamp up to the row's own
`last_edited_time` (verified: a row's own timestamp exactly matched its
most-recently-edited descendant block, recursively), so this catches real
content changes, not just title/property edits.

For big pulls, redirect to a file and slice it instead of re-reading everything:

```bash
notion query <db> --json > rows.json && jq -r '.[].Status' rows.json | sort | uniq -c
```

See [`SKILL.md`](SKILL.md) for the full command reference, the markdown
subset accepted by writes, and the gotchas (endpoint drift, lazily-assigned
auto-increment IDs, formula/rollup limits).

## Running tests

```bash
uv run --with pytest --with click --with requests --with pycryptodome -- pytest tests/ -q
```

Tests cover the pure conversion layers (segment rendering, markdown parsing,
filter DSL, property coercion) and never touch a real workspace.

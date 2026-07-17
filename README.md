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

To also use it directly from a terminal, alias the installed copy:

```bash
alias notion='uv run ~/.claude/skills/notion-cli/notion_cli.py'
```

## Install as a standalone CLI

The CLI is a single-file Python script with
[PEP 723](https://peps.python.org/pep-0723/) inline metadata, so
[`uv`](https://docs.astral.sh/uv/) handles dependencies on the fly:

```bash
uv run notion_cli.py --help
```

For convenience, alias it:

```bash
alias notion='uv run /path/to/notion-cli/notion_cli.py'
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
notion page <url-or-id> --props-only       # cheapest possible read
notion query <db> --select ID,Status,Title --filter 'Status=In progress' --sort ID
notion query <db> --filter 'Due<2026-08-01' --filter 'Status!=Done'   # ANDed
notion schema <db>                         # property name → type
notion search "quarterly launch plan"
notion comments <page>                     # discussions INCL. resolved ones
notion users [query]

# Write
notion create --parent <db> --prop 'Title=New row' --prop 'Status=Triage' \
  --prop 'Owner=user://<uuid>' --icon 🚀 --md body.md
notion update <page> --prop 'Status=Done' --prop 'Due='   # empty value clears
notion append <page> --md notes.md         # markdown incl. callouts, todos, @user()/@page() mentions
notion edit <page> "old text" "new text"   # in-place replace, formatting preserved
notion comment <page> "ping @user(<uuid>)"
notion delete-block <block-id>
```

Filters run client-side over flattened values (`=`, `!=`, `>`, `>=`, `<`,
`<=`, `~` contains, `Prop is_empty`) — numeric-aware, so `--filter 'ID>195'`
does what you mean. Relation cells pointing at rows of the same query render
as `#<ID> <Title>` so parent/sub-item hierarchy stays visible in flat output.

Every command supports `--json` for structured output; `--raw` dumps the
untouched API records when you need to debug. For big pulls, redirect to a
file and slice it instead of re-reading everything:

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

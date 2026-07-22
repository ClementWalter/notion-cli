#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "click", "pycryptodome"]
# ///
"""Token-efficient Notion CLI on the session-token (v3) API.

Authenticates like the Notion web client itself — with the user's `token_v2`
browser cookie — so it sees exactly what the user sees and needs no workspace
integration (mirrors the slack-user-cli model). Reads render pages/queries as
*compact* text so only the information an agent actually needs enters its
context; writes go through `submitTransaction` with schema-aware coercion.

Data model notes (differs from the public API):
- records live in tables (block, collection, discussion, comment, notion_user)
  fetched via syncRecordValues / loadPageChunk; values may be double-nested
  (`{"value": {"value": {...}}}`) and are normalized on access;
- rich text is segment-encoded: `[["text", [["b"]]], ["‣", [["u", uuid]]]]`;
- database rows are blocks whose `properties` are keyed by schema property id.

Output conventions:
- default: compact human/agent-readable text (stable, line-oriented)
- --json:  flattened JSON (properties reduced to plain values)
- --raw:   untouched API JSON (debugging only — token-expensive)
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, Iterator

import click
import requests

log = logging.getLogger("notion-cli")

API_BASE = "https://www.notion.so/api/v3"
CONFIG_PATH = Path.home() / ".config" / "notion-cli" / "config.json"
LEGACY_TOKEN_PATH = Path.home() / ".config" / "notion-reader" / "config.json"
ID_NAMES_PATH = Path.home() / ".config" / "notion-cli" / "cache" / "id_names.json"


# --------------------------------------------------------------------------
# config / http
# --------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    # The file holds the session token — keep it out of reach of other users.
    CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --------------------------------------------------------------------------
# id → name/title cache
#
# Notion ids are immutable and never reused, so — mirroring slack-user-cli's
# permanent id_names.json store — this is a no-TTL cache: once an id has been
# resolved to a name/title (by any command, as a side effect), it's reused
# forever with no further API call. Kept separate from CONFIG_PATH (which
# holds the auth token) since this file is safe to share/inspect/delete.
# --------------------------------------------------------------------------


def load_id_cache() -> dict[str, dict[str, str]]:
    if not ID_NAMES_PATH.is_file():
        return {"users": {}, "pages": {}}
    try:
        data = json.loads(ID_NAMES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {"users": {}, "pages": {}}
    data.setdefault("users", {})
    data.setdefault("pages", {})
    return data


def save_id_cache(data: dict[str, dict[str, str]]) -> None:
    ID_NAMES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ID_NAMES_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(ID_NAMES_PATH)


def merge_id_cache(cache: dict[str, dict[str, str]], kind: str, mapping: dict[str, str]) -> bool:
    """Merge newly-learned id→name/title pairs into `cache` in place.
    Returns True if anything new was added (so callers only write to disk
    when there's actually something to persist)."""
    changed = False
    bucket = cache[kind]
    for rid, name in mapping.items():
        if name and bucket.get(rid) != name:
            bucket[rid] = name
            changed = True
    return changed


def cache_names(kind: str, mapping: dict[str, str]) -> None:
    """Load-merge-save in one call, for callers that just learned some
    id→name/title pairs as a side effect of an unrelated fetch."""
    if not mapping:
        return
    cache = load_id_cache()
    if merge_id_cache(cache, kind, mapping):
        save_id_cache(cache)


class Api:
    """v3 transport: token_v2 cookie + active-user header, retry on 429/5xx.

    The active-user header is REQUIRED when the session knows several Notion
    accounts — without it the API returns empty, permission-filtered results
    (HTTP 200 with no records), which reads like missing content but isn't.
    """

    def __init__(self, cfg: dict):
        token = os.environ.get("NOTION_TOKEN_V2") or cfg.get("token_v2")
        if not token:
            raise click.ClickException(
                "No token. Run `notion_cli.py auth` (paste token_v2 from the "
                "browser: devtools → Application → Cookies → notion.so → "
                "token_v2), or `auth --import` to reuse a stored one."
            )
        self.space_id = cfg.get("space_id")
        self.user_id = cfg.get("user_id")
        self.s = requests.Session()
        self.s.headers.update({"Cookie": f"token_v2={token}", "Content-Type": "application/json"})
        if self.user_id:
            self.s.headers["x-notion-active-user-header"] = self.user_id

    def post(self, path: str, body: dict, *, retries: int = 5) -> dict:
        for attempt in range(retries):
            resp = self.s.post(f"{API_BASE}/{path}", json=body, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("%s -> %s, retrying in %.1fs", path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise click.ClickException("401 — token_v2 expired; re-run `auth`")
            if not resp.ok:
                raise click.ClickException(f"{resp.status_code} on {path}: {resp.text[:300]}")
            return resp.json()
        raise click.ClickException(f"still failing after {retries} retries: {path}")

    # ---- records ----------------------------------------------------------

    def records(self, table: str, ids: list[str]) -> dict[str, dict]:
        """Fetch records by id; returns {id: normalized_value}."""
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            d = self.post(
                "syncRecordValues",
                {"requests": [{"pointer": {"table": table, "id": rid, "spaceId": self.space_id}, "version": -1} for rid in chunk]},
            )
            for rid, wrap in d.get("recordMap", {}).get(table, {}).items():
                v = unwrap(wrap)
                if v:
                    out[rid] = v
        return out

    def load_page(self, page_id: str) -> dict[str, dict[str, dict]]:
        """loadPageChunk until exhausted; returns merged {table: {id: value}}."""
        tables: dict[str, dict[str, dict]] = {}
        cursor: dict = {"stack": []}
        chunk = 0
        while True:
            d = self.post(
                "loadPageChunk",
                {"pageId": page_id, "limit": 100, "cursor": cursor, "chunkNumber": chunk, "verticalColumns": False},
            )
            for table, recs in d.get("recordMap", {}).items():
                if not isinstance(recs, dict):  # e.g. __version__ is an int
                    continue
                dst = tables.setdefault(table, {})
                for rid, wrap in recs.items():
                    v = unwrap(wrap)
                    if v:
                        dst[rid] = v
            cursor = d.get("cursor") or {"stack": []}
            chunk += 1
            if not cursor.get("stack"):
                return tables

    def block(self, bid: str) -> dict:
        recs = self.records("block", [bid])
        if bid not in recs:
            raise click.ClickException(f"block {bid} not found or not accessible")
        return recs[bid]

    # ---- writes -----------------------------------------------------------

    def transact(self, ops: list[dict]) -> None:
        # the classic submitTransaction endpoint is gone; the current client
        # writes through saveTransactionsFanout (same transaction shape)
        self.post(
            "saveTransactionsFanout",
            {
                "requestId": str(uuidlib.uuid4()),
                "transactions": [
                    {"id": str(uuidlib.uuid4()), "spaceId": self.space_id, "debug": {"userAction": "notion-cli"}, "operations": ops}
                ],
            },
        )


def unwrap(wrap: Any) -> dict:
    """Normalize a recordMap entry — `{"value": {...}}` or the double-nested
    `{"value": {"value": {...}, "role": …}}` — down to the record dict."""
    v = wrap.get("value", {}) if isinstance(wrap, dict) else {}
    if isinstance(v, dict) and isinstance(v.get("value"), dict) and "id" in v["value"]:
        v = v["value"]
    return v if isinstance(v, dict) and "id" in v else {}


def op(table: str, rid: str, path: list, command: str, args: Any, space_id: str) -> dict:
    return {"pointer": {"table": table, "id": rid, "spaceId": space_id}, "path": path, "command": command, "args": args}


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# id / url helpers
# --------------------------------------------------------------------------

_HEX32 = re.compile(r"([0-9a-f]{32})")


def parse_id(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("collection://"):
        ref = ref[len("collection://"):]
    compact = ref.replace("-", "").lower()
    if re.fullmatch(r"[0-9a-f]{32}", compact):
        return dash(compact)
    path = ref.split("?")[0].replace("-", "").lower()
    # page slugs ending in hex-only letters ("…-Update-<id>") merge with the
    # id into one long hex run — the id is always the LAST 32 chars of the
    # last long-enough run, so slice from the end rather than regex-window
    runs = [r for r in re.findall(r"[0-9a-f]+", path) if len(r) >= 32]
    if not runs:
        raise click.ClickException(f"could not extract a Notion id from {ref!r}")
    return dash(runs[-1][-32:])


def dash(h: str) -> str:
    h = h.replace("-", "")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def page_url(pid: str) -> str:
    return f"https://www.notion.so/{pid.replace('-', '')}"


# --------------------------------------------------------------------------
# segments (v3 rich text) -> compact inline markdown
# --------------------------------------------------------------------------


def seg_to_md(segments: list | None, names: dict[str, str] | None = None) -> str:
    """Render v3 segments. `names` optionally maps user/page ids to labels."""
    if not segments:
        return ""
    names = names or {}
    out = []
    for seg in segments:
        text = seg[0] if seg else ""
        marks = seg[1] if len(seg) > 1 else []
        if text == "‣":  # inline mention pill
            rendered = ""
            for m in marks:
                kind = m[0]
                if kind == "u":
                    rendered = f"@{names.get(m[1], m[1])}"
                elif kind == "p":
                    rendered = f"[{names.get(m[1], 'page')}]({page_url(m[1])})"
                elif kind == "d":
                    d = m[1]
                    rendered = d.get("start_date", "") + (f"..{d['end_date']}" if d.get("end_date") else "")
                elif len(m) > 1:  # unknown pill kinds (link mentions, …): salvage a url/text
                    v = m[1]
                    if isinstance(v, str):
                        rendered = f"<{v}>" if v.startswith("http") else v
                    elif isinstance(v, dict):
                        href = v.get("href") or v.get("url")
                        rendered = f"<{href}>" if href else rendered
            out.append(rendered)
            continue
        if text == "⁍":  # inline equation
            expr = next((m[1] for m in marks if m[0] == "e"), "")
            out.append(f"${expr}$")
            continue
        href = None
        for m in marks:
            k = m[0]
            if k == "b":
                text = f"**{text}**"
            elif k == "i":
                text = f"*{text}*"
            elif k == "s":
                text = f"~~{text}~~"
            elif k == "c":
                text = f"`{text}`"
            elif k == "a":
                href = m[1]
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def seg_plain(segments: list | None) -> str:
    if not segments:
        return ""
    return "".join(s[0] for s in segments if s and s[0] not in ("‣", "⁍"))


# --------------------------------------------------------------------------
# inline markdown -> segments
# --------------------------------------------------------------------------

_INLINE = re.compile(
    r"(@user\((?P<user>[0-9a-f-]{32,36})\))"
    r"|(@page\((?P<page>[^)]+)\))"
    r"|(\*\*(?P<bold>.+?)\*\*)"
    r"|(`(?P<code>[^`]+)`)"
    r"|(\[(?P<label>[^\]]+)\]\((?P<url>[^)]+)\))"
)


def md_to_segments(text: str) -> list:
    segs: list = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            segs.append([text[pos:m.start()]])
        if m.group("user"):
            segs.append(["‣", [["u", dash(m.group("user"))]]])
        elif m.group("page"):
            segs.append(["‣", [["p", parse_id(m.group("page"))]]])
        elif m.group("bold") is not None:
            segs.append([m.group("bold"), [["b"]]])
        elif m.group("code") is not None:
            segs.append([m.group("code"), [["c"]]])
        else:
            segs.append([m.group("label"), [["a", m.group("url")]]])
        pos = m.end()
    if pos < len(text):
        segs.append([text[pos:]])
    return segs


# --------------------------------------------------------------------------
# blocks -> compact markdown
# --------------------------------------------------------------------------

_HEADINGS = {"header": "#", "sub_header": "##", "sub_sub_header": "###"}
_COLOR_TO_MD = re.compile(r"_background$")


def render_page_body(api: Api, page_id: str, max_depth: int) -> str:
    tables = api.load_page(page_id)
    blocks = tables.get("block", {})
    names = _name_map(tables)
    cache_names("users", {uid: u.get("name") or "" for uid, u in tables.get("notion_user", {}).items()})
    # Only cache blocks that are actual pages — _name_map itself covers every
    # block (mentions can target any block id), but a paragraph/heading/bullet
    # block's title text is not a page title and would pollute the cache.
    cache_names("pages", {
        bid: seg_plain(b.get("properties", {}).get("title"))
        for bid, b in blocks.items()
        if b.get("type") in ("page", "collection_view_page")
    })
    # user mentions reference users the page chunk doesn't carry — resolve them
    uids = {m[1] for b in blocks.values() for segs in (b.get("properties") or {}).values()
            if isinstance(segs, list) for seg in segs
            if seg and seg[0] == "‣" for m in seg[1] if m[0] == "u"} - set(names)
    if uids:
        resolved = {uid: (u.get("name") or uid) for uid, u in api.records("notion_user", list(uids)).items()}
        names.update(resolved)
        cache_names("users", resolved)
    root = blocks.get(page_id, {})
    lines = _render_children(api, root.get("content", []), blocks, names, 0, max_depth)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _name_map(tables: dict) -> dict[str, str]:
    """id → display label, for rendering mentions from a page's recordMap."""
    names: dict[str, str] = {}
    for uid, u in tables.get("notion_user", {}).items():
        names[uid] = u.get("name") or (u.get("given_name", "") + " " + u.get("family_name", "")).strip() or uid
    for bid, b in tables.get("block", {}).items():
        t = seg_plain(b.get("properties", {}).get("title"))
        if t:
            names[bid] = t
    return names


def _render_children(api: Api, ids: list[str], blocks: dict, names: dict, depth: int, max_depth: int) -> list[str]:
    lines: list[str] = []
    missing = [i for i in ids if i not in blocks]
    if missing:
        blocks.update(api.records("block", missing))
    for cid in ids:
        b = blocks.get(cid)
        if not b or not b.get("alive", True):
            continue
        lines += _render_block(api, b, blocks, names, depth, max_depth)
    return lines


def _render_block(api: Api, b: dict, blocks: dict, names: dict, depth: int, max_depth: int) -> list[str]:
    t = b.get("type")
    ind = "  " * depth
    props = b.get("properties", {})
    fmt = b.get("format", {})
    title = seg_to_md(props.get("title"), names)
    lines: list[str] = []
    recurse = True

    if t == "text":
        lines.append(f"{ind}{title}" if title else "")
    elif t in _HEADINGS:
        lines.append(f"{ind}{_HEADINGS[t]} {title}")
    elif t == "bulleted_list":
        lines.append(f"{ind}- {title}")
    elif t == "numbered_list":
        lines.append(f"{ind}1. {title}")
    elif t == "to_do":
        box = "x" if seg_plain(props.get("checked")) == "Yes" else " "
        lines.append(f"{ind}- [{box}] {title}")
    elif t == "toggle":
        lines.append(f"{ind}▸ {title}")
    elif t == "callout":
        icon = fmt.get("page_icon", "")
        lines.append(f"{ind}> [!{icon}] {title}")
    elif t == "quote":
        lines.append(f"{ind}> {title}")
    elif t == "code":
        lang = seg_plain(props.get("language")) or ""
        code = seg_plain(props.get("title"))
        lines += [f"{ind}```{lang.lower()}", *(f"{ind}{ln}" for ln in code.splitlines()), f"{ind}```"]
    elif t == "divider":
        lines.append(f"{ind}---")
    elif t in ("image", "file", "pdf", "video", "audio"):
        # signed S3 sources are expiring noise — render the name/caption only,
        # keep genuinely external urls
        src = fmt.get("display_source") or seg_plain(props.get("source"))
        caption = seg_to_md(props.get("caption"), names)
        if src and src.startswith("http") and "secure.notion-static" not in src and "amazonaws" not in src and "/attachment:" not in src:
            lines.append(f"{ind}![{t}: {caption or src}]({src})")
        else:
            lines.append(f"{ind}![{t}: {caption or seg_plain(props.get('title')) or 'attached'}]")
    elif t == "bookmark":
        lines.append(f"{ind}<{seg_plain(props.get('link'))}>")
    elif t == "equation":
        lines.append(f"{ind}$$ {seg_plain(props.get('title'))} $$")
    elif t == "page":
        lines.append(f"{ind}§ [{seg_plain(props.get('title')) or 'page'}]({page_url(b['id'])})")
        recurse = False
    elif t in ("collection_view_page", "collection_view"):
        cname = names.get(b.get("collection_id", ""), "")
        lines.append(f"{ind}§db [{cname or 'database'}]({page_url(b['id'])})")
        recurse = False
    elif t == "table":
        lines += _render_table(api, b, blocks, names, ind)
        recurse = False
    elif t == "alias":
        target = (fmt.get("alias_pointer") or {}).get("id", "")
        lines.append(f"{ind}[alias]({page_url(target)})")
        recurse = False
    elif t in ("column_list", "column", "transclusion_container", "transclusion_reference", "table_of_contents", "breadcrumb"):
        pass
    else:
        if title:
            lines.append(f"{ind}{title}")

    if recurse and b.get("content"):
        if depth + 1 > max_depth:
            lines.append(f"{ind}  […children truncated at depth {max_depth}]")
        else:
            child_depth = depth if t in ("column_list", "column", "transclusion_container", "transclusion_reference") else depth + 1
            lines += _render_children(api, b["content"], blocks, names, child_depth, max_depth)
    return lines


def _render_table(api: Api, b: dict, blocks: dict, names: dict, ind: str) -> list[str]:
    order = b.get("format", {}).get("table_block_column_order", [])
    header = b.get("format", {}).get("table_block_column_header", False)
    ids = b.get("content", [])
    missing = [i for i in ids if i not in blocks]
    if missing:
        blocks.update(api.records("block", missing))
    out = []
    for i, rid in enumerate(ids):
        row = blocks.get(rid, {})
        cells = [seg_to_md(row.get("properties", {}).get(col), names) for col in order]
        out.append(f"{ind}| " + " | ".join(cells) + " |")
        if i == 0 and header:
            out.append(f"{ind}|" + "---|" * len(cells))
    return out


# --------------------------------------------------------------------------
# markdown -> v3 blocks
# --------------------------------------------------------------------------

_MD_COLOR = {  # public-API-style color names -> v3 block colors
    "blue_bg": "blue_background", "green_bg": "green_background", "yellow_bg": "yellow_background",
    "orange_bg": "orange_background", "red_bg": "red_background", "purple_bg": "purple_background",
    "pink_bg": "pink_background", "gray_bg": "gray_background", "brown_bg": "brown_background",
}
_CALLOUT = re.compile(r"^> \[!(?P<icon>[^\]:]*)(?::(?P<color>[a-z_]+))?\] ?(?P<text>.*)$")


def md_to_v3_blocks(md: str) -> list[dict]:
    """Parse a pragmatic markdown subset into v3 block specs:
    {"type", "properties", "format", "children": [...]}."""
    lines = md.splitlines()
    blocks: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, blocks)]
    i = 0

    def target(indent: int) -> list[dict]:
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        return stack[-1][1]

    while i < len(lines):
        raw = lines[i]
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)
        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip() or "Plain Text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            target(indent).append({"type": "code", "properties": {"title": [["\n".join(code_lines)]], "language": [[lang]]}})
            continue

        blk: dict | None = None
        m = _CALLOUT.match(stripped)
        if m:
            blk = {"type": "callout", "properties": {"title": md_to_segments(m.group("text"))}, "format": {}}
            if m.group("icon"):
                blk["format"]["page_icon"] = m.group("icon")
            if m.group("color"):
                blk["format"]["block_color"] = _MD_COLOR.get(m.group("color"), m.group("color"))
        elif stripped.startswith("### "):
            blk = {"type": "sub_sub_header", "properties": {"title": md_to_segments(stripped[4:])}}
        elif stripped.startswith("## "):
            blk = {"type": "sub_header", "properties": {"title": md_to_segments(stripped[3:])}}
        elif stripped.startswith("# "):
            blk = {"type": "header", "properties": {"title": md_to_segments(stripped[2:])}}
        elif stripped.startswith(("- [ ] ", "- [x] ")):
            blk = {"type": "to_do", "properties": {"title": md_to_segments(stripped[6:]), "checked": [["Yes" if stripped[3] == "x" else "No"]]}}
        elif stripped.startswith(("- ", "* ")):
            blk = {"type": "bulleted_list", "properties": {"title": md_to_segments(stripped[2:])}}
        elif re.match(r"^\d+\. ", stripped):
            blk = {"type": "numbered_list", "properties": {"title": md_to_segments(stripped.split(". ", 1)[1])}}
        elif stripped.startswith("> "):
            blk = {"type": "quote", "properties": {"title": md_to_segments(stripped[2:])}}
        elif re.fullmatch(r"-{3,}", stripped):
            blk = {"type": "divider", "properties": {}}
        else:
            blk = {"type": "text", "properties": {"title": md_to_segments(stripped)}}

        target(indent).append(blk)
        if blk["type"] in ("bulleted_list", "numbered_list", "to_do", "toggle", "callout", "quote"):
            blk.setdefault("children", [])
            stack.append((indent, blk["children"]))
        i += 1

    def prune(bs: list[dict]) -> None:
        for b in bs:
            if "children" in b:
                if b["children"]:
                    prune(b["children"])
                else:
                    del b["children"]

    prune(blocks)
    return blocks


def blocks_to_ops(api: Api, specs: list[dict], parent_id: str, parent_table: str = "block") -> tuple[list[dict], list[str]]:
    """Create-ops for block specs under a parent; returns (ops, top_level_ids)."""
    ops: list[dict] = []
    top: list[str] = []

    def emit(spec: dict, pid: str, ptable: str) -> str:
        bid = str(uuidlib.uuid4())
        record: dict[str, Any] = {
            "id": bid,
            "type": spec["type"],
            "properties": spec.get("properties", {}),
            "parent_id": pid,
            "parent_table": ptable,
            "alive": True,
            "space_id": api.space_id,
            "version": 1,
            "created_time": now_ms(),
            "last_edited_time": now_ms(),
        }
        if spec.get("format"):
            record["format"] = spec["format"]
        children = spec.get("children", [])
        if children:
            record["content"] = []
        ops.append(op("block", bid, [], "set", record, api.space_id))
        for ch in children:
            cid = emit(ch, bid, "block")
            ops.append(op("block", bid, ["content"], "listAfter", {"id": cid}, api.space_id))
        return bid

    for spec in specs:
        bid = emit(spec, parent_id, parent_table)
        ops.append(op(parent_table if parent_table == "block" else "block", parent_id, ["content"], "listAfter", {"id": bid}, api.space_id))
        top.append(bid)
    return ops, top


# --------------------------------------------------------------------------
# collections: schema, flatten, coercion
# --------------------------------------------------------------------------


def resolve_collection(api: Api, ref: str) -> tuple[dict, str | None]:
    """Resolve a ref (collection id / collection-view block id / db url) to
    (collection_record, a_view_id)."""
    rid = parse_id(ref)
    colls = api.records("collection", [rid])
    if rid in colls:
        coll = colls[rid]
        parent = api.records("block", [coll.get("parent_id", "")]).get(coll.get("parent_id", ""), {})
        views = parent.get("view_ids") or []
        return coll, (views[0] if views else None)
    blk = api.block(rid)
    cid = blk.get("collection_id") or (blk.get("format", {}).get("collection_pointer") or {}).get("id")
    if not cid:
        raise click.ClickException(f"{ref} is neither a collection nor a collection-view block")
    coll = api.records("collection", [cid])[cid]
    views = blk.get("view_ids") or []
    return coll, (views[0] if views else None)


def block_to_spec(bid: str, blocks: dict, fetch=None) -> dict | None:
    """Deep-copy an existing block subtree into a create-spec
    (type/properties/format/children) that `blocks_to_ops` can rebuild with
    fresh ids. Strips `copied_from_pointer` provenance so the clone is a plain
    authored tree, not a linked copy. `fetch(id)->record` resolves children the
    caller's `blocks` map doesn't already carry."""
    b = blocks.get(bid)
    if b is None and fetch is not None:
        b = fetch(bid)
        if b:
            blocks[bid] = b
    if not b or not b.get("alive", True):
        return None
    spec: dict = {"type": b["type"], "properties": b.get("properties", {})}
    fmt = {k: v for k, v in (b.get("format") or {}).items() if k != "copied_from_pointer"}
    if fmt:
        spec["format"] = fmt
    kids = [block_to_spec(cid, blocks, fetch) for cid in b.get("content", [])]
    kids = [k for k in kids if k]
    if kids:
        spec["children"] = kids
    return spec


def list_templates(api: Api, coll: dict) -> list[tuple[str, str]]:
    """(template_page_id, title) for every template on the collection."""
    ids = coll.get("template_pages") or []
    recs = api.records("block", ids)
    return [(tid, seg_plain(recs.get(tid, {}).get("properties", {}).get("title")) or tid) for tid in ids]


def resolve_template(api: Api, coll: dict, ref: str) -> tuple[str, str]:
    """Resolve a template ref (page id or case-insensitive title substring) to
    (id, title). Raises with the available list when nothing matches."""
    tpls = list_templates(api, coll)
    if not tpls:
        raise click.ClickException("this database has no templates")
    try:
        want = parse_id(ref)
    except click.ClickException:
        want = None
    for tid, title in tpls:
        if want and tid == want:
            return tid, title
    hits = [(tid, title) for tid, title in tpls if ref.lower() in title.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise click.ClickException(f"ambiguous template {ref!r}: " + ", ".join(t for _, t in hits))
    raise click.ClickException(f"no template matching {ref!r}; available: " + ", ".join(t for _, t in tpls))


def clone_template_ops(api: Api, coll: dict, ref: str, new_id: str) -> tuple[list[dict], str]:
    """Ops that recreate a template's body under new_id, in one transaction."""
    tid, title = resolve_template(api, coll, ref)
    tables = api.load_page(tid)
    tblocks = tables.get("block", {})
    root = tblocks.get(tid) or api.records("block", [tid]).get(tid, {})

    def fetch(x):
        return api.records("block", [x]).get(x)

    specs = [block_to_spec(cid, tblocks, fetch) for cid in root.get("content", [])]
    specs = [s for s in specs if s]
    ops, _ = blocks_to_ops(api, specs, new_id)
    return ops, title


def schema_by_name(coll: dict) -> dict[str, tuple[str, str]]:
    """name -> (prop_id, type); includes the implicit title property."""
    out = {"Title": ("title", "title")}
    for pid, p in coll.get("schema", {}).items():
        out[p.get("name", pid)] = (pid, p.get("type", "text"))
    return out


def flatten_value(segments: list | None, ptype: str, names: dict[str, str] | None = None) -> Any:
    if not segments:
        return None
    names = names or {}
    if ptype == "checkbox":
        return seg_plain(segments) == "Yes"
    if ptype in ("number", "auto_increment_id"):
        try:
            txt = seg_plain(segments)
            return float(txt) if "." in txt else int(txt)
        except ValueError:
            return seg_plain(segments)
    if ptype == "date":
        for seg in segments:
            if seg[0] == "‣":
                for m in seg[1]:
                    if m[0] == "d":
                        d = m[1]
                        start = d.get("start_date", "") + ((" " + d["start_time"]) if d.get("start_time") else "")
                        return start + (f"..{d['end_date']}" if d.get("end_date") else "")
        return seg_plain(segments)
    if ptype == "person":
        users = [m[1] for seg in segments if seg[0] == "‣" for m in seg[1] if m[0] == "u"]
        return ", ".join(names.get(u, u) for u in users)
    if ptype == "relation":
        pages = [m[1] for seg in segments if seg[0] == "‣" for m in seg[1] if m[0] == "p"]
        return ", ".join(page_url(p) for p in pages)
    if ptype == "file":
        return ", ".join(s[0] for s in segments if s[0] not in (",",))
    return seg_to_md(segments, names)


def flatten_row(row: dict, sch: dict[str, tuple[str, str]], names: dict[str, str] | None = None) -> dict:
    props = row.get("properties", {})
    out: dict[str, Any] = {"id": row["id"], "url": page_url(row["id"])}
    for name, (pid, ptype) in sch.items():
        out[name] = flatten_value(props.get(pid), ptype, names)
    return out


def coerce_segments(value: str, ptype: str) -> list:
    """--prop string -> v3 property segments, by schema type."""
    if value == "":
        return []
    if ptype == "checkbox":
        return [["Yes" if value.lower() in ("1", "true", "yes", "x", "__yes__") else "No"]]
    if ptype == "date":
        start, _, end = value.partition("..")
        d: dict[str, Any] = {"type": "date", "start_date": start}
        if "T" in start or " " in start:
            sd, _, st = start.replace("T", " ").partition(" ")
            d = {"type": "datetime", "start_date": sd, "start_time": st[:5], "time_zone": "Europe/Paris"}
        if end:
            d["end_date"] = end
            d["type"] = "daterange" if d["type"] == "date" else d["type"]
        return [["‣", [["d", d]]]]
    if ptype == "person":
        segs = []
        for v in value.split(","):
            v = v.strip().replace("user://", "")
            if v:
                segs.append(["‣", [["u", dash(v)]]])
        return segs
    if ptype == "relation":
        segs = []
        for v in value.split(","):
            if v.strip():
                segs.append(["‣", [["p", parse_id(v.strip())]]])
        return segs
    if ptype in ("title", "text"):
        return md_to_segments(value)
    # number/select/status/multi_select/url/email/phone stored as plain text
    return [[value]]


# --------------------------------------------------------------------------
# client-side filter DSL (applied to flattened values)
# --------------------------------------------------------------------------

_FILTER_RE = re.compile(r"^(?P<prop>.+?)\s*(?P<op>!=|>=|<=|=|>|<|~)\s*(?P<val>.*)$")


def make_matcher(expr: str, known: set[str]):
    expr = expr.strip()
    if expr.endswith(" is_empty") or expr.endswith(" is_not_empty"):
        prop, _, tail = expr.rpartition(" ")
        prop = prop.strip()
        if prop not in known:
            raise click.ClickException(f"unknown property {prop!r}; known: {', '.join(sorted(known))}")
        want_empty = tail == "is_empty"
        return lambda row: (row.get(prop) in (None, "", False)) == want_empty
    m = _FILTER_RE.match(expr)
    if not m:
        raise click.ClickException(f"cannot parse filter {expr!r}")
    prop, oper, val = m.group("prop").strip(), m.group("op"), m.group("val").strip()
    if prop not in known:
        raise click.ClickException(f"unknown property {prop!r}; known: {', '.join(sorted(known))}")

    def as_num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def check(row) -> bool:
        actual = row.get(prop)
        if oper == "~":
            return val.lower() in str(actual or "").lower()
        a_num, v_num = as_num(actual), as_num(val)
        if a_num is not None and v_num is not None:
            a, v = a_num, v_num
        else:
            a, v = str(actual if actual is not None else ""), val
            if isinstance(actual, bool):
                a = "true" if actual else "false"
                v = val.lower()
        return {"=": a == v, "!=": a != v, ">": a > v, ">=": a >= v, "<": a < v, "<=": a <= v}[oper]

    return check


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


@click.group()
@click.option("--debug", is_flag=True)
def cli(debug):
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING, stream=sys.stderr, format="%(levelname)s %(message)s")


def api_or_die() -> Api:
    return Api(load_config())


def _bind_and_save(token: str, space: str | None) -> None:
    """Validate a token_v2, pick the (user, space) pair, persist the config."""
    token = token.strip()
    r = requests.post(f"{API_BASE}/getSpaces", json={}, headers={"Cookie": f"token_v2={token}"}, timeout=30)
    if not r.ok:
        raise click.ClickException(f"token rejected ({r.status_code})")
    candidates: list[tuple[str, str, str]] = []  # (user_id, space_id, space_name)
    for uid, tables in r.json().items():
        for sid, wrap in tables.get("space", {}).items():
            v = unwrap(wrap)
            candidates.append((uid, sid, v.get("name") or sid))
    if not candidates:
        raise click.ClickException("token valid but no spaces visible")
    if space:
        candidates = [c for c in candidates if space.lower() in c[2].lower()]
        if not candidates:
            raise click.ClickException(f"no space matching {space!r}")
    if len(candidates) > 1:
        for i, (_, sid, name) in enumerate(candidates):
            click.echo(f"  [{i}] {name} ({sid})")
        idx = click.prompt("pick a space", type=int)
        candidates = [candidates[idx]]
    uid, sid, name = candidates[0]
    save_config({"token_v2": token, "user_id": uid, "space_id": sid, "space_name": name})
    click.echo(f"ok: bound to space {name!r} ({sid}) as user {uid}")


@cli.command()
@click.option("--token", "token", default=None, help="token_v2 cookie value (v03:…)")
@click.option("--import", "import_", is_flag=True, help=f"reuse the token stored in {LEGACY_TOKEN_PATH}")
@click.option("--space", default=None, help="workspace name to bind (when the account has several)")
def auth(token, import_, space):
    """Store a token_v2 you provide (pasted or imported) and bind it.

    Get the cookie from a logged-in browser: devtools → Application → Cookies
    → https://www.notion.so → token_v2. For automatic extraction see `login`.
    """
    if import_:
        if not LEGACY_TOKEN_PATH.exists():
            raise click.ClickException(f"nothing to import at {LEGACY_TOKEN_PATH}")
        token = json.loads(LEGACY_TOKEN_PATH.read_text())["token_v2"]
    if not token:
        token = click.prompt("token_v2", hide_input=True)
    _bind_and_save(token, space)


# Chromium cookie stores this CLI knows how to open, in preference order:
# (label, cookie sqlite path, keychain service holding the encryption key)
_COOKIE_SOURCES = [
    ("notion", "~/Library/Application Support/Notion/Cookies", "Notion Safe Storage"),
    ("notion", "~/Library/Application Support/Notion/Partitions/notion/Cookies", "Notion Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Default/Cookies", "Chrome Safe Storage"),
    ("chrome", "~/Library/Application Support/Google/Chrome/Profile 1/Cookies", "Chrome Safe Storage"),
    ("arc", "~/Library/Application Support/Arc/User Data/Default/Cookies", "Arc Safe Storage"),
    ("brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies", "Brave Safe Storage"),
]


def _decrypt_chromium_cookie(encrypted: bytes, keychain_service: str) -> str | None:
    """Decrypt a macOS-Chromium `v10` cookie value using the app's keychain key
    (AES-128-CBC, PBKDF2 'saltysalt'/1003 — the scheme every Chromium fork and
    Electron app uses on macOS). Newer Chromium prepends a 32-byte SHA-256 of
    the host key to the plaintext; both layouts are handled."""
    import subprocess

    from Crypto.Cipher import AES
    from Crypto.Protocol.KDF import PBKDF2

    if not encrypted.startswith(b"v10"):
        return None
    # `security` pops a GUI prompt when the calling terminal lacks keychain
    # access — the timeout turns a stuck prompt into a clean error
    pw = subprocess.run(
        ["security", "find-generic-password", "-ws", keychain_service],
        capture_output=True, text=True, timeout=45,
    )
    if pw.returncode != 0:
        return None
    key = PBKDF2(pw.stdout.strip().encode(), b"saltysalt", dkLen=16, count=1003)
    plain = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(encrypted[3:])
    plain = plain[: -plain[-1]]  # strip PKCS#7 padding
    for candidate in (plain, plain[32:]):
        try:
            txt = candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if txt.startswith("v0"):
            return txt
    return None


def iter_tokens(source: str) -> Iterator[tuple[str, str]]:
    """Yield (token_v2, source_label) candidates from local cookie stores."""
    import shutil
    import sqlite3
    import subprocess
    import tempfile

    seen: set[str] = set()
    for label, path, service in _COOKIE_SOURCES:
        if source not in ("auto", label):
            continue
        db = Path(path).expanduser()
        if not db.exists():
            continue
        # the store is locked while the app runs — always work on a copy
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "Cookies"
            shutil.copy2(db, tmp)
            for wal in ("-wal", "-shm"):
                side = Path(str(db) + wal)
                if side.exists():
                    shutil.copy2(side, Path(str(tmp) + wal))
            conn = sqlite3.connect(tmp)
            # encrypted_value is binary but some stores give it TEXT affinity —
            # force bytes so sqlite doesn't try (and fail) to decode UTF-8
            conn.text_factory = bytes
            rows = conn.execute(
                "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%notion.so' AND name='token_v2'"
            ).fetchall()
        for (enc,) in rows:
            try:
                token = _decrypt_chromium_cookie(bytes(enc), service)
            except subprocess.TimeoutExpired:
                raise click.ClickException(
                    f"keychain prompt for {service!r} timed out — run this in a "
                    "real terminal and click 'Allow' on the macOS dialog"
                )
            if token and token not in seen:
                seen.add(token)
                yield token, label


@cli.command()
@click.option("--source", type=click.Choice(["auto", "notion", "chrome", "arc", "brave"]), default="auto", show_default=True)
@click.option("--space", default=None, help="workspace name to bind (when the account has several)")
def login(source, space):
    """Extract token_v2 from the Notion app / a Chromium browser and bind it.

    Decrypts the local cookie stores with the apps' macOS-keychain keys — no
    password typed, no browser automation; requires being logged in to
    notion.so in one of them. Stale sessions (a logged-out desktop app) are
    skipped: the first token that VALIDATES against the API wins. May pop one
    keychain 'Allow' dialog per app.
    """
    tried: list[str] = []
    for token, label in iter_tokens(source):
        r = requests.post(f"{API_BASE}/getSpaces", json={}, headers={"Cookie": f"token_v2={token}"}, timeout=30)
        if r.ok:
            click.echo(f"token_v2 extracted from {label} (valid)")
            _bind_and_save(token, space)
            return
        tried.append(f"{label}:{r.status_code}")
        log.info("token from %s rejected (%s), trying next store", label, r.status_code)
    raise click.ClickException(
        ("all extracted tokens were stale (" + ", ".join(tried) + ")" if tried else "no decryptable token_v2 found in Notion/Chrome/Arc/Brave")
        + " — log in to notion.so in one of those apps, or paste the cookie via `auth`."
    )


@cli.command()
def whoami():
    """Show the bound user and workspace."""
    cfg = load_config()
    api = Api(cfg)
    users = api.records("notion_user", [cfg["user_id"]])
    u = users.get(cfg["user_id"], {})
    click.echo(f"{u.get('name', cfg['user_id'])} <{u.get('email', '?')}> — space {cfg.get('space_name')} ({cfg.get('space_id')})")


@cli.command()
@click.argument("ref")
@click.option("--props-only", is_flag=True, help="skip the body (cheapest read)")
@click.option("--no-props", is_flag=True, help="body only")
@click.option("--depth", default=6, show_default=True)
@click.option("--json", "as_json", is_flag=True)
@click.option("--raw", is_flag=True, help="raw record JSON (expensive; debugging)")
def page(ref, props_only, no_props, depth, as_json, raw):
    """Render a page: flattened properties + body as compact markdown."""
    api = api_or_die()
    pid = parse_id(ref)
    blk = api.block(pid)
    if raw:
        click.echo(json.dumps(blk, indent=2, ensure_ascii=False))
        return
    flat: dict[str, Any] = {"id": pid, "url": page_url(pid)}
    if not blk.get("alive", True):
        flat["deleted"] = True  # record still resolves after deletion (trash)
    if blk.get("parent_table") == "collection":
        coll = api.records("collection", [blk["parent_id"]]).get(blk["parent_id"], {})
        sch = schema_by_name(coll)
        user_ids = [m[1] for segs in blk.get("properties", {}).values() if isinstance(segs, list)
                    for seg in segs if seg and seg[0] == "‣" for m in seg[1] if m[0] == "u"]
        names = {uid: (u.get("name") or uid) for uid, u in api.records("notion_user", list(set(user_ids))).items()}
        flat.update({k: v for k, v in flatten_row(blk, sch, names).items() if k not in ("id", "url")})
    else:
        flat["Title"] = seg_plain(blk.get("properties", {}).get("title"))
    title = seg_plain(blk.get("properties", {}).get("title"))
    if title:
        cache_names("pages", {pid: title})
    body = None if props_only else render_page_body(api, pid, depth)
    if as_json:
        if body is not None:
            flat["body"] = body
        click.echo(json.dumps(flat, ensure_ascii=False, indent=2))
        return
    if not no_props:
        for k, v in flat.items():
            click.echo(f"{k}: {v}")
    if body:
        if not no_props:
            click.echo("---")
        click.echo(body)


@cli.command()
@click.argument("ids", nargs=-1, required=True)
@click.option("--depth", default=6, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def pages(ids, depth, as_json):
    """Fetch multiple pages' bodies in one call — content only, no properties
    (like `page --no-props`, batched). For any already-known set of ids (from
    `search`, `resolve`, or elsewhere) that don't come from a single `query`
    — use `query --with-body` instead when they do, it's one query call
    total rather than a query plus N of these."""
    api = api_or_die()
    out = []
    for ref in ids:
        pid = parse_id(ref)
        title = seg_plain(api.block(pid).get("properties", {}).get("title"))
        if title:
            cache_names("pages", {pid: title})
        body = render_page_body(api, pid, depth)
        if as_json:
            out.append({"id": pid, "title": title, "body": body})
        else:
            click.echo(f"# [{pid}] {title or '(untitled)'}\n")
            click.echo(body)
            click.echo("\n---\n")
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False, indent=1))


@cli.command()
@click.argument("ref")
@click.option("--select", "select_", help="comma-separated property names")
@click.option("--filter", "filters", multiple=True, help="'Status=Done', 'ID>195', 'Title~vault', 'Due is_empty' (ANDed)")
@click.option("--sort", "sort_", help="'Prop' or 'Prop:desc'")
@click.option("--limit", type=int, default=None)
@click.option("--names/--no-names", default=True, help="resolve people to display names")
@click.option("--with-body", is_flag=True, help="also fetch and include each matched row's full page body (one call instead of one `page` call per row)")
@click.option("--body-depth", default=6, show_default=True, help="nested-children recursion cap for --with-body")
@click.option("--json", "as_json", is_flag=True)
def query(ref, select_, filters, sort_, limit, names, with_body, body_depth, as_json):
    """Query a database; one compact line per row (filters applied client-side)."""
    api = api_or_die()
    coll, view_id = resolve_collection(api, ref)
    if not view_id:
        raise click.ClickException("no view found on this collection — pass the database block url")
    d = api.post(
        "queryCollection",
        {
            "collection": {"id": coll["id"], "spaceId": api.space_id},
            "collectionView": {"id": view_id, "spaceId": api.space_id},
            "loader": {
                "reducers": {"collection_group_results": {"type": "results", "limit": 9999}},
                "sort": [],
                "searchQuery": "",
                "userTimeZone": "Europe/Paris",
            },
            "source": {"type": "collection", "id": coll["id"], "spaceId": api.space_id},
        },
    )
    ids = d.get("result", {}).get("reducerResults", {}).get("collection_group_results", {}).get("blockIds", [])
    rm = {rid: unwrap(w) for rid, w in d.get("recordMap", {}).get("block", {}).items()}
    sch = schema_by_name(coll)
    name_map: dict[str, str] = {}
    if names:
        uids = {m[1] for r in rm.values() for segs in r.get("properties", {}).values() if isinstance(segs, list)
                for seg in segs if seg and seg[0] == "‣" for m in seg[1] if m[0] == "u"}
        name_map = {uid: (u.get("name") or uid) for uid, u in api.records("notion_user", list(uids)).items()}
        cache_names("users", name_map)
    rows = [flatten_row(rm[i], sch, name_map) for i in ids if i in rm and rm[i].get("alive", True)]
    cache_names("pages", {r["id"]: seg_plain(rm[r["id"]].get("properties", {}).get("title")) for r in rows if r.get("id") in rm})
    # relation cells flatten to bare page urls; when the target is another row
    # of this same query, label it "#<ID> <Title>" so hierarchy is readable
    if not as_json:
        labels = {}
        for r in rows:
            rid_ = r.get("ID")
            labels[r["url"]] = (f"#{rid_} " if rid_ is not None else "") + str(r.get("Title") or "")
        rel_cols = [n for n, (_, t) in sch.items() if t == "relation"]
        for r in rows:
            for col in rel_cols:
                if r.get(col):
                    r[col] = ", ".join(labels.get(u.strip(), u.strip()) for u in str(r[col]).split(","))
    matchers = [make_matcher(f, set(sch) | {"id", "url"}) for f in filters]
    rows = [r for r in rows if all(m(r) for m in matchers)]
    if sort_:
        key, _, direction = sort_.partition(":")

        def sort_key(r):
            v = r.get(key)
            try:
                num = float(v)
            except (TypeError, ValueError):
                num = None
            # Nones last, numbers before strings, numeric compare when possible
            return (v is None, num is None, num if num is not None else 0.0, str(v or ""))

        rows.sort(key=sort_key, reverse=direction == "desc")
    if limit:
        rows = rows[:limit]
    cols = [c.strip() for c in select_.split(",")] if select_ else None
    bodies = {r["id"]: render_page_body(api, r["id"], body_depth) for r in rows} if with_body else {}
    out = []
    for r in rows:
        picked = {"id": r["id"], **{c: r.get(c) for c in cols}} if cols else r
        if as_json:
            if with_body:
                picked["body"] = bodies[r["id"]]
            out.append(picked)
        else:
            click.echo(" | ".join(str(v) for k, v in picked.items() if k not in ("id", "url")) + f"\t{r['url']}")
            if with_body:
                click.echo("---")
                click.echo(bodies[r["id"]])
                click.echo("---\n")
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False, indent=1))


@cli.command(name="schema")
@click.argument("ref")
def schema_cmd(ref):
    """Print a database schema (property name → type) and its ids."""
    api = api_or_die()
    coll, view_id = resolve_collection(api, ref)
    click.echo(f"collection: {coll['id']}  (view: {view_id})")
    for name, (pid, ptype) in sorted(schema_by_name(coll).items()):
        click.echo(f"  {name}: {ptype}")


def _epoch_ms(iso: str) -> int:
    """Parse a YYYY-MM-DD[ HH:MM[:SS]] (UTC) date to epoch ms."""
    import datetime

    s = iso.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise click.ClickException(f"bad date {iso!r} — use YYYY-MM-DD or YYYY-MM-DD HH:MM")


@cli.command()
@click.argument("text")
@click.option("--limit", type=int, default=10, show_default=True)
@click.option("--created-after", help="only pages created at/after this UTC date (client-side)")
@click.option("--edited-after", help="only pages last-edited at/after this UTC date (client-side)")
@click.option("--json", "as_json", is_flag=True)
def search(text, limit, created_after, edited_after, as_json):
    """Workspace search (same index as the app's quick-find).

    --created-after / --edited-after filter client-side on each hit's
    timestamp (the private search API ignores server-side time filters), so
    the fetch is widened automatically when a date bound is set.
    """
    api = api_or_die()
    cut_c = _epoch_ms(created_after) if created_after else None
    cut_e = _epoch_ms(edited_after) if edited_after else None
    fetch = max(limit, 100) if (cut_c or cut_e) else limit
    d = api.post(
        "search",
        {
            "type": "BlocksInSpace",
            "query": text,
            "spaceId": api.space_id,
            "limit": fetch,
            "filters": {
                "isDeletedOnly": False, "excludeTemplates": True, "navigableBlockContentOnly": True,
                "requireEditPermissions": False, "includePublicPagesWithoutExplicitAccess": False,
                "ancestors": [], "createdBy": [], "editedBy": [], "lastEditedTime": {}, "createdTime": {},
            },
            "sort": {"field": "relevance"},
            "source": "quick_find_input_change",
        },
    )
    import datetime

    rm = {rid: unwrap(w) for rid, w in d.get("recordMap", {}).get("block", {}).items()}
    out = []
    kept = 0
    for r in d.get("results", []):
        if kept >= limit:
            break
        rid = r.get("id")
        blk = rm.get(rid, {})
        ct, et = blk.get("created_time", 0), blk.get("last_edited_time", 0)
        if cut_c and ct < cut_c:
            continue
        if cut_e and et < cut_e:
            continue
        kept += 1
        title = seg_plain(blk.get("properties", {}).get("title")) or "?"
        if as_json:
            out.append({"id": rid, "url": page_url(rid), "title": title,
                        "created": datetime.datetime.fromtimestamp(ct / 1000, datetime.timezone.utc).isoformat() if ct else None,
                        "edited": datetime.datetime.fromtimestamp(et / 1000, datetime.timezone.utc).isoformat() if et else None})
        else:
            click.echo(f"{title}\t{page_url(rid)}")
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False, indent=1))


@cli.command()
@click.argument("page_ref")
@click.option("--resolved/--open-only", default=True, help="include resolved discussions (default: yes)")
def comments(page_ref, resolved):
    """List discussions incl. RESOLVED ones (the public API can't see those)."""
    api = api_or_die()
    pid = parse_id(page_ref)
    tables = api.load_page(pid)
    blks = tables.get("block", {})

    def on_this_page(bid: str) -> bool:
        # loadPageChunk also returns ancestor pages' discussions — keep only
        # those anchored on this page or on blocks inside it
        seen = set()
        while bid and bid not in seen:
            if bid == pid:
                return True
            seen.add(bid)
            b = blks.get(bid)
            if not b or b.get("type") == "page" and b["id"] != pid:
                return False
            bid = b.get("parent_id", "")
        return False

    discussions = {did: d for did, d in tables.get("discussion", {}).items() if on_this_page(d.get("parent_id", ""))}
    if not discussions:
        click.echo("no discussions")
        return
    comment_ids = [c for d in discussions.values() for c in d.get("comments", [])]
    comment_recs = tables.get("comment", {})
    missing = [c for c in comment_ids if c not in comment_recs]
    if missing:
        comment_recs.update(api.records("comment", missing))
    uids = {c.get("created_by_id") for c in comment_recs.values() if c.get("created_by_id")}
    # also resolve users @-mentioned inside comment bodies, not just authors
    uids |= {m[1] for c in comment_recs.values() for seg in (c.get("text") or [])
             if seg and seg[0] == "‣" for m in seg[1] if m[0] == "u"}
    names = {uid: (u.get("name") or uid) for uid, u in api.records("notion_user", list(uids)).items()}
    for did, disc in discussions.items():
        if disc.get("resolved") and not resolved:
            continue
        state = "resolved" if disc.get("resolved") else "OPEN"
        click.echo(f"discussion {did} [{state}] on block {disc.get('parent_id')}")
        for cid in disc.get("comments", []):
            c = comment_recs.get(cid, {})
            ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime((c.get("created_time") or 0) / 1000))
            click.echo(f"  [{ts}] {names.get(c.get('created_by_id'), c.get('created_by_id', '?'))}: {seg_to_md(c.get('text'), names)}")


@cli.command()
@click.argument("query_", metavar="[QUERY]", required=False)
def users(query_):
    """List workspace members (from the space permission list)."""
    api = api_or_die()
    cfg = load_config()
    spaces = api.records("space", [cfg["space_id"]])
    perms = spaces.get(cfg["space_id"], {}).get("permissions", [])
    uids = [p.get("user_id") for p in perms if p.get("user_id")]
    fetched = api.records("notion_user", uids)
    cache_names("users", {uid: u.get("name", "") for uid, u in fetched.items()})
    for uid, u in fetched.items():
        label = f"{u.get('name', '?')}\t{u.get('email', '')}\t{uid}"
        if not query_ or query_.lower() in label.lower():
            click.echo(label)


@cli.command()
@click.argument("ids", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
def resolve(ids, as_json):
    """Resolve one or more ids to a display name/title — user or page —
    using the local id_names cache first. Only ids not already cached (by
    any prior command — `page`, `query`, `users`, or a previous `resolve`)
    cost an API call, one batched `notion_user` lookup plus one batched
    `block` lookup for whatever's left, never a full listing. The right way
    to name an id instead of guessing it from context."""
    cache = load_id_cache()
    parsed = [parse_id(rid) for rid in ids]
    out: dict[str, str] = {}
    missing = []
    for pid in parsed:
        if pid in cache["users"]:
            out[pid] = cache["users"][pid]
        elif pid in cache["pages"]:
            out[pid] = cache["pages"][pid]
        else:
            missing.append(pid)
    if missing:
        api = api_or_die()
        found_users = api.records("notion_user", missing)
        user_names = {uid: (u.get("name") or uid) for uid, u in found_users.items()}
        out.update(user_names)
        merge_id_cache(cache, "users", user_names)
        still_missing = [pid for pid in missing if pid not in found_users]
        if still_missing:
            found_blocks = api.records("block", still_missing)
            page_titles = {bid: (seg_plain(b.get("properties", {}).get("title")) or bid) for bid, b in found_blocks.items()}
            out.update(page_titles)
            merge_id_cache(cache, "pages", page_titles)
            for bid in still_missing:
                out.setdefault(bid, bid)  # unresolved — keep the raw id, don't guess
        save_id_cache(cache)
    if as_json:
        click.echo(json.dumps(out, ensure_ascii=False))
        return
    for pid in parsed:
        click.echo(f"{pid}\t{out.get(pid, pid)}")


@cli.command()
@click.argument("ref")
@click.option("--depth", default=1, show_default=True)
def blocks(ref, depth):
    """List child blocks with ids (targeting aid for edit/delete-block)."""
    api = api_or_die()
    pid = parse_id(ref)
    tables = api.load_page(pid)
    blks = tables.get("block", {})
    names = _name_map(tables)

    def walk(bid: str, d: int):
        b = blks.get(bid) or api.records("block", [bid]).get(bid, {})
        for cid in b.get("content", []):
            c = blks.get(cid) or api.records("block", [cid]).get(cid)
            if not c or not c.get("alive", True):
                continue
            txt = seg_to_md(c.get("properties", {}).get("title"), names)[:90]
            click.echo(f"{'  ' * (depth - d)}{cid}  {c['type']}  {txt}")
            if c.get("content") and d > 1:
                walk(cid, d - 1)

    walk(pid, depth)


# ---- writes ---------------------------------------------------------------


def _read_md(md_file: str | None, text: str | None) -> str:
    if md_file:
        return sys.stdin.read() if md_file == "-" else Path(md_file).read_text()
    if text is not None:
        return text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise click.ClickException("no content: pass TEXT, --md FILE, or pipe stdin")


@cli.command()
@click.argument("page_ref")
@click.argument("text", required=False)
@click.option("--md", "md_file", help="markdown file to append ('-' = stdin)")
def append(page_ref, text, md_file):
    """Append markdown content to a page."""
    api = api_or_die()
    pid = parse_id(page_ref)
    specs = md_to_v3_blocks(_read_md(md_file, text))
    ops, top = blocks_to_ops(api, specs, pid)
    ops.append(op("block", pid, [], "update", {"last_edited_time": now_ms()}, api.space_id))
    api.transact(ops)
    click.echo(f"appended {len(top)} block(s) to {page_url(pid)}")


def _parse_prop_args(props: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for p in props:
        if "=" not in p:
            raise click.ClickException(f"--prop needs Name=Value, got {p!r}")
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


@cli.command()
@click.option("--parent", "parent_ref", required=True, help="database (collection) or page to create under")
@click.option("--prop", "props", multiple=True, help="Name=Value (schema-aware; repeatable; empty clears)")
@click.option("--template", "template", help="clone this database template's body (id or title substring)")
@click.option("--md", "md_file", help="markdown body file ('-' = stdin; appended after --template)")
@click.option("--body", help="markdown body inline (appended after --template)")
@click.option("--icon", help="emoji icon")
def create(parent_ref, props, template, md_file, body, icon):
    """Create a row in a database (schema-coerced props) or a child page.

    --template clones a database template's body synchronously into the same
    create transaction (no async placeholder race, unlike the API's template
    instantiation); --md/--body then appends beneath the cloned body.
    """
    api = api_or_die()
    kv = _parse_prop_args(props)
    new_id = str(uuidlib.uuid4())
    record: dict[str, Any] = {
        "id": new_id, "type": "page", "alive": True, "space_id": api.space_id,
        "version": 1, "created_time": now_ms(), "last_edited_time": now_ms(), "properties": {},
    }
    ops: list[dict] = []
    coll = None
    try:
        coll, _ = resolve_collection(api, parent_ref)
        sch = schema_by_name(coll)
        record["parent_id"], record["parent_table"] = coll["id"], "collection"
        for name, val in kv.items():
            if name not in sch:
                raise click.ClickException(f"unknown property {name!r}; known: {', '.join(sorted(sch))}")
            pid_, ptype = sch[name]
            record["properties"][pid_] = coerce_segments(val, ptype)
    except click.ClickException as e:
        if "unknown property" in str(e):
            raise
        if template:
            raise click.ClickException("--template only applies when creating in a database")
        log.info("parent is not a database (%s); creating a child page", e)
        ppid = parse_id(parent_ref)
        record["parent_id"], record["parent_table"] = ppid, "block"
        record["properties"]["title"] = md_to_segments(kv.get("Title") or kv.get("title") or "Untitled")
        ops.append(op("block", ppid, ["content"], "listAfter", {"id": new_id}, api.space_id))
    if icon:
        record["format"] = {"page_icon": icon}
    ops.insert(0, op("block", new_id, [], "set", record, api.space_id))
    tpl_title = None
    if template:
        tpl_ops, tpl_title = clone_template_ops(api, coll, template, new_id)
        ops += tpl_ops
    md = _read_md(md_file, body) if (md_file or body) else None
    if md:
        child_ops, _ = blocks_to_ops(api, md_to_v3_blocks(md), new_id)
        ops += child_ops
    api.transact(ops)
    click.echo(f"created {page_url(new_id)}" + (f" (from template {tpl_title!r})" if tpl_title else ""))


@cli.command(name="templates")
@click.argument("ref")
def templates_cmd(ref):
    """List a database's templates (id + title) for use with `create --template`."""
    api = api_or_die()
    coll, _ = resolve_collection(api, ref)
    tpls = list_templates(api, coll)
    if not tpls:
        click.echo("no templates on this database")
        return
    for tid, title in tpls:
        click.echo(f"{title}\t{page_url(tid)}")


@cli.command()
@click.argument("page_ref")
@click.option("--prop", "props", multiple=True, help="Name=Value (schema-aware; empty clears)")
@click.option("--archive/--restore", default=None)
@click.option("--icon", help="emoji icon")
def update(page_ref, props, archive, icon):
    """Update row properties / archive state."""
    api = api_or_die()
    pid = parse_id(page_ref)
    blk = api.block(pid)
    ops: list[dict] = []
    if props:
        if blk.get("parent_table") == "collection":
            coll = api.records("collection", [blk["parent_id"]])[blk["parent_id"]]
            sch = schema_by_name(coll)
        else:
            sch = {"Title": ("title", "title")}
        for name, val in _parse_prop_args(props).items():
            if name not in sch:
                raise click.ClickException(f"unknown property {name!r}; known: {', '.join(sorted(sch))}")
            prop_id, ptype = sch[name]
            ops.append(op("block", pid, ["properties", prop_id], "set", coerce_segments(val, ptype), api.space_id))
    if archive is not None:
        ops.append(op("block", pid, [], "update", {"alive": not archive}, api.space_id))
    if icon:
        ops.append(op("block", pid, ["format", "page_icon"], "set", icon, api.space_id))
    if not ops:
        raise click.ClickException("nothing to update")
    ops.append(op("block", pid, [], "update", {"last_edited_time": now_ms()}, api.space_id))
    api.transact(ops)
    click.echo(f"updated {page_url(pid)}")


@cli.command()
@click.argument("page_ref")
@click.argument("old")
@click.argument("new")
@click.option("--all", "replace_all", is_flag=True, help="replace every match (default: must be unique)")
def edit(page_ref, old, new, replace_all):
    """Search-and-replace text inside a page's blocks.

    Replacement happens inside individual text segments so formatting and
    mentions elsewhere in the block are preserved; a match spanning a
    formatting boundary is reported instead of mangled.
    """
    api = api_or_die()
    pid = parse_id(page_ref)
    tables = api.load_page(pid)
    blks = tables.get("block", {})
    hits: list[str] = []
    spanning: list[str] = []
    for bid, b in blks.items():
        if not b.get("alive", True):
            continue
        title = b.get("properties", {}).get("title")
        if not title:
            continue
        in_seg = any(old in seg[0] for seg in title if seg and seg[0] not in ("‣", "⁍"))
        if in_seg:
            hits.append(bid)
        elif old in seg_plain(title):
            spanning.append(bid)
    if spanning:
        raise click.ClickException(f"match spans formatting boundaries in block(s) {', '.join(spanning)} — narrow the string (see `blocks`)")
    if not hits:
        raise click.ClickException("no match")
    if len(hits) > 1 and not replace_all:
        for bid in hits:
            click.echo(f"match in {bid} ({blks[bid]['type']})", err=True)
        raise click.ClickException(f"{len(hits)} matches — pass --all or narrow the string")
    ops = []
    for bid in hits:
        title = blks[bid]["properties"]["title"]
        new_title = [[seg[0].replace(old, new), *seg[1:]] if seg and seg[0] not in ("‣", "⁍") else seg for seg in title]
        ops.append(op("block", bid, ["properties", "title"], "set", new_title, api.space_id))
        ops.append(op("block", bid, [], "update", {"last_edited_time": now_ms()}, api.space_id))
    api.transact(ops)
    click.echo(f"replaced in {len(hits)} block(s)")


@cli.command(name="delete-block")
@click.argument("block_ref")
def delete_block(block_ref):
    """Remove a block (alive=false + detach from its parent's content)."""
    api = api_or_die()
    bid = parse_id(block_ref)
    b = api.block(bid)
    ops = [op("block", bid, [], "update", {"alive": False}, api.space_id)]
    if b.get("parent_table") == "block":
        ops.append(op("block", b["parent_id"], ["content"], "listRemove", {"id": bid}, api.space_id))
    api.transact(ops)
    click.echo("deleted")


@cli.command()
@click.argument("page_ref")
@click.argument("text")
@click.option("--discussion", "discussion_id", help="reply into an existing discussion id")
def comment(page_ref, text, discussion_id):
    """Add a comment (new page-level discussion, or reply with --discussion)."""
    api = api_or_die()
    pid = parse_id(page_ref)
    cid = str(uuidlib.uuid4())
    ops: list[dict] = []
    if not discussion_id:
        discussion_id = str(uuidlib.uuid4())
        ops.append(
            op("discussion", discussion_id, [], "set",
               {"id": discussion_id, "parent_id": pid, "parent_table": "block", "resolved": False,
                "space_id": api.space_id, "version": 1, "comments": [cid]}, api.space_id)
        )
        ops.append(op("block", pid, ["discussions"], "listAfter", {"id": discussion_id}, api.space_id))
    else:
        ops.append(op("discussion", discussion_id, ["comments"], "listAfter", {"id": cid}, api.space_id))
    ops.insert(0, op("comment", cid, [], "set",
                     {"id": cid, "parent_id": discussion_id, "parent_table": "discussion", "text": md_to_segments(text),
                      "space_id": api.space_id, "alive": True, "version": 1,
                      "created_by_table": "notion_user", "created_by_id": api.user_id,
                      "last_edited_by_table": "notion_user", "last_edited_by_id": api.user_id,
                      "created_time": now_ms(), "last_edited_time": now_ms()},
                     api.space_id))
    api.transact(ops)
    click.echo(f"comment {cid} added (discussion {discussion_id})")


if __name__ == "__main__":
    cli()

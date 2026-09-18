"""Custom Agent desired-state helpers.

The web client stores Custom Agents as `workflow` records. The session
cookie (`token_v2`) can list them (`getCustomAgents`) and read the
record (`syncRecordValues`). Instruction bodies live on a normal Notion
page. The official Agents API (PAT) is optional and only needed for
credit_limit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import click
import requests
import yaml

log = logging.getLogger("notion-cli")

PUBLIC_API_BASE = "https://api.notion.com"
PUBLIC_API_VERSION = "2026-03-11"
STATE_NAME = ".state.json"

WRITABLE_FIELDS = frozenset({
    "name", "description", "icon", "model", "status",
    "credit_limit", "connections", "triggers", "instructions",
})
WORKFLOW_DATA_PATH = {
    "name": ["data", "name"],
    "description": ["data", "description"],
    "icon": ["data", "icon"],
    "model": ["data", "model"],
    "status": ["data", "status"],
    "connections": ["data", "modules"],
    "triggers": ["data", "triggers"],
}
MANAGED_FIELDS = (
    "name",
    "description",
    "icon",
    "model",
    "status",
    "credit_limit",
    "connections",
    "triggers",
)


class PublicApi:
    """Official Notion API (`api.notion.com`) authenticated with a PAT.

    `$NOTION_API_KEY` overrides a stored `api_key`. Pass `api_key=` to
    validate a token that is not yet in config/env.
    """

    def __init__(self, cfg: dict, *, api_key: str | None = None):
        key = api_key or os.environ.get("NOTION_API_KEY") or cfg.get("api_key")
        if not key:
            raise click.ClickException(
                "No official API token. Create a personal access token "
                "(Settings → Connections → Personal access tokens, Notion API "
                "capability), then `notion auth --pat`. On Business/Enterprise "
                "a workspace owner may need to enable PAT creation."
            )
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {key}",
            "Notion-Version": PUBLIC_API_VERSION,
            "Content-Type": "application/json",
        })

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        retries: int = 5,
    ) -> dict:
        url = f"{PUBLIC_API_BASE}{path}"
        delay = 8
        for _ in range(retries):
            resp = self.s.request(method, url, json=json_body, params=params, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                raw = resp.headers.get("Retry-After")
                try:
                    hinted = float(raw) if raw is not None else 0.0
                except ValueError:
                    hinted = 0.0
                wait = hinted if hinted > 0 else delay
                log.warning("%s %s -> %s, retrying in %.1fs", method, path, resp.status_code, wait)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code == 401:
                raise click.ClickException(
                    "401 — official API token unauthorized; re-run `notion auth --pat`"
                )
            if not resp.ok:
                msg = resp.text[:300]
                try:
                    err = resp.json()
                    msg = err.get("message") or msg
                except ValueError:
                    pass
                raise click.ClickException(f"{resp.status_code} on {method} {path}: {msg}")
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise click.ClickException(f"still failing after {retries} retries: {method} {path}")

    def me(self) -> dict:
        return self.request("GET", "/v1/users/me")

    def query_agents(
        self,
        *,
        query: str | None = None,
        include_deleted: bool = False,
        agent_type: str | None = "custom_agent",
        page_size: int = 100,
        verbose: bool = False,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "page_size": page_size,
            "verbose": verbose,
            "include_deleted": include_deleted,
        }
        if query:
            body["query"] = query
        if agent_type:
            body["filter"] = {"property": "agent_type", "string": {"equals": agent_type}}
        out: list[dict] = []
        while True:
            data = self.request("POST", "/v1/agents/query", json_body=body)
            out.extend(data.get("results") or [])
            if not data.get("has_more"):
                return out
            body["start_cursor"] = data.get("next_cursor")

    def get_agent(self, agent_id: str, *, verbose: bool = True) -> dict:
        params = {"verbose": "true"} if verbose else None
        return self.request("GET", f"/v1/agents/{agent_id}", params=params)

    def set_status(self, agent_id: str, status: str) -> dict:
        return self.request("PATCH", f"/v1/agents/{agent_id}/status", json_body={"status": status})

    def set_credit_limit(self, agent_id: str, credit_limit: int | None) -> dict:
        return self.request(
            "PATCH",
            f"/v1/agents/{agent_id}/credit_limit",
            json_body={"credit_limit": credit_limit},
        )


def flatten_rich_text(value: Any) -> str | None:
    """Turn v3 segments or a plain string into one line of text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if not isinstance(value, list):
        return str(value)
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, list) and item and isinstance(item[0], str):
            parts.append(item[0])
        elif isinstance(item, list):
            nested = flatten_rich_text(item)
            if nested:
                parts.append(nested)
    text = "".join(parts).strip()
    return text or None


def workflow_to_agent(rec: dict) -> dict:
    """Normalize a v3 `workflow` record into the manifest/list shape."""
    data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    instr = data.get("instructions") if isinstance(data.get("instructions"), dict) else {}
    return {
        "id": rec.get("id"),
        "agent_type": "custom_agent",
        "name": data.get("name"),
        "description": flatten_rich_text(data.get("description")),
        "icon": data.get("icon"),
        "model": data.get("model"),
        "status": data.get("status"),
        "credit_limit": None,
        "connections": data.get("modules") or [],
        "triggers": data.get("triggers") or [],
        "instructions_page_id": instr.get("id"),
        "alive": rec.get("alive"),
        "version": rec.get("version"),
    }


def list_session_agents(api: Any, *, query: str | None = None, include_deleted: bool = False) -> list[dict]:
    """List Custom Agents visible to the session cookie."""
    body: dict[str, Any] = {"spaceId": api.space_id}
    if include_deleted:
        body["includeDeleted"] = True
    payload = api.post("getCustomAgents", body)
    ids = [str(item) for item in (payload.get("agentIds") or []) if item]
    recs = api.records("workflow", ids) if ids else {}
    out: list[dict] = []
    needle = query.lower() if query else None
    for aid in ids:
        rec = recs.get(aid)
        if not rec:
            continue
        if not include_deleted and rec.get("alive") is False:
            continue
        agent = workflow_to_agent(rec)
        if needle:
            blob = f"{agent.get('name') or ''} {agent.get('description') or ''}".lower()
            if needle not in blob:
                continue
        out.append(agent)
    return out


def get_workflow_record(api: Any, agent_id: str) -> dict:
    recs = api.records("workflow", [agent_id])
    rec = recs.get(agent_id)
    if not rec:
        raise click.ClickException(f"agent {agent_id} not found or not accessible")
    return rec


def get_session_agent(api: Any, agent_id: str) -> dict:
    return workflow_to_agent(get_workflow_record(api, agent_id))


def parse_agent_id(ref: str, parse_id: Callable[[str], str]) -> str:
    """Accept a UUID, `/agent/{hex}` URL, or the personal-agent alias."""
    ref = ref.strip()
    if ref in {"notion_ai", "33333333-3333-3333-3333-333333333333"}:
        return "notion_ai"
    return parse_id(ref)


def agent_url(agent_id: str) -> str:
    if agent_id == "notion_ai":
        return "https://www.notion.so"
    return f"https://app.notion.com/agent/{agent_id.replace('-', '')}"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "agent"


def unique_slug(name: str, used: set[str]) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    return slug


def normalize_md(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def dump_yaml(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def agent_to_manifest(agent: dict, slug: str, instructions_file: str | None) -> dict:
    """Desired-state view of a retrieve-agent payload (no ephemeral fields)."""
    out: dict[str, Any] = {
        "kind": agent.get("agent_type") or "custom_agent",
        "slug": slug,
        "id": agent.get("id"),
        "name": agent.get("name"),
        "description": agent.get("description"),
        "icon": agent.get("icon"),
        "model": agent.get("model"),
        "status": agent.get("status"),
        "connections": agent.get("connections") or [],
        "triggers": agent.get("triggers") or [],
    }
    if agent.get("credit_limit") is not None:
        out["credit_limit"] = agent.get("credit_limit")
    if instructions_file:
        out["instructions"] = instructions_file
    return out


def load_agent_state(dir_path: Path) -> dict:
    path = dir_path / STATE_NAME
    if not path.is_file():
        return {"version": 1, "agents": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "agents": {}}
    if not isinstance(data, dict):
        return {"version": 1, "agents": {}}
    data.setdefault("version", 1)
    data.setdefault("agents", {})
    if not isinstance(data["agents"], dict):
        data["agents"] = {}
    return data


def save_agent_state(dir_path: Path, state: dict) -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / STATE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def iter_manifest_paths(src: Path) -> list[Path]:
    if src.is_file():
        return [src]
    if not src.is_dir():
        raise click.ClickException(f"{src} is not a file or directory")
    return sorted(src.glob("*.yaml")) + sorted(
        p for p in src.glob("*.yml") if p.suffix != ".yaml"
    )


def load_manifest(path: Path) -> tuple[dict, str | None]:
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise click.ClickException(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(spec, dict):
        raise click.ClickException(f"{path}: expected a mapping")
    spec.setdefault("slug", slugify(spec.get("name") or path.stem))
    md = None
    instr = spec.get("instructions")
    if isinstance(instr, str):
        candidate = path.parent / instr
        md = candidate.read_text(encoding="utf-8") if candidate.is_file() else instr
    return spec, md


@dataclass
class FieldDiff:
    field: str
    action: str
    before: Any = None
    after: Any = None
    note: str = ""


def plan_agent(
    desired: dict,
    live: dict | None,
    desired_md: str | None = None,
    live_md: str | None = None,
) -> list[FieldDiff]:
    """Diff one desired manifest against a live retrieve-agent payload.

    Fields omitted from `desired` are unmanaged. Matching values are
    `noop`; writable mismatches are `update`. A missing live agent is
    `create`. Hidden live values are `skip`.
    """
    if live is None:
        diffs = [FieldDiff(
            "agent",
            "create",
            None,
            desired.get("name") or desired.get("slug"),
            "create workflow + instructions page via saveTransactionsFanout",
        )]
        for field_name in MANAGED_FIELDS:
            if field_name not in desired:
                continue
            action = "update" if field_name == "credit_limit" else "create"
            diffs.append(FieldDiff(field_name, action, None, desired.get(field_name)))
        if "instructions" in desired or desired_md is not None:
            diffs.append(FieldDiff(
                "instructions", "create", None,
                normalize_md(desired_md) if desired_md is not None else None,
            ))
        return diffs
    diffs: list[FieldDiff] = []
    for field_name in MANAGED_FIELDS:
        if field_name not in desired:
            continue
        after = desired.get(field_name)
        before = live.get(field_name)
        if field_name in {"connections", "triggers"}:
            before = before or []
        if before == "hidden":
            diffs.append(FieldDiff(
                field_name, "skip", before, after, "live value hidden (need full access)",
            ))
            continue
        if after == before:
            diffs.append(FieldDiff(field_name, "noop", before, after))
        elif field_name in WRITABLE_FIELDS:
            diffs.append(FieldDiff(field_name, "update", before, after))
        else:
            diffs.append(FieldDiff(
                field_name, "unsupported", before, after,
                "cannot write this field",
            ))
    if "instructions" in desired or desired_md is not None:
        after = normalize_md(desired_md) if desired_md is not None else None
        before = normalize_md(live_md) if live_md is not None else None
        page_id = live.get("instructions_page_id")
        if after == before:
            diffs.append(FieldDiff("instructions", "noop", before, after))
        elif not page_id:
            diffs.append(FieldDiff(
                "instructions", "unsupported", before, after,
                "agent has no instructions page to rewrite",
            ))
        else:
            diffs.append(FieldDiff("instructions", "update", before, after))
    return diffs


def resolve_live_id(
    spec: dict,
    state: dict,
    live_by_id: dict[str, dict],
    live_by_name: dict[str, list[str]],
) -> str | None:
    if spec.get("id"):
        return spec["id"]
    slug = spec.get("slug")
    stored = (state.get("agents") or {}).get(slug) if slug else None
    if isinstance(stored, dict) and stored.get("id"):
        return stored["id"]
    name = spec.get("name")
    if name and name in live_by_name and len(live_by_name[name]) == 1:
        return live_by_name[name][0]
    return None


def description_to_segments(value: Any) -> Any:
    """Store a yaml string as v3 rich-text segments; leave lists alone."""
    if value is None or isinstance(value, list):
        return value
    return [[str(value)]]


def workflow_write_value(field_name: str, value: Any) -> Any:
    if field_name == "description":
        return description_to_segments(value)
    return value


def new_workflow_records(
    *,
    workflow_id: str,
    page_id: str,
    space_id: str,
    user_id: str,
    spec: dict,
    now: int,
) -> tuple[dict, dict]:
    """Minimal workflow + instructions page that Notion will attach bots to."""
    workflow = {
        "id": workflow_id,
        "version": 1,
        "parent_id": space_id,
        "parent_table": "space",
        "space_id": space_id,
        "alive": True,
        "created_time": now,
        "last_edited_time": now,
        "created_by_id": user_id,
        "created_by_table": "notion_user",
        "last_edited_by_id": user_id,
        "last_edited_by_table": "notion_user",
        "permissions": [
            {"role": "editor", "type": "user_permission", "user_id": user_id},
            {"role": "reader", "type": "space_permission"},
        ],
        "data": {
            "name": spec.get("name") or spec.get("slug") or "Untitled agent",
            "description": description_to_segments(spec.get("description")),
            "icon": spec.get("icon"),
            "instructions": {"id": page_id, "table": "block", "spaceId": space_id},
        },
    }
    if spec.get("model") is not None:
        workflow["data"]["model"] = spec["model"]
    if spec.get("status") is not None:
        workflow["data"]["status"] = spec["status"]
    if spec.get("connections") is not None:
        workflow["data"]["modules"] = spec["connections"]
    if spec.get("triggers") is not None:
        workflow["data"]["triggers"] = spec["triggers"]
    page = {
        "id": page_id,
        "type": "page",
        "alive": True,
        "space_id": space_id,
        "version": 1,
        "created_time": now,
        "last_edited_time": now,
        "parent_id": workflow_id,
        "parent_table": "workflow",
        "properties": {"title": [["Instructions"]]},
    }
    return workflow, page


def format_model(model):
    if isinstance(model, dict):
        if model.get("type"):
            effort = model.get("reasoningEffort")
            return f"{model['type']}/{effort}" if effort else str(model["type"])
        if model.get("mode") == "pinned":
            return str(model.get("id") or "pinned")
        return str(model.get("mode") or "auto")
    return str(model or "auto")


def format_agent_line(agent: dict) -> str:
    aid = agent.get("id") or ""
    return "\t".join([
        agent.get("status") or "?",
        agent.get("name") or "",
        agent.get("agent_type") or "",
        format_model(agent.get("model")),
        agent_url(str(aid)) if aid else "",
    ])


def format_agent_text(agent: dict, *, instructions: str | None = None) -> str:
    aid = str(agent.get("id") or "")
    lines = [
        f"id: {aid}",
        f"url: {agent_url(aid) if aid else ''}",
        f"name: {agent.get('name') or ''}",
        f"kind: {agent.get('agent_type') or ''}",
        f"status: {agent.get('status') or ''}",
        f"pause_reason: {agent.get('pause_reason')}",
        f"model: {format_model(agent.get('model'))}",
        f"credit_limit: {agent.get('credit_limit')}",
        f"instructions_page: {agent.get('instructions_page_id')}",
        f"connections: {len(agent.get('connections') or [])}",
        f"triggers: {len(agent.get('triggers') or [])}",
    ]
    desc = agent.get("description")
    if desc:
        lines.insert(4, f"description: {desc}")
    if instructions:
        lines.append("---")
        lines.append(instructions.rstrip())
    return "\n".join(lines)


def format_diffs(slug: str, agent_id: str | None, diffs: list[FieldDiff]) -> str:
    header = f"{slug}" + (f"\t{agent_url(agent_id)}" if agent_id else "")
    rows = [header]
    for d in diffs:
        extra = f"  # {d.note}" if d.note else ""
        if d.field == "instructions":
            before_n = 0 if d.before is None else d.before.count("\n")
            after_n = 0 if d.after is None else d.after.count("\n")
            rows.append(f"  {d.field}: {d.action}  ({before_n} → {after_n} lines){extra}")
        elif d.field in {"connections", "triggers", "icon", "model"}:
            rows.append(f"  {d.field}: {d.action}{extra}")
        else:
            rows.append(f"  {d.field}: {d.action}  {d.before!r} → {d.after!r}{extra}")
    return "\n".join(rows)

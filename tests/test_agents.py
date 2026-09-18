"""Unit tests for Custom Agent desired-state helpers. No network."""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
import notion_cli  # noqa: E402
from agent_iac import (  # noqa: E402
    FieldDiff,
    agent_to_manifest,
    agent_url,
    format_agent_line,
    format_diffs,
    iter_manifest_paths,
    load_agent_state,
    load_manifest,
    normalize_md,
    new_workflow_records,
    parse_agent_id,
    plan_agent,
    resolve_live_id,
    save_agent_state,
    slugify,
    unique_slug,
    flatten_rich_text,
    workflow_to_agent,
    workflow_write_value,
)

UUID = "3d15a735-8d5e-8165-87ae-0092813206cf"


def test_parse_agent_id_from_chat_url():
    assert parse_agent_id(
        f"https://app.notion.com/agent/{UUID.replace('-', '')}?wfv=chat",
        notion_cli.parse_id,
    ) == UUID


def test_parse_agent_id_personal_alias():
    assert parse_agent_id("notion_ai", notion_cli.parse_id) == "notion_ai"
    assert parse_agent_id("33333333-3333-3333-3333-333333333333", notion_cli.parse_id) == "notion_ai"


def test_agent_url_custom():
    assert agent_url(UUID) == f"https://app.notion.com/agent/{UUID.replace('-', '')}"


def test_slugify_and_unique():
    used: set[str] = set()
    assert unique_slug("Inbox triage!", used) == "inbox-triage"
    assert unique_slug("Inbox triage", used) == "inbox-triage-2"
    assert slugify("") == "agent"


def test_agent_to_manifest_drops_ephemeral_and_keeps_settings():
    agent = {
        "object": "agent",
        "id": UUID,
        "agent_type": "custom_agent",
        "name": "Inbox",
        "description": "file mail",
        "icon": {"type": "emoji", "emoji": "📥"},
        "model": {"mode": "pinned", "id": "claude-sonnet-5"},
        "status": "active",
        "pause_reason": None,
        "credit_limit": 200,
        "connections": [{"type": "notion", "permissions": []}],
        "triggers": [{"type": "recurrence", "enabled": True}],
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_run_at": None,
        "instructions": "do the thing",
        "instructions_page_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "has_unpublished_changes": False,
        "is_favorited": True,
    }
    got = agent_to_manifest(agent, "inbox", "inbox.md")
    assert got["kind"] == "custom_agent"
    assert got["slug"] == "inbox"
    assert got["instructions"] == "inbox.md"
    assert got["credit_limit"] == 200
    assert "created_time" not in got
    assert "pause_reason" not in got
    assert "instructions_page_id" not in got


def test_plan_writable_name_model_connections_triggers():
    desired = {
        "name": "Inbox 2",
        "status": "disabled",
        "credit_limit": 50,
        "connections": [{"type": "slack"}],
        "triggers": [{"enabled": True}],
        "model": {"type": "soursop-shortcake", "reasoningEffort": "high"},
    }
    live = {
        "name": "Inbox",
        "status": "active",
        "credit_limit": 50,
        "connections": [{"type": "notion"}],
        "triggers": [],
        "model": None,
        "instructions_page_id": UUID,
    }
    diffs = {d.field: d for d in plan_agent(desired, live)}
    assert diffs["status"].action == "update"
    assert diffs["credit_limit"].action == "noop"
    assert diffs["name"].action == "update"
    assert diffs["connections"].action == "update"
    assert diffs["triggers"].action == "update"
    assert diffs["model"].action == "update"


def test_plan_create_is_create():
    diffs = {d.field: d for d in plan_agent(
        {"name": "New", "model": {"type": "soursop-shortcake"}, "connections": []},
        None,
    )}
    assert diffs["agent"].action == "create"
    assert diffs["name"].action == "create"
    assert diffs["model"].action == "create"
    assert diffs["connections"].action == "create"


def test_plan_omitted_fields_are_unmanaged():
    diffs = plan_agent({"status": "active"}, {"status": "active", "name": "Inbox"})
    assert [d.field for d in diffs] == ["status"]


def test_plan_hidden_credit_limit_is_skipped():
    diffs = plan_agent({"credit_limit": 10}, {"credit_limit": "hidden"})
    assert diffs[0].action == "skip"


def test_plan_instructions_need_a_page():
    diffs = {d.field: d for d in plan_agent(
        {"instructions": "x.md"}, {"name": "A"}, desired_md="new", live_md="old",
    )}
    assert diffs["instructions"].action == "unsupported"
    diffs = {d.field: d for d in plan_agent(
        {"instructions": "x.md"},
        {"instructions_page_id": UUID},
        desired_md="hello\n",
        live_md="hello",
    )}
    assert diffs["instructions"].action == "noop"
    diffs = {d.field: d for d in plan_agent(
        {"instructions": "x.md"},
        {"instructions_page_id": UUID},
        desired_md="new",
        live_md="old",
    )}
    assert diffs["instructions"].action == "update"


def test_normalize_md_collapses_newlines():
    assert normalize_md("a\r\n\r\n") == "a\n"


def test_state_roundtrip(tmp_path: Path):
    state = {"version": 1, "agents": {"inbox": {"id": UUID}}}
    save_agent_state(tmp_path, state)
    assert load_agent_state(tmp_path)["agents"]["inbox"]["id"] == UUID


def test_load_manifest_reads_sidecar_md(tmp_path: Path):
    (tmp_path / "inbox.md").write_text("# hi\n")
    (tmp_path / "inbox.yaml").write_text(
        yaml.safe_dump({"kind": "custom_agent", "name": "Inbox", "instructions": "inbox.md"})
    )
    spec, md = load_manifest(tmp_path / "inbox.yaml")
    assert spec["slug"] == "inbox"
    assert md == "# hi\n"


def test_load_manifest_inline_instructions(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("name: A\ninstructions: |\n  do it\n")
    spec, md = load_manifest(tmp_path / "a.yaml")
    assert spec["slug"] == "a"
    assert md.strip() == "do it"


def test_iter_manifest_paths(tmp_path: Path):
    (tmp_path / "a.yaml").write_text("name: A\n")
    (tmp_path / "b.yml").write_text("name: B\n")
    (tmp_path / ".state.json").write_text("{}")
    names = [p.name for p in iter_manifest_paths(tmp_path)]
    assert names == ["a.yaml", "b.yml"]


def test_resolve_live_id_prefers_yaml_then_state_then_unique_name():
    spec = {"slug": "inbox", "name": "Inbox"}
    state = {"agents": {"inbox": {"id": UUID}}}
    assert resolve_live_id({"id": "other"}, state, {}, {}) == "other"
    assert resolve_live_id(spec, state, {}, {}) == UUID
    assert resolve_live_id(
        {"name": "Inbox"}, {"agents": {}}, {}, {"Inbox": [UUID]}
    ) == UUID
    assert resolve_live_id(
        {"name": "Inbox"}, {"agents": {}}, {}, {"Inbox": [UUID, "dup"]}
    ) is None


def test_format_agent_line_and_plan_text():
    line = format_agent_line({
        "id": UUID,
        "name": "Inbox",
        "status": "active",
        "agent_type": "custom_agent",
        "model": {"mode": "pinned", "id": "claude-sonnet-5"},
    })
    assert line.startswith("active\tInbox\tcustom_agent\tclaude-sonnet-5\t")
    text = format_diffs("inbox", UUID, [
        FieldDiff("status", "update", "active", "disabled"),
    ])
    assert "inbox" in text
    assert "status: update" in text


def test_public_api_prefers_explicit_key(monkeypatch):
    from agent_iac import PublicApi

    monkeypatch.setenv("NOTION_API_KEY", "env-key")
    api = PublicApi({"api_key": "cfg-key"}, api_key="explicit")
    assert api.s.headers["Authorization"] == "Bearer explicit"
    api = PublicApi({"api_key": "cfg-key"})
    assert api.s.headers["Authorization"] == "Bearer env-key"


def test_public_api_missing_key_explains_pat(monkeypatch):
    from agent_iac import PublicApi

    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(Exception, match="personal access token"):
        PublicApi({})


def test_flatten_rich_text_segments_and_plain():
    assert flatten_rich_text([["Turns slack thread into projects."]]) == "Turns slack thread into projects."
    assert flatten_rich_text("already plain") == "already plain"
    assert flatten_rich_text(None) is None


def test_workflow_to_agent_maps_modules_and_instruction_page():
    rec = {
        "id": UUID,
        "alive": True,
        "version": 3,
        "data": {
            "name": "Notion Idea",
            "description": [["Turns slack thread into projects."]],
            "icon": "https://example/icon.png",
            "model": {"type": "soursop-shortcake", "reasoningEffort": "high"},
            "status": "runnable",
            "modules": [{"type": "notion"}],
            "triggers": [{"enabled": True}],
            "instructions": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "table": "block"},
        },
    }
    agent = workflow_to_agent(rec)
    assert agent["name"] == "Notion Idea"
    assert agent["description"] == "Turns slack thread into projects."
    assert agent["connections"] == [{"type": "notion"}]
    assert agent["instructions_page_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert agent["status"] == "runnable"

def test_workflow_write_value_segments_description():
    assert workflow_write_value("description", "plain") == [["plain"]]
    assert workflow_write_value("description", [["already"]]) == [["already"]]
    assert workflow_write_value("name", "Inbox") == "Inbox"


def test_new_workflow_records_maps_connections_to_modules():
    wf, page = new_workflow_records(
        workflow_id=UUID,
        page_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        space_id="space",
        user_id="user",
        spec={
            "name": "Scratch",
            "description": "hi",
            "model": {"type": "soursop-shortcake"},
            "status": "runnable",
            "connections": [{"type": "notion"}],
            "triggers": [{"enabled": True}],
        },
        now=1,
    )
    assert wf["data"]["name"] == "Scratch"
    assert wf["data"]["description"] == [["hi"]]
    assert wf["data"]["model"] == {"type": "soursop-shortcake"}
    assert wf["data"]["status"] == "runnable"
    assert wf["data"]["modules"] == [{"type": "notion"}]
    assert wf["data"]["triggers"] == [{"enabled": True}]
    assert wf["data"]["instructions"]["id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert page["parent_id"] == UUID
    assert page["parent_table"] == "workflow"


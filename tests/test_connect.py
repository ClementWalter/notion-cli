#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "requests==2.34.2",
#     "click==8.4.2",
#     "pycryptodome==3.23.0",
#     "pyyaml==6.0.3",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Terminal-free use from Claude Desktop: `connect`, workspace choice, session refresh."""

import json
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notion_cli as cli  # noqa: E402

TEAM = {"user_id": "u1", "email": "ada@example.com", "user_name": "Ada", "space_id": "aaaa-1111", "space_name": "Team"}
HOME = {"user_id": "u1", "email": "ada@example.com", "user_name": "Ada", "space_id": "bbbb-2222", "space_name": "Home"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real config, vault broker or environment session."""
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(cli, "auth_broker", lambda *a: {})
    for name in ("NOTION_TOKEN_V2", "NOTION_SPACE", "NOTION_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def browser(monkeypatch):
    """A signed-in Chrome whose session sees two workspaces."""
    monkeypatch.setattr(cli, "iter_tokens", lambda source: iter([("v03:fresh", "chrome")]))
    monkeypatch.setattr(cli, "list_spaces", lambda token: [TEAM, HOME])


@pytest.fixture
def signed_out(monkeypatch):
    monkeypatch.setattr(cli, "iter_tokens", lambda source: iter([]))


def test_env_value_drops_unfilled_extension_setting(monkeypatch):
    monkeypatch.setenv("NOTION_SPACE", "${user_config.workspace}")
    assert cli.env_value("NOTION_SPACE") is None


def test_env_value_keeps_a_real_setting(monkeypatch):
    monkeypatch.setenv("NOTION_SPACE", " Home ")
    assert cli.env_value("NOTION_SPACE") == "Home"


def test_match_spaces_by_name_substring():
    assert cli.match_spaces([TEAM, HOME], "hom") == [HOME]


def test_match_spaces_by_undashed_id():
    assert cli.match_spaces([TEAM, HOME], "bbbb2222") == [HOME]


def test_match_spaces_error_lists_workspaces():
    with pytest.raises(click.ClickException, match="Home, Team"):
        cli.match_spaces([TEAM, HOME], "nowhere")


def test_choose_space_keeps_the_bound_workspace():
    assert cli.choose_space([TEAM, HOME], None, "bbbb-2222") == HOME


def test_choose_space_defaults_to_the_first():
    assert cli.choose_space([TEAM, HOME], None, None) == TEAM


def test_space_setting_matching_saved_space_needs_no_call(monkeypatch):
    monkeypatch.setenv("NOTION_SPACE", "Team")
    monkeypatch.setattr(cli, "list_spaces", lambda token: pytest.fail("network"))
    assert cli.apply_space_setting({"space_id": "aaaa-1111", "space_name": "Team"}, "t")["space_id"] == "aaaa-1111"


def test_space_setting_switches_workspace(monkeypatch, browser):
    monkeypatch.setenv("NOTION_SPACE", "Home")
    assert cli.apply_space_setting({"space_id": "aaaa-1111", "space_name": "Team"}, "t")["space_id"] == "bbbb-2222"


def test_env_only_token_binds_a_workspace(browser):
    assert cli.apply_space_setting({}, "v03:pasted")["space_id"] == "aaaa-1111"


def test_connect_saves_the_browser_session(browser):
    CliRunner().invoke(cli.cli, ["connect", "--json"])
    assert json.loads(cli.CONFIG_PATH.read_text())["token_v2"] == "v03:fresh"


def test_connect_never_prompts_with_several_workspaces(browser):
    assert CliRunner().invoke(cli.cli, ["connect"], input="").exit_code == 0


def test_connect_reports_the_other_workspaces(browser):
    result = CliRunner().invoke(cli.cli, ["connect", "--json"])
    assert json.loads(result.stdout)["other_spaces"] == ["Home"]


def test_connect_space_argument_binds_that_workspace(browser):
    CliRunner().invoke(cli.cli, ["connect", "--space", "Home"])
    assert json.loads(cli.CONFIG_PATH.read_text())["space_id"] == "bbbb-2222"


def test_connect_reply_hides_the_token(browser):
    assert "v03:fresh" not in CliRunner().invoke(cli.cli, ["connect", "--json"]).stdout


def test_connect_signed_out_names_the_connect_tool(signed_out):
    assert "`connect` tool" in CliRunner().invoke(cli.cli, ["connect"]).output


def test_connect_signed_out_names_the_keychain_item(signed_out):
    assert '"Chrome Safe Storage"' in CliRunner().invoke(cli.cli, ["connect"]).output


def test_whoami_signed_out_says_where_to_sign_in(signed_out):
    assert "https://www.notion.so" in CliRunner().invoke(cli.cli, ["whoami"]).output


def test_first_read_imports_the_browser_session(browser):
    cli.api_or_die()
    assert json.loads(cli.CONFIG_PATH.read_text())["space_name"] == "Team"


@pytest.fixture
def two_chrome_profiles(monkeypatch, tmp_path):
    """Two Chrome profiles holding a Notion cookie each; counts keychain reads."""
    stores = []
    for name in ("Default", "Profile 1"):
        db = tmp_path / name
        db.touch()
        stores.append(("chrome", str(db), "Chrome Safe Storage"))
    reads = []
    monkeypatch.setattr(cli, "_COOKIE_SOURCES", stores)
    monkeypatch.setattr(cli, "stored_cookies", lambda db: [db.name.encode()])
    monkeypatch.setattr(cli, "keychain_secret", lambda service: reads.append(service) or "secret")
    monkeypatch.setattr(cli, "_decrypt_chromium_cookie", lambda enc, secret: "v03:" + enc.decode())
    return reads


def test_keychain_is_read_once_per_browser(two_chrome_profiles):
    list(cli.iter_tokens("auto"))
    assert two_chrome_profiles == ["Chrome Safe Storage"]


def test_keychain_untouched_without_a_notion_cookie(monkeypatch, tmp_path):
    db = tmp_path / "Cookies"
    db.touch()
    monkeypatch.setattr(cli, "_COOKIE_SOURCES", [("chrome", str(db), "Chrome Safe Storage")])
    monkeypatch.setattr(cli, "stored_cookies", lambda db: [])
    monkeypatch.setattr(cli, "keychain_secret", lambda service: pytest.fail("keychain read"))
    assert list(cli.iter_tokens("auto")) == []


def test_unanswered_keychain_prompt_names_always_allow(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired("security", 45)
    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(click.ClickException, match="Always Allow"):
        cli.keychain_secret("Chrome Safe Storage")


class StubResponse:
    def __init__(self, status: int):
        self.status_code = status
        self.ok = status == 200
        self.headers = {}
        self.text = "{}"

    def json(self):
        return {"ok": True}


@pytest.fixture
def expired_then_ok(monkeypatch):
    """Notion answers 401 to the stale cookie and 200 to any other."""
    def post(self, url, json=None, timeout=None):
        return StubResponse(401 if self.headers["Cookie"] == "token_v2=v03:stale" else 200)
    monkeypatch.setattr(cli.requests.Session, "post", post)


def test_expired_session_retries_with_the_browser_one(expired_then_ok):
    api = cli.Api({"token_v2": "v03:stale"}, refresh=lambda: "v03:fresh")
    assert api.post("getSpaces", {}) == {"ok": True}


def test_expired_session_without_browser_one_says_reconnect(expired_then_ok):
    api = cli.Api({"token_v2": "v03:stale"}, refresh=lambda: None)
    with pytest.raises(click.ClickException, match="`connect` tool"):
        api.post("getSpaces", {})


def test_expired_session_refreshes_only_once(expired_then_ok):
    calls = []
    api = cli.Api({"token_v2": "v03:stale"}, refresh=lambda: calls.append(1) or "v03:stale")
    with pytest.raises(click.ClickException):
        api.post("getSpaces", {})
    assert calls == [1]


def test_agents_export_defaults_to_downloads(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_export_agents", lambda dest, *a: seen.update(dest=dest))
    CliRunner().invoke(cli.cli, ["agents", "export"])
    assert seen["dest"] == Path.home() / "Downloads" / "Notion" / "agents"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

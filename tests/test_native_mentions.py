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
"""Unit tests for native Slack/Linear/GitHub mentions: `@[label](url)` chips to
Slack messages, Linear issues or GitHub objects are upgraded to `eoi` pills backed by `external_object_instance` records built from
Notion's `unfurlUrl` response, and rendered back as `@[message](url)`. No network.
"""

import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import (  # noqa: E402
    eoi_names,
    eoi_records,
    link_mention_segment,
    md_to_segments,
    native_mention_integration,
    native_mentions,
    seg_to_md,
)

SPACE = "e665eddd-2a6d-4d3f-928a-0eabb195e2e4"
PARENT = "11111111-1111-1111-1111-111111111111"
SLACK_URL = "https://acme.slack.com/archives/C0000000001/p1790078504822339"
LINEAR_URL = "https://linear.app/acme/issue/PRO-459/create-rfc"
DRIVE_URL = "https://docs.google.com/document/d/1B9Uwm6YVsJ2/edit"
LINEAR_UNFURL = {"root": {"external_object_id": "ext-issue", "bot_id": "bot-linear", "uri": LINEAR_URL,
                          "attributes": [{"id": "title", "values": ["Create RFC"]}, {"id": "identifier", "values": ["PRO-459"]}]}}
UNFURL = {
    "root": {
        "external_object_id": "ext-root",
        "bot_id": "bot-slack",
        "uri": SLACK_URL,
        "attributes": [{"id": "title", "values": [["I think we need to properly spec analytics"]]}],
    },
    "relations": [
        {"external_object_id": "ext-user", "bot_id": "bot-slack", "uri": "https://slack.com/users/U1",
         "attributes": [{"id": "title", "values": ["Rand Hindi"]}]},
        {"external_object_id": "ext-chan", "bot_id": "bot-slack", "uri": "https://slack.com/channels/C1",
         "attributes": [{"id": "title", "values": ["#product"]}]},
    ],
}


class StubApi:
    """Stands in for `Api`: `unfurl` answers from a fixed table instead of the network."""

    space_id = SPACE

    def __init__(self, responses: dict):
        self.responses = responses

    def unfurl(self, url: str, integration: str):
        return self.responses.get((url, integration))


@pytest.fixture
def records():
    return eoi_records(UNFURL, SLACK_URL, PARENT, SPACE)


@pytest.mark.parametrize("url", [
    SLACK_URL,
    SLACK_URL + "?thread_ts=1790078504.822339&cid=C0000000001",
    "https://acme.slack.com/archives/C0000000001",
])
def test_slack_archive_url_maps_to_slack(url):
    assert native_mention_integration(url) == "Slack"


@pytest.mark.parametrize("url", [
    LINEAR_URL,
    "https://linear.app/acme/project/wrappers-new-erc-7984-features-48f34027",
])
def test_linear_url_maps_to_linear(url):
    assert native_mention_integration(url) == "Linear"


@pytest.mark.parametrize("url", [
    "https://github.com/acme/fhevm/blob/8ff0de4/protocol-contracts/contracts/token/contracts/Token.sol#L102",
    "https://github.com/acme/defi-app/pull/176",
    "https://github.com/acme/fhevm/issues/1",
    "https://github.com/acme/fhevm",
])
def test_github_url_maps_to_github(url):
    assert native_mention_integration(url) == "GitHub"


@pytest.mark.parametrize("url", [
    DRIVE_URL,
    "https://linear.app/acme/settings",
    "https://github.com/features",
    "https://acme.slack.com/docs/T00000000/F0000000001",
    "https://evil.example/acme.slack.com/archives/C1/p1",
    "",
])
def test_other_url_has_no_native_integration(url):
    assert native_mention_integration(url) is None


def test_root_instance_is_parented_to_mentioning_block(records):
    _, ops = records
    assert ops[0]["args"]["parent_id"] == PARENT


def test_root_instance_keeps_original_url(records):
    _, ops = records
    assert ops[0]["args"]["format"]["original_url"] == SLACK_URL


def test_one_child_instance_per_relation(records):
    _, ops = records
    assert len(ops) == 1 + len(UNFURL["relations"])


def test_children_are_parented_to_root_instance(records):
    eid, ops = records
    assert {o["args"]["parent_id"] for o in ops[1:]} == {eid}


def test_root_maps_relation_uris_to_child_ids(records):
    _, ops = records
    assert set(ops[0]["args"]["format"]["related_external_object_uris_to_instance_ids"].values()) == {o["pointer"]["id"] for o in ops[1:]}


def test_slack_chip_becomes_eoi_pill():
    segs, _ = native_mentions(StubApi({(SLACK_URL, "Slack"): UNFURL}), [link_mention_segment(SLACK_URL, "label")], PARENT)
    assert segs[0][1][0][0] == "eoi"


def test_slack_chip_upgrade_emits_instance_ops():
    _, ops = native_mentions(StubApi({(SLACK_URL, "Slack"): UNFURL}), [link_mention_segment(SLACK_URL, "label")], PARENT)
    assert len(ops) == 3


def test_linear_chip_becomes_eoi_pill():
    segs, _ = native_mentions(StubApi({(LINEAR_URL, "Linear"): LINEAR_UNFURL}), [link_mention_segment(LINEAR_URL, "PRO-459")], PARENT)
    assert segs[0][1][0][0] == "eoi"


def test_linear_chip_upgrade_emits_one_instance():
    _, ops = native_mentions(StubApi({(LINEAR_URL, "Linear"): LINEAR_UNFURL}), [link_mention_segment(LINEAR_URL, "PRO-459")], PARENT)
    assert len(ops) == 1


def test_chip_without_native_integration_is_kept():
    chip = link_mention_segment(DRIVE_URL, "notes")
    segs, _ = native_mentions(StubApi({}), [chip], PARENT)
    assert segs == [chip]


def test_slack_chip_kept_when_unfurl_fails():
    chip = link_mention_segment(SLACK_URL, "label")
    segs, _ = native_mentions(StubApi({}), [chip], PARENT)
    assert segs == [chip]


def test_plain_text_segment_is_kept():
    segs, _ = native_mentions(StubApi({(SLACK_URL, "Slack"): UNFURL}), [["see ", [["a", SLACK_URL]]]], PARENT)
    assert segs == [["see ", [["a", SLACK_URL]]]]


def test_eoi_names_label_from_rich_text_title(records):
    eid, ops = records
    assert eoi_names(ops[0]["args"])[eid] == "I think we need to properly spec analytics"


def test_eoi_names_label_from_string_title(records):
    _, ops = records
    child = ops[1]["args"]
    assert eoi_names(child)[child["id"]] == "Rand Hindi"


def test_eoi_names_uri_prefers_original_url(records):
    eid, ops = records
    assert eoi_names(ops[0]["args"])["uri:" + eid] == SLACK_URL


def test_eoi_pill_renders_writeable_link_mention(records):
    eid, ops = records
    assert seg_to_md([["‣", [["eoi", eid]]]], eoi_names(ops[0]["args"]), writeable=True) == f"@[I think we need to properly spec analytics]({SLACK_URL})"


def test_eoi_pill_renders_plain_link(records):
    eid, ops = records
    assert seg_to_md([["‣", [["eoi", eid]]]], eoi_names(ops[0]["args"])) == f"[I think we need to properly spec analytics]({SLACK_URL})"


@pytest.mark.parametrize("url", [SLACK_URL, SLACK_URL + "?thread_ts=1790078504.822339&cid=C0000000001"])
def test_plain_slack_hyperlink_is_refused(url):
    with pytest.raises(click.ClickException):
        md_to_segments(f"see [the thread]({url})", users={})


def test_slack_link_mention_is_accepted():
    assert md_to_segments(f"@[the thread]({SLACK_URL})", users={})[0][1][0][0] == "lm"


def test_plain_linear_hyperlink_is_refused():
    with pytest.raises(click.ClickException):
        md_to_segments(f"[PRO-459]({LINEAR_URL})", users={})


def test_other_hyperlink_is_accepted():
    assert md_to_segments(f"[notes]({DRIVE_URL})", users={}) == [["notes", [["a", DRIVE_URL]]]]


def test_slack_docs_hyperlink_is_accepted():
    url = "https://acme.slack.com/docs/T00000000/F0000000001"
    assert md_to_segments(f"[canvas]({url})", users={}) == [["canvas", [["a", url]]]]


def test_plain_github_hyperlink_is_refused():
    with pytest.raises(click.ClickException):
        md_to_segments("[PR 176](https://github.com/acme/defi-app/pull/176)", users={})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

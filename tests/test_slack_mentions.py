"""Unit tests for native Slack mentions: Slack `@[label](url)` chips are
upgraded to `eoi` pills backed by `external_object_instance` records built from
Notion's `unfurlUrl` response, and rendered back as `@[message](url)`. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import (  # noqa: E402
    eoi_names,
    eoi_records,
    is_slack_archive_url,
    link_mention_segment,
    native_slack_mentions,
    seg_to_md,
)

SPACE = "12345678-0000-4000-8000-000000000003"
PARENT = "11111111-1111-1111-1111-111111111111"
SLACK_URL = "https://acme.slack.com/archives/C0000000001/p1790078504822339"
LINEAR_URL = "https://linear.app/acme/issue/PRO-459/create-rfc"
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

    def unfurl(self, url: str):
        return self.responses.get(url)


@pytest.fixture
def records():
    return eoi_records(UNFURL, SLACK_URL, PARENT, SPACE)


@pytest.mark.parametrize("url", [
    SLACK_URL,
    SLACK_URL + "?thread_ts=1790078504.822339&cid=C0000000001",
    "https://acme.slack.com/archives/C0000000001",
])
def test_slack_archive_url_is_detected(url):
    assert is_slack_archive_url(url)


@pytest.mark.parametrize("url", [
    LINEAR_URL,
    "https://acme.slack.com/docs/T00000000/F0000000001",
    "https://evil.example/acme.slack.com/archives/C1/p1",
    "",
])
def test_non_archive_url_is_not_detected(url):
    assert not is_slack_archive_url(url)


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
    segs, _ = native_slack_mentions(StubApi({SLACK_URL: UNFURL}), [link_mention_segment(SLACK_URL, "label")], PARENT)
    assert segs[0][1][0][0] == "eoi"


def test_slack_chip_upgrade_emits_instance_ops():
    _, ops = native_slack_mentions(StubApi({SLACK_URL: UNFURL}), [link_mention_segment(SLACK_URL, "label")], PARENT)
    assert len(ops) == 3


def test_non_slack_chip_is_kept():
    chip = link_mention_segment(LINEAR_URL, "PRO-459")
    segs, _ = native_slack_mentions(StubApi({}), [chip], PARENT)
    assert segs == [chip]


def test_slack_chip_kept_when_unfurl_fails():
    chip = link_mention_segment(SLACK_URL, "label")
    segs, _ = native_slack_mentions(StubApi({}), [chip], PARENT)
    assert segs == [chip]


def test_plain_text_segment_is_kept():
    segs, _ = native_slack_mentions(StubApi({SLACK_URL: UNFURL}), [["see ", [["a", SLACK_URL]]]], PARENT)
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

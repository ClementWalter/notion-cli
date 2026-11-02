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
"""Unit tests for resolving a page ref to the database in its body.

`query`/`schema` are routinely handed a hub page (or a database row) whose body
embeds the wanted database; these pin which databases count and the error that
names them. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import inline_databases, no_collection_message  # noqa: E402

PAGE = "00000000-0000-0000-0000-00000000000a"
DB_ID = "00000000-0000-0000-0000-0000000000db"


@pytest.fixture
def hub():
    """A page: text, a column holding a database, a child page holding another."""
    return {
        PAGE: {"id": PAGE, "type": "page", "content": ["t1", "col", "child", "gone"]},
        "t1": {"id": "t1", "type": "text"},
        "col": {"id": "col", "type": "column", "content": [DB_ID]},
        DB_ID: {"id": DB_ID, "type": "collection_view", "collection_id": "c1", "content": ["row"]},
        "row": {"id": "row", "type": "page"},
        "child": {"id": "child", "type": "page", "content": ["nested"]},
        "nested": {"id": "nested", "type": "collection_view", "collection_id": "c2"},
        "gone": {"id": "gone", "type": "collection_view", "collection_id": "c3", "alive": False},
    }


def test_finds_database_nested_in_a_column(hub):
    assert [b["id"] for b in inline_databases(hub, PAGE)] == [DB_ID]


def test_page_without_body_has_no_database():
    assert inline_databases({PAGE: {"id": PAGE, "type": "page"}}, PAGE) == []


def test_message_for_a_plain_page_points_to_page():
    assert "read it with `notion page x`" in no_collection_message("x", [], {})


def test_message_lists_each_database_with_its_url(hub):
    dbs = [hub[DB_ID], {"id": "00000000-0000-0000-0000-0000000000dc", "collection_id": "c9"}]
    colls = {"c1": {"name": [["Pipeline"]]}}
    msg = no_collection_message("x", dbs, colls)
    assert f"§db [Pipeline](https://www.notion.so/{DB_ID.replace('-', '')})" in msg

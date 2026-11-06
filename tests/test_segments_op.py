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
"""Unit tests for `segments_op`, the op `edit` sends to rewrite a block's text.

Table rows reject a raw `set` on `properties.<id>`, so a cell goes through
`updateBlockPropertyValue`; a text block keeps the plain `set`. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import segments_op  # noqa: E402


@pytest.fixture
def cell_op():
    return segments_op({"id": "row", "type": "table_row"}, "abc", [["x"]], "space")


@pytest.fixture
def text_op():
    return segments_op({"id": "blk", "type": "text"}, "title", [["x"]], "space")


def test_table_cell_uses_high_level_property_op(cell_op):
    assert cell_op["command"] == "updateBlockPropertyValue"


def test_table_cell_updates_only_its_column(cell_op):
    assert cell_op["args"] == {"primitiveOp": {"command": "update", "args": {"abc": [["x"]]}}}


def test_table_cell_targets_the_row(cell_op):
    assert cell_op["pointer"]["id"] == "row"


def test_text_block_uses_plain_set(text_op):
    assert text_op["command"] == "set"


def test_text_block_sets_the_title_path(text_op):
    assert text_op["path"] == ["properties", "title"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

"""Unit tests for `move_to_top_ops`, which backs `append --top`.

New blocks are appended at the end by `blocks_to_ops`, then each one is moved
right above the page's first original block, so their order is kept. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import move_to_top_ops  # noqa: E402


@pytest.fixture
def ops():
    return move_to_top_ops("page", ["a", "b"], "first", "space")


def test_each_new_block_is_removed_then_reinserted(ops):
    assert [o["command"] for o in ops] == ["listRemove", "listBefore", "listRemove", "listBefore"]


def test_new_blocks_land_before_the_first_original_block_in_order(ops):
    assert [o["args"] for o in ops if o["command"] == "listBefore"] == [
        {"id": "a", "before": "first"},
        {"id": "b", "before": "first"},
    ]


def test_ops_target_the_page_content_list(ops):
    assert {(o["pointer"]["id"], tuple(o["path"])) for o in ops} == {("page", ("content",))}


def test_empty_page_keeps_the_appended_order():
    assert move_to_top_ops("page", ["a"], None, "space") == []

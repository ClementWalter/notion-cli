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
"""Unit tests for `check_ops`, the transaction that ticks a to-do block.

A raw `set` on `properties.checked` is rejected on CRDT to-dos, so the
checkbox goes through `updateBlockPropertyValue`. No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import check_ops  # noqa: E402


@pytest.fixture
def tick_op():
    return check_ops("blk", False, "space")[0]


def test_check_uses_high_level_property_op(tick_op):
    assert tick_op["command"] == "updateBlockPropertyValue"


def test_check_sets_checked_yes(tick_op):
    assert tick_op["args"] == {"primitiveOp": {"command": "update", "args": {"checked": [["Yes"]]}}}


def test_uncheck_sets_checked_no():
    assert check_ops("blk", True, "space")[0]["args"]["primitiveOp"]["args"] == {"checked": [["No"]]}


def test_check_bumps_last_edited_time():
    assert set(check_ops("blk", False, "space")[1]["args"]) == {"last_edited_time"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))

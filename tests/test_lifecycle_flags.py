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
"""Unit tests for `lifecycle_flags`, the trash and archive state `page` prints.

An archived row stays alive but leaves database queries, so `page` must say so.
No network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import lifecycle_flags  # noqa: E402


def test_live_block_has_no_flags():
    assert lifecycle_flags({"alive": True}) == {}


def test_trashed_block_is_deleted():
    assert lifecycle_flags({"alive": False}) == {"deleted": True}


def test_archived_block_carries_utc_time():
    assert lifecycle_flags({"alive": True, "archived_time": 1793967541554}) == {"archived": "2026-11-06T12:19:01Z"}


def test_null_archived_time_is_not_archived():
    assert lifecycle_flags({"alive": True, "archived_time": None}) == {}

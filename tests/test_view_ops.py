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
"""Unit tests for the linked-view builders behind `notion view`.

Notion accepts any filter operator and silently returns no rows for an unknown
one, so the operator map and the value shapes are pinned here. No network.
"""

import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from notion_cli import linked_view_ops, view_filter, view_sort  # noqa: E402


@pytest.fixture
def coll():
    return {
        "id": "00000000-0000-0000-0000-000000001234",
        "name": [["Pipeline"]],
        "schema": {
            "title": {"name": "Launch", "type": "title"},
            "s1": {"name": "Stage", "type": "select", "options": [{"id": "o1", "value": "Discovery"}]},
            "st": {"name": "Status", "type": "status", "options": [{"id": "o2", "value": "Done"}]},
            "n1": {"name": "Budget", "type": "number"},
            "u1": {"name": "Contract", "type": "url"},
            "p1": {"name": "Owner", "type": "person"},
            "d1": {"name": " Batch", "type": "date"},
        },
    }


@pytest.mark.parametrize(
    ("expr", "operator"),
    [
        ("Stage=Discovery", "enum_is"),
        ("Stage!=Discovery", "enum_is_not"),
        ("Status=Done", "enum_is"),
        ("Launch=Opal", "string_is"),
        ("Launch!=Opal", "string_is_not"),
        ("Launch~Opal", "string_contains"),
        ("Title~Opal", "string_contains"),
        ("Contract~etherscan", "string_contains"),
        ("Budget=50000", "number_equals"),
        ("Budget!=50000", "number_does_not_equal"),
        ("Budget>50000", "number_greater_than"),
        ("Budget>=50000", "number_greater_than_or_equal_to"),
        ("Budget<50000", "number_less_than"),
        ("Budget<=50000", "number_less_than_or_equal_to"),
        ("Owner is_empty", "is_empty"),
        ("Stage is_not_empty", "is_not_empty"),
    ],
)
def test_filter_operator(coll, expr, operator):
    assert view_filter(expr, coll)["filter"]["operator"] == operator


def test_filter_targets_property_id(coll):
    assert view_filter("Stage=Discovery", coll)["property"] == "s1"


def test_filter_wraps_value_as_exact(coll):
    assert view_filter("Stage=Discovery", coll)["filter"]["value"] == {"type": "exact", "value": "Discovery"}


def test_number_filter_value_is_numeric(coll):
    assert view_filter("Budget>=50000", coll)["filter"]["value"]["value"] == 50000


def test_empty_filter_has_no_value(coll):
    assert "value" not in view_filter("Owner is_empty", coll)["filter"]


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ("Nope=1", "unknown property"),
        ("Stage=Launched", "not an option"),
        ("Stage~Disc", "select property"),
        ("Owner=someone", "only is_empty"),
        ("Budget=lots", "number property"),
        ("no operator here", "cannot parse"),
    ],
)
def test_filter_rejects(coll, expr, message):
    with pytest.raises(click.ClickException, match=message):
        view_filter(expr, coll)


@pytest.mark.parametrize(("expr", "direction"), [("Stage", "ascending"), ("Stage:asc", "ascending"), ("Stage:desc", "descending")])
def test_sort_direction(coll, expr, direction):
    assert view_sort(expr, coll)["direction"] == direction


def test_property_name_matches_without_stray_spaces(coll):
    assert view_sort("Batch", coll)["property"] == "d1"


def test_sort_rejects_unknown_direction(coll):
    with pytest.raises(click.ClickException, match="asc or desc"):
        view_sort("Stage:up", coll)


@pytest.fixture
def ops(coll):
    filters = [view_filter("Stage=Discovery", coll)]
    return linked_view_ops("page", coll, "Discovery", filters, [], ["title", "s1"], "space", after="anchor")[0]


def test_view_record_holds_filter(ops):
    assert ops[0]["args"]["query2"]["filter"]["filters"][0]["property"] == "s1"


def test_view_record_is_a_table(ops):
    assert ops[0]["args"]["type"] == "table"


def test_view_shows_listed_properties_in_order(ops):
    assert [p["property"] for p in ops[0]["args"]["format"]["table_properties"] if p["visible"]] == ["title", "s1"]


def test_view_hides_unlisted_properties(ops):
    assert [p["property"] for p in ops[0]["args"]["format"]["table_properties"] if not p["visible"]] == ["st", "n1", "u1", "p1", "d1"]


def test_block_points_at_the_view(ops):
    assert ops[1]["args"]["view_ids"] == [ops[0]["args"]["id"]]


def test_block_links_the_collection(ops):
    assert ops[1]["args"]["format"]["collection_pointer"]["id"] == "00000000-0000-0000-0000-000000001234"


def test_block_lands_after_anchor(ops):
    assert ops[2]["args"] == {"id": ops[1]["args"]["id"], "after": "anchor"}


def test_block_appends_without_anchor(coll):
    ops, block_id = linked_view_ops("page", coll, "All", [], [], None, "space")
    assert ops[2]["args"] == {"id": block_id}


def test_block_lands_before_first_block_on_top(coll):
    ops, block_id = linked_view_ops("page", coll, "All", [], [], None, "space", before="first")
    assert ops[2]["command"] == "listBefore"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))

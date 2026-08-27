"""Unit tests for notion_cli's pure conversion layers (v3 record model).

Covers the parts that do the token-saving work and are easy to get subtly
wrong: id parsing, segment rendering both ways, markdown→v3-block parsing,
property flattening/coercion by schema type, and the client-side filter DSL.
No network.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import notion_cli  # noqa: E402
from notion_cli import (  # noqa: E402
    _epoch_ms,
    apply_table_md_replace,
    block_to_spec,
    coerce_segments,
    dash,
    flatten_value,
    load_id_cache,
    make_matcher,
    md_to_segments,
    md_to_v3_blocks,
    merge_id_cache,
    parse_gfm_table,
    find_section,
    parse_id,
    property_text_matches,
    refuse_hard_delete,
    rewrite_named_mentions,
    save_id_cache,
    unique_names,
    seg_plain,
    seg_to_md,
    split_gfm_row,
)

UUID = "11aa22bb-33cc-44dd-55ee-66ff77aa88bb"
USER = "99aa88bb-77cc-46dd-55ee-44ff33aa22bb"


# ---- parse_id -------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        UUID,
        UUID.replace("-", ""),
        f"https://www.notion.so/myworkspace/Some-Page-{UUID.replace('-', '')}",
        f"https://app.notion.com/p/{UUID.replace('-', '')}",
        f"collection://{UUID}",
        f"https://www.notion.so/{UUID.replace('-', '')}?v=647ad414a07146c88c70a3953901139c",
    ],
)
def test_parse_id_accepts_common_forms(ref):
    assert parse_id(ref) == UUID


def test_parse_id_rejects_garbage():
    with pytest.raises(Exception):
        parse_id("not-an-id")


def test_dash_formats_uuid():
    assert dash(UUID.replace("-", "")) == UUID


# ---- segments -> markdown ---------------------------------------------------


def test_seg_bold():
    assert seg_to_md([["hi", [["b"]]]]) == "**hi**"


def test_seg_code():
    assert seg_to_md([["x=1", [["c"]]]]) == "`x=1`"


def test_seg_link():
    assert seg_to_md([["doc", [["a", "https://x.co"]]]]) == "[doc](https://x.co)"


def test_seg_bold_link_combines():
    assert seg_to_md([["doc", [["b"], ["a", "https://x.co"]]]]) == "[**doc**](https://x.co)"


def test_seg_user_mention_resolves_name():
    assert seg_to_md([["‣", [["u", USER]]]], names={USER: "Clément"}) == "@Clément"


def test_seg_user_mention_falls_back_to_id():
    assert seg_to_md([["‣", [["u", USER]]]]) == f"@{USER}"


def test_seg_page_mention_renders_link():
    got = seg_to_md([["‣", [["p", UUID, "space"]]]], names={UUID: "Vault"})
    assert got == f"[Vault](https://www.notion.so/{UUID.replace('-', '')})"


def test_seg_date_pill():
    assert seg_to_md([["‣", [["d", {"type": "date", "start_date": "2026-07-31"}]]]]) == "2026-07-31"


def test_seg_plain_skips_pills():
    assert seg_plain([["a"], ["‣", [["u", USER]]], ["b"]]) == "ab"


# ---- inline markdown -> segments ---------------------------------------------


def test_md_seg_bold():
    assert md_to_segments("say **loud**") == [["say "], ["loud", [["b"]]]]


def test_md_seg_link():
    assert md_to_segments("[doc](https://x.co)") == [["doc", [["a", "https://x.co"]]]]


def test_md_seg_user_mention():
    assert md_to_segments(f"ping @user({USER})") == [["ping "], ["‣", [["u", USER]]]]


def test_md_seg_page_mention_from_url():
    got = md_to_segments(f"see @page(https://www.notion.so/{UUID.replace('-', '')})")
    assert got == [["see "], ["‣", [["p", UUID]]]]


def test_md_seg_roundtrip_plain():
    assert seg_to_md(md_to_segments("plain words")) == "plain words"


# ---- markdown -> v3 blocks -----------------------------------------------------


def test_v3_heading_types():
    assert [b["type"] for b in md_to_v3_blocks("# a\n## b\n### c")] == ["header", "sub_header", "sub_sub_header"]


def test_v3_todo_checked_encoding():
    b = md_to_v3_blocks("- [x] done")[0]
    assert b["properties"]["checked"] == [["Yes"]]


def test_v3_todo_unchecked_encoding():
    assert md_to_v3_blocks("- [ ] later")[0]["properties"]["checked"] == [["No"]]


def test_v3_nested_bullet_becomes_child():
    blocks = md_to_v3_blocks("- parent\n  - child")
    assert blocks[0]["children"][0]["type"] == "bulleted_list"


def test_v3_callout_icon_and_color():
    b = md_to_v3_blocks("> [!💸:blue_bg] TLDR")[0]
    assert (b["format"]["page_icon"], b["format"]["block_color"]) == ("💸", "blue_background")


def test_v3_plain_quote():
    assert md_to_v3_blocks("> just a quote")[0]["type"] == "quote"


def test_v3_code_language():
    b = md_to_v3_blocks("```python\nx = 1\n```")[0]
    assert b["properties"]["language"] == [["python"]]


def test_v3_code_content():
    b = md_to_v3_blocks("```python\nx = 1\ny = 2\n```")[0]
    assert b["properties"]["title"] == [["x = 1\ny = 2"]]


def test_v3_divider():
    assert md_to_v3_blocks("---")[0]["type"] == "divider"


def test_v3_paragraph_is_text():
    assert md_to_v3_blocks("hello")[0]["type"] == "text"


# ---- GFM tables --------------------------------------------------------------


def test_split_gfm_row_empty_cells():
    assert split_gfm_row("| 2026-08-24 | 50,000 |  | [Slack](https://x.test) |") == [
        "2026-08-24", "50,000", "", "[Slack](https://x.test)",
    ]


def test_parse_gfm_table_header_and_separator():
    t = parse_gfm_table(["| Date | Amount |", "|---|---|", "| 2026-08-25 | 39,000 |"])
    assert t["header"] is True
    assert t["rows"] == [["Date", "Amount"], ["2026-08-25", "39,000"]]


def test_parse_gfm_table_separator_with_spaces():
    t = parse_gfm_table(["| a | b |", "| --- | --- |", "| 1 | 2 |"])
    assert t["header"] is True
    assert t["rows"][1] == ["1", "2"]


def test_v3_table_type_and_header():
    blocks = md_to_v3_blocks("| Date | Amount |\n| --- | --- |\n| 2026-08-25 | 39,000 |")
    assert blocks[0]["type"] == "table"
    assert blocks[0]["format"]["table_block_column_header"] is True
    assert len(blocks[0]["children"]) == 2
    assert blocks[0]["children"][0]["type"] == "table_row"
    assert len(blocks[0]["format"]["table_block_column_order"]) == 2


def test_v3_table_bold_and_link_cells():
    blocks = md_to_v3_blocks("| **Running total** | [Slack](https://x.test) |\n| --- | --- |")
    cols = blocks[0]["format"]["table_block_column_order"]
    row = blocks[0]["children"][0]["properties"]
    assert row[cols[0]] == [["Running total", [["b"]]]]
    assert row[cols[1]] == [["Slack", [["a", "https://x.test"]]]]


def test_v3_table_does_not_swallow_following_paragraph():
    blocks = md_to_v3_blocks("| a | b |\n| --- | --- |\n| 1 | 2 |\n\nhello")
    assert [b["type"] for b in blocks] == ["table", "text"]


def test_apply_table_md_replace_inserts_row():
    rendered = (
        "| Date | Amount | Note | Source |\n"
        "|---|---|---|---|\n"
        "| 2026-08-24 | 50,000 |  | [Slack](https://x.test) |\n"
        "| **Running total** | **2,680,000** | toward ~5M |  |"
    )
    old = (
        "| 2026-08-24 | 50,000 |  | [Slack](https://x.test) |\n"
        "| **Running total** | **2,680,000** | toward ~5M |  |"
    )
    new = (
        "| 2026-08-24 | 50,000 |  | [Slack](https://x.test) |\n"
        "| 2026-08-25 | 39,000 |  | [Slack](https://y.test) |\n"
        "| **Running total** | **2,719,000** | toward ~5M |  |"
    )
    parsed = apply_table_md_replace(rendered, old, new)
    assert parsed["rows"][-2] == ["2026-08-25", "39,000", "", "[Slack](https://y.test)"]
    assert parsed["rows"][-1][1] == "**2,719,000**"


def test_property_text_matches_table_cells_not_just_title():
    blks = {
        "row": {
            "id": "row",
            "type": "table_row",
            "alive": True,
            "properties": {"]NN<": [["2,680,000", [["b"]]]]},
        }
    }
    hits, whole, spanning = property_text_matches(blks, "2,680,000")
    assert whole == []
    assert hits == [("row", "]NN<")]
    assert spanning == []


# ---- property flattening --------------------------------------------------------


def test_flatten_status_text():
    assert flatten_value([["Done"]], "status") == "Done"


def test_flatten_checkbox_yes():
    assert flatten_value([["Yes"]], "checkbox") is True


def test_flatten_checkbox_missing_is_none():
    assert flatten_value(None, "checkbox") is None


def test_flatten_number_int():
    assert flatten_value([["81"]], "number") == 81


def test_flatten_auto_increment_numeric():
    # numeric so `--sort ID` orders 60 < 229 instead of lexicographic "229"<"60"
    assert flatten_value([["275"]], "auto_increment_id") == 275


def test_flatten_date_pill():
    segs = [["‣", [["d", {"type": "date", "start_date": "2026-06-23"}]]]]
    assert flatten_value(segs, "date") == "2026-06-23"


def test_flatten_person_resolves_names():
    segs = [["‣", [["u", USER]]]]
    assert flatten_value(segs, "person", {USER: "Clément"}) == "Clément"


def test_flatten_relation_renders_urls():
    segs = [["‣", [["p", UUID, "sp"]]]]
    assert flatten_value(segs, "relation") == f"https://www.notion.so/{UUID.replace('-', '')}"


# ---- write-side coercion ----------------------------------------------------------


def test_coerce_status_plain_segment():
    assert coerce_segments("Triage", "status") == [["Triage"]]


def test_coerce_date():
    assert coerce_segments("2026-07-31", "date") == [["‣", [["d", {"type": "date", "start_date": "2026-07-31"}]]]]


def test_coerce_date_range():
    got = coerce_segments("2026-07-01..2026-07-03", "date")
    assert got[0][1][0][1] == {"type": "daterange", "start_date": "2026-07-01", "end_date": "2026-07-03"}


def test_coerce_person_from_user_url():
    assert coerce_segments(f"user://{USER}", "person") == [["‣", [["u", USER]]]]


def test_coerce_person_from_cached_name(monkeypatch):
    monkeypatch.setattr(
        notion_cli, "load_id_cache", lambda: {"users": {USER: "Ada Lovelace"}, "pages": {}}
    )
    assert coerce_segments("Ada Lovelace", "person") == [["‣", [["u", USER]]]]
    assert coerce_segments("@Ada Lovelace", "person") == [["‣", [["u", USER]]]]


def test_coerce_person_unknown_name_errors(monkeypatch):
    monkeypatch.setattr(notion_cli, "load_id_cache", lambda: {"users": {}, "pages": {}})
    with pytest.raises(Exception, match="unknown person"):
        coerce_segments("Ada Lovelace", "person")


def test_coerce_relation_from_url():
    got = coerce_segments(f"https://app.notion.com/p/{UUID.replace('-', '')}", "relation")
    assert got == [["‣", [["p", UUID]]]]


def test_coerce_checkbox_yes_forms():
    assert coerce_segments("__YES__", "checkbox") == [["Yes"]]


def test_coerce_empty_clears():
    assert coerce_segments("", "date") == []


def test_coerce_title_keeps_inline_markdown():
    assert coerce_segments("a **b**", "title") == [["a "], ["b", [["b"]]]]


# ---- date parsing -------------------------------------------------------------------


def test_epoch_ms_date_only():
    assert _epoch_ms("2026-07-16") == 1784160000000


def test_epoch_ms_datetime():
    assert _epoch_ms("2026-07-16 12:00") == 1784203200000


def test_epoch_ms_rejects_garbage():
    with pytest.raises(Exception):
        _epoch_ms("last tuesday")


# ---- template block cloning ---------------------------------------------------------


def test_block_to_spec_copies_type_and_props():
    blocks = {"a": {"id": "a", "type": "sub_header", "properties": {"title": [["Why"]]}, "content": []}}
    assert block_to_spec("a", blocks) == {"type": "sub_header", "properties": {"title": [["Why"]]}}


def test_block_to_spec_strips_provenance():
    blocks = {"a": {"id": "a", "type": "callout", "properties": {},
                    "format": {"page_icon": "🎙️", "copied_from_pointer": {"id": "x", "table": "block"}}, "content": []}}
    assert block_to_spec("a", blocks)["format"] == {"page_icon": "🎙️"}


def test_block_to_spec_recurses_children():
    blocks = {
        "p": {"id": "p", "type": "callout", "properties": {}, "content": ["c"]},
        "c": {"id": "c", "type": "text", "properties": {"title": [["hi"]]}, "content": []},
    }
    spec = block_to_spec("p", blocks)
    assert spec["children"][0] == {"type": "text", "properties": {"title": [["hi"]]}}


def test_block_to_spec_skips_dead_blocks():
    blocks = {"a": {"id": "a", "type": "text", "properties": {}, "alive": False, "content": []}}
    assert block_to_spec("a", blocks) is None


# ---- client-side filter DSL ---------------------------------------------------------

KNOWN = {"Status", "ID", "Title", "Due", "Milestone"}
ROW = {"Status": "Done", "ID": "196", "Title": "Earn module", "Due": "2026-06-23", "Milestone": True}


def test_match_status_equals():
    assert make_matcher("Status=Done", KNOWN)(ROW) is True


def test_match_id_numeric_gt():
    assert make_matcher("ID>195", KNOWN)(ROW) is True


def test_match_id_numeric_gt_false():
    assert make_matcher("ID>196", KNOWN)(ROW) is False


def test_match_title_contains_case_insensitive():
    assert make_matcher("Title~EARN", KNOWN)(ROW) is True


def test_match_date_lexicographic():
    assert make_matcher("Due>=2026-06-01", KNOWN)(ROW) is True


def test_match_checkbox_true():
    assert make_matcher("Milestone=true", KNOWN)(ROW) is True


def test_match_is_empty_on_missing():
    assert make_matcher("Due is_empty", KNOWN)({"Due": None}) is True


def test_match_is_not_empty():
    assert make_matcher("Due is_not_empty", KNOWN)(ROW) is True


def test_match_unknown_property_raises():
    with pytest.raises(Exception, match="unknown property"):
        make_matcher("Nope=1", KNOWN)


# ---- id → name/title cache -------------------------------------------------


def test_merge_id_cache_adds_new_entry():
    cache = {"users": {}, "pages": {}}
    changed = merge_id_cache(cache, "users", {USER: "Ada"})
    assert cache["users"] == {USER: "Ada"}


def test_merge_id_cache_reports_change_on_new_entry():
    cache = {"users": {}, "pages": {}}
    assert merge_id_cache(cache, "users", {USER: "Ada"}) is True


def test_merge_id_cache_reports_no_change_when_identical():
    cache = {"users": {USER: "Ada"}, "pages": {}}
    assert merge_id_cache(cache, "users", {USER: "Ada"}) is False


def test_merge_id_cache_updates_changed_name():
    cache = {"users": {USER: "Ada"}, "pages": {}}
    merge_id_cache(cache, "users", {USER: "Ada Lovelace"})
    assert cache["users"][USER] == "Ada Lovelace"


def test_merge_id_cache_skips_empty_names():
    cache = {"users": {}, "pages": {}}
    changed = merge_id_cache(cache, "users", {USER: ""})
    assert cache["users"] == {} and changed is False


def test_load_id_cache_defaults_to_empty_shape_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_cli, "ID_NAMES_PATH", tmp_path / "missing" / "id_names.json")
    assert load_id_cache() == {"users": {}, "pages": {}}


def test_save_and_load_id_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_cli, "ID_NAMES_PATH", tmp_path / "id_names.json")
    save_id_cache({"users": {USER: "Ada"}, "pages": {UUID: "Vault Launch Plan"}})
    assert load_id_cache() == {"users": {USER: "Ada"}, "pages": {UUID: "Vault Launch Plan"}}


def test_load_id_cache_recovers_from_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "id_names.json"
    path.write_text("not json")
    monkeypatch.setattr(notion_cli, "ID_NAMES_PATH", path)
    assert load_id_cache() == {"users": {}, "pages": {}}


def test_unique_names_drops_collisions():
    assert unique_names({USER: "Ada", UUID: "Ada", "aa": "Bob"}) == {"bob": "aa"}


def test_rewrite_named_mentions_longest_wins():
    users = {"ada": USER, "ada lovelace": USER}
    assert rewrite_named_mentions("hi @Ada Lovelace", users) == f"hi @user({USER})"


def test_rewrite_named_mentions_leaves_explicit_user():
    users = {"ada": USER}
    assert rewrite_named_mentions(f"@user({USER})", users) == f"@user({USER})"


def test_md_to_segments_named_mention(monkeypatch):
    monkeypatch.setattr(
        notion_cli, "load_id_cache", lambda: {"users": {USER: "Ada Lovelace"}, "pages": {}}
    )
    assert md_to_segments("ping @Ada Lovelace") == [["ping "], ["‣", [["u", USER]]]]


def test_seg_to_md_writeable_roundtrip():
    segs = [["‣", [["u", USER]]], [" and "], ["‣", [["p", UUID]]]]
    md = seg_to_md(segs, writeable=True)
    assert md == f"@user({USER}) and @page({UUID})"


def test_property_text_matches_rendered_mention_as_whole():
    blks = {
        "b1": {
            "alive": True,
            "properties": {"title": [["‣", [["u", USER]]]]},
        }
    }
    hits, whole, spanning = property_text_matches(blks, "@Ada", names={USER: "Ada"})
    assert hits == []
    assert whole == [("b1", "title")]
    assert spanning == []


def test_find_section_stops_at_same_level():
    blocks = {
        "a": {"type": "header", "alive": True, "properties": {"title": [["1. What"]]}},
        "b": {"type": "text", "alive": True, "properties": {"title": [["old"]]}},
        "c": {"type": "header", "alive": True, "properties": {"title": [["2. Crew"]]}},
    }
    hid, parent, kids = find_section(["a", "b", "c"], blocks, "1. What")
    assert hid == "a"
    assert parent == "page"
    assert kids == ["b"]


def test_find_section_nested_heading_uses_parent():
    blocks = {
        "links": {"type": "sub_header", "alive": True, "properties": {"title": [["4. Links"]]}, "content": ["lin", "b1", "gh"]},
        "lin": {"type": "sub_sub_header", "alive": True, "properties": {"title": [["Linear"]]}},
        "b1": {"type": "bulleted_list", "alive": True, "properties": {"title": [["old"]]}},
        "gh": {"type": "sub_sub_header", "alive": True, "properties": {"title": [["GitHub"]]}},
    }
    hid, parent, kids = find_section(["links"], blocks, "Linear")
    assert hid == "lin"
    assert parent == "links"
    assert kids == ["b1"]


def test_refuse_hard_delete_path():
    with pytest.raises(Exception, match="hard delete"):
        refuse_hard_delete("deleteBlocks", {})


def test_refuse_hard_delete_body():
    with pytest.raises(Exception, match="hard delete"):
        refuse_hard_delete("saveTransactionsFanout", {"permanentlyDelete": True})


def test_refuse_hard_delete_nested():
    with pytest.raises(Exception, match="hard delete"):
        refuse_hard_delete(
            "saveTransactionsFanout",
            {"transactions": [{"operations": [{"permanently_deleted_time": 1}]}]},
        )


def test_refuse_hard_delete_allows_trash():
    refuse_hard_delete("saveTransactionsFanout", {"operations": [{"alive": False}]})


# --------------------------------------------------------------------------
# rendered-body cache
# --------------------------------------------------------------------------


@pytest.fixture
def body_cache(tmp_path, monkeypatch):
    """An isolated, empty body cache for one test."""
    monkeypatch.delenv("NOTION_CLI_NO_CACHE", raising=False)
    monkeypatch.setattr(notion_cli, "BODY_CACHE_PATH", tmp_path / "bodies.sqlite3")
    monkeypatch.setattr(notion_cli, "_body_db", None)
    return notion_cli


SETTLED = 1_700_000_000_000  # a last_edited_time far outside the settle window


def test_cached_body_is_empty_before_anything_is_stored(body_cache):
    assert body_cache.cached_body("pid", 6, False, SETTLED) is None


def test_stored_body_is_returned_for_the_same_revision(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    assert body_cache.cached_body("pid", 6, False, SETTLED) == "# hello"


def test_stored_body_is_not_returned_for_a_newer_revision(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    assert body_cache.cached_body("pid", 6, False, SETTLED + 1000) is None


def test_stored_body_is_not_returned_for_a_different_depth(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    assert body_cache.cached_body("pid", 3, False, SETTLED) is None


def test_stored_body_is_not_returned_for_a_different_writeable_flag(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    assert body_cache.cached_body("pid", 6, True, SETTLED) is None


def test_cached_body_without_a_revision_stamp_is_never_served(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    assert body_cache.cached_body("pid", 6, False, None) is None


def test_an_actively_edited_page_is_not_cached(body_cache):
    now_ms = int(time.time() * 1000)
    body_cache.store_body("pid", 6, False, now_ms, "# mid-edit")
    assert body_cache.cached_body("pid", 6, False, now_ms) is None


def test_a_body_older_than_the_max_age_backstop_is_not_served(body_cache, monkeypatch):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    monkeypatch.setattr(notion_cli, "BODY_CACHE_MAX_AGE_S", -1)
    assert body_cache.cached_body("pid", 6, False, SETTLED) is None


def test_invalidate_drops_the_entry(body_cache):
    body_cache.store_body("pid", 6, False, SETTLED, "# hello")
    body_cache.invalidate_bodies(["pid"])
    assert body_cache.cached_body("pid", 6, False, SETTLED) is None


def test_invalidate_leaves_other_pages_alone(body_cache):
    body_cache.store_body("keep", 6, False, SETTLED, "# keep")
    body_cache.invalidate_bodies(["drop"])
    assert body_cache.cached_body("keep", 6, False, SETTLED) == "# keep"


def test_cache_is_disabled_by_the_env_var(body_cache, monkeypatch):
    monkeypatch.setenv("NOTION_CLI_NO_CACHE", "1")
    monkeypatch.setattr(notion_cli, "_body_db", None)
    assert body_cache.body_cache_db() is None


def test_an_unwritable_cache_path_degrades_to_no_cache(body_cache, monkeypatch, tmp_path):
    # a file where the cache directory should be — mkdir must fail
    blocker = tmp_path / "blocked"
    blocker.write_text("")
    monkeypatch.setattr(notion_cli, "BODY_CACHE_PATH", blocker / "bodies.sqlite3")
    assert body_cache.body_cache_db() is None


def test_a_write_invalidates_the_touched_blocks_cached_body(body_cache, monkeypatch):
    body_cache.store_body("blk-1", 6, False, SETTLED, "# stale")
    api = notion_cli.Api.__new__(notion_cli.Api)  # no auth needed; post is stubbed
    api.space_id = "space"
    monkeypatch.setattr(notion_cli.Api, "post", lambda self, path, body, **kw: {})
    api.transact([notion_cli.op("block", "blk-1", ["properties"], "set", ["x"], "space")])
    assert body_cache.cached_body("blk-1", 6, False, SETTLED) is None


def test_invalidating_a_nested_block_drops_its_containing_pages_body(body_cache, monkeypatch):
    # to_do nested in a toggle nested in the page — only the page has a cached body
    parents = {
        "todo": {"id": "todo", "parent_table": "block", "parent_id": "toggle"},
        "toggle": {"id": "toggle", "parent_table": "block", "parent_id": "page"},
        "page": {"id": "page", "parent_table": "collection", "parent_id": "coll"},
    }
    body_cache.store_body("page", 6, False, SETTLED, "# stale")
    api = notion_cli.Api.__new__(notion_cli.Api)
    monkeypatch.setattr(notion_cli.Api, "records", lambda self, table, ids: {i: parents[i] for i in ids})
    notion_cli.invalidate_block_ancestry(api, "todo", parents["todo"])
    assert body_cache.cached_body("page", 6, False, SETTLED) is None


def test_ancestry_invalidation_stops_at_the_page_boundary(body_cache, monkeypatch):
    parents = {"page": {"id": "page", "parent_table": "collection", "parent_id": "coll"}}
    body_cache.store_body("coll", 6, False, SETTLED, "# the database, not the page")
    api = notion_cli.Api.__new__(notion_cli.Api)
    monkeypatch.setattr(notion_cli.Api, "records", lambda self, table, ids: {i: parents[i] for i in ids})
    notion_cli.invalidate_block_ancestry(api, "page", parents["page"])
    assert body_cache.cached_body("coll", 6, False, SETTLED) == "# the database, not the page"

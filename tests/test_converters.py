"""Unit tests for notion_cli's pure conversion layers (v3 record model).

Covers the parts that do the token-saving work and are easy to get subtly
wrong: id parsing, segment rendering both ways, markdown→v3-block parsing,
property flattening/coercion by schema type, and the client-side filter DSL.
No network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import notion_cli  # noqa: E402
from notion_cli import (  # noqa: E402
    _epoch_ms,
    block_to_spec,
    coerce_segments,
    dash,
    flatten_value,
    load_id_cache,
    make_matcher,
    md_to_segments,
    md_to_v3_blocks,
    merge_id_cache,
    parse_id,
    save_id_cache,
    seg_plain,
    seg_to_md,
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

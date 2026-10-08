"""Tests for the cookie-store query `login` uses to find token_v2 candidates."""

import sqlite3

import pytest

from notion_cli import TOKEN_COOKIE_SQL


@pytest.fixture
def cookie_store():
    """In-memory Chromium-shaped cookie table with Notion and unrelated rows."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, last_access_utc INTEGER)")
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?)",
        [
            (".www.notion.so", "token_v2", b"legacy", 100),
            (".app.notion.com", "token_v2", b"sso", 200),
            (".app.notion.com", "notion_user_id", b"uid", 300),
            (".example.com", "token_v2", b"other", 400),
        ],
    )
    return conn


def test_reads_tokens_from_both_notion_hosts_freshest_first(cookie_store):
    assert [v for (v,) in cookie_store.execute(TOKEN_COOKIE_SQL)] == [b"sso", b"legacy"]

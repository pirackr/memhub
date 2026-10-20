"""Tests for memhub.search.parse_query: safe FTS5 MATCH expression building."""

import sqlite3

import pytest

from memhub.search import parse_query
from memhub.store import MemhubError, init_store, put


# --- basic tokenization -----------------------------------------------------


def test_bare_words_are_quoted_and_anded():
    assert parse_query("hello world") == '"hello" AND "world"'


def test_trailing_star_becomes_prefix():
    # NOTE: the '*' goes OUTSIDE the quotes -- that is FTS5's prefix syntax.
    # '"cat*"' (star inside) is a plain phrase and does NOT prefix-match.
    assert parse_query("cat*") == '"cat"*'


def test_hyphen_is_safe_inside_quotes():
    assert parse_query("pre-fix") == '"pre-fix"'


def test_quoted_phrase_and_trailing_word():
    assert parse_query('"exact phrase" tail') == '"exact phrase" AND "tail"'


def test_quoted_word_mid_query():
    assert parse_query('say "hi"') == '"say" AND "hi"'


# --- errors ------------------------------------------------------------------


def test_unclosed_quote_raises():
    with pytest.raises(MemhubError, match="unbalanced quote in query"):
        parse_query('quote " inside')


def test_operators_and_punctuation_only_raises_no_term():
    with pytest.raises(MemhubError, match="search requires at least one term"):
        parse_query("( ) ^ + : *")


def test_empty_and_whitespace_raise_no_term():
    with pytest.raises(MemhubError, match="search requires at least one term"):
        parse_query("")
    with pytest.raises(MemhubError, match="search requires at least one term"):
        parse_query("   ")


def test_empty_phrase_dropped_raises_no_term():
    with pytest.raises(MemhubError, match="search requires at least one term"):
        parse_query('""')


# --- round-trip safety against a real v2 store -------------------------------


@pytest.fixture()
def v2_conn(tmp_path):
    db = tmp_path / "s.sqlite"
    init_store(db)
    conn = sqlite3.connect(db)
    yield conn
    conn.close()


@pytest.mark.parametrize(
    "nasty",
    ['"; DROP', 'a"b', '"unclosed', "AND OR", "NEAR/a/b"],
)
def test_parse_query_never_produces_fts5_syntax_error(v2_conn, nasty):
    try:
        expr = parse_query(nasty)
    except MemhubError:
        return
    v2_conn.execute(
        "SELECT 1 FROM memories_fts WHERE memories_fts MATCH ?", (expr,)
    ).fetchall()


def test_prefix_expression_actually_prefix_matches(v2_conn):
    # Guards the '"cat"*' (star outside quotes) form: it must prefix-match.
    put(v2_conn, "/notes.txt", b"cats and dogs", content_type="text/plain")
    v2_conn.commit()
    expr = parse_query("cat*")
    rows = v2_conn.execute(
        "SELECT path FROM memories_fts WHERE memories_fts MATCH ?", (expr,)
    ).fetchall()
    assert [r[0] for r in rows] == ["/notes.txt"]

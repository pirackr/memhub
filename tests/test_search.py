"""Tests for memhub.search: query parsing and ranked search."""

import sqlite3

import pytest

from memhub.search import Hit, SearchResult, parse_bound, parse_query, search
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


# --- search(): ranking, filters, snippets ------------------------------------


def _seed(conn, path, content, *, content_type="text/plain"):
    put(conn, path, content.encode("utf-8") if isinstance(content, str) else content,
        content_type=content_type)
    conn.commit()


def _set_updated(conn, path, ts):
    conn.execute("UPDATE memories SET updated_at = ? WHERE path = ?", (ts, path))
    conn.commit()


# --- ranking -----------------------------------------------------------------


def test_path_match_outranks_content_match(v2_conn):
    _seed(v2_conn, "/notes.txt", "widget report")
    _seed(v2_conn, "/widget/notes.txt", "gadget")
    result = search(v2_conn, "widget")
    assert result.total == 2
    paths = [h.path for h in result.hits]
    assert paths == ["/widget/notes.txt", "/notes.txt"]
    assert result.hits[0].score > result.hits[1].score


def test_and_semantics(v2_conn):
    _seed(v2_conn, "/both.txt", "alpha beta")
    _seed(v2_conn, "/only-alpha.txt", "alpha")
    _seed(v2_conn, "/only-beta.txt", "beta")
    result = search(v2_conn, "alpha beta")
    assert [h.path for h in result.hits] == ["/both.txt"]
    assert result.total == 1


# --- prefix / phrase ---------------------------------------------------------


def test_prefix_matches_token_prefix(v2_conn):
    _seed(v2_conn, "/a.txt", "catalog of things")
    assert search(v2_conn, "cat*").total == 1
    assert search(v2_conn, "cat").total == 0


def test_phrase_requires_adjacency(v2_conn):
    _seed(v2_conn, "/x.txt", "the red quick fox jumped")
    assert search(v2_conn, '"red fox"').total == 0
    _seed(v2_conn, "/y.txt", "a red fox appeared")
    assert search(v2_conn, '"red fox"').total == 1


# --- binary / path-only hits -------------------------------------------------


def test_binary_row_is_path_only(v2_conn):
    _seed(v2_conn, "/images/photo.jpg", b"\x00\x01\x02binary",
          content_type="image/jpeg")
    result = search(v2_conn, "photo")
    assert result.total == 1
    hit = result.hits[0]
    assert hit.match == "path"
    assert hit.snippet is None
    assert hit.path == "/images/photo.jpg"


# --- snippets ----------------------------------------------------------------


def test_snippet_capped_at_200_preserving_term(v2_conn):
    long = "supercalifragilisticexpialidocious"  # 34-char tokens force snippet > 200
    needle = "ZEBRAFISH"
    content = " ".join([long] * 8) + f" {needle} " + " ".join([long] * 8)
    _seed(v2_conn, "/long.txt", content)
    result = search(v2_conn, "zebrafish")
    assert result.total == 1
    snippet = result.hits[0].snippet
    assert snippet is not None
    assert len(snippet) <= 200
    assert "ZEBRAFISH" in snippet


def test_snippet_short_doc_is_content(v2_conn):
    _seed(v2_conn, "/s.txt", "the quick brown fox")
    result = search(v2_conn, "quick")
    hit = result.hits[0]
    assert hit.match == "content"
    assert "quick" in hit.snippet


# --- total / pagination ------------------------------------------------------


def test_total_counts_all_and_pagination(v2_conn):
    for i in range(5):
        _seed(v2_conn, f"/doc{i}.txt", "shared keyword here")
    result = search(v2_conn, "shared", limit=2)
    assert result.total == 5
    assert len(result.hits) == 2
    page2 = search(v2_conn, "shared", limit=2, offset=2)
    assert page2.total == 5
    assert len(page2.hits) == 2
    assert {h.path for h in result.hits}.isdisjoint({h.path for h in page2.hits})


def test_offset_beyond_total(v2_conn):
    for i in range(3):
        _seed(v2_conn, f"/d{i}.txt", "match me")
    result = search(v2_conn, "match", offset=10)
    assert result.hits == []
    assert result.total == 3


# --- date filters ------------------------------------------------------------


def test_since_filter(v2_conn):
    _seed(v2_conn, "/old.txt", "keepme text")
    _set_updated(v2_conn, "/old.txt", "2020-01-01T00:00:00Z")
    _seed(v2_conn, "/new.txt", "keepme text")
    _set_updated(v2_conn, "/new.txt", "2026-06-01T00:00:00Z")
    result = search(v2_conn, "keepme", since="2026-01-01")
    assert [h.path for h in result.hits] == ["/new.txt"]


def test_until_filter_includes_whole_day(v2_conn):
    _seed(v2_conn, "/a.txt", "keepme text")
    _set_updated(v2_conn, "/a.txt", "2026-03-01T23:59:59Z")
    _seed(v2_conn, "/b.txt", "keepme text")
    _set_updated(v2_conn, "/b.txt", "2026-03-02T00:00:00Z")
    result = search(v2_conn, "keepme", until="2026-03-01")
    assert [h.path for h in result.hits] == ["/a.txt"]


def test_combined_filters(v2_conn):
    _seed(v2_conn, "/a/keep.txt", "needle text")
    _set_updated(v2_conn, "/a/keep.txt", "2026-05-01T00:00:00Z")
    _seed(v2_conn, "/a/early.txt", "needle text")
    _set_updated(v2_conn, "/a/early.txt", "2020-01-01T00:00:00Z")
    _seed(v2_conn, "/b/other.txt", "needle text")
    _set_updated(v2_conn, "/b/other.txt", "2026-05-01T00:00:00Z")
    result = search(v2_conn, "needle", since="2026-01-01", path="/a")
    assert [h.path for h in result.hits] == ["/a/keep.txt"]


# --- path prefix -------------------------------------------------------------


def test_path_prefix_is_component_aware(v2_conn):
    _seed(v2_conn, "/a/b.txt", "findme text")
    _seed(v2_conn, "/ab.txt", "findme text")
    result = search(v2_conn, "findme", path="/a")
    assert [h.path for h in result.hits] == ["/a/b.txt"]


# --- v1 store guard ----------------------------------------------------------


def test_v1_store_requires_reindex(tmp_path):
    db = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memories (id TEXT PRIMARY KEY, path TEXT UNIQUE, "
        "content TEXT, blob BLOB, content_type TEXT, size INTEGER, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    with pytest.raises(MemhubError, match="store is not indexed; run memhub reindex"):
        search(conn, "anything")
    conn.close()


# --- parse_bound -------------------------------------------------------------


def test_parse_bound_date_only_since():
    assert parse_bound("2026-10-18", "since") == "2026-10-18T00:00:00Z"


def test_parse_bound_date_only_until_is_next_day():
    assert parse_bound("2026-10-18", "until") == "2026-10-19T00:00:00Z"


def test_parse_bound_offset_normalized_to_utc():
    assert parse_bound("2026-10-18T10:30:00+02:00", "since") == "2026-10-18T08:30:00Z"


def test_parse_bound_z_passthrough():
    assert parse_bound("2026-10-18T10:30:00Z", "until") == "2026-10-18T10:30:00Z"


def test_parse_bound_requires_timezone():
    with pytest.raises(MemhubError, match="since"):
        parse_bound("2026-10-18T10:30:00", "since")


def test_parse_bound_invalid_format():
    with pytest.raises(MemhubError):
        parse_bound("not-a-date", "since")


def test_reversed_range_error(v2_conn):
    with pytest.raises(MemhubError, match="since must not be after"):
        search(v2_conn, "x", since="2026-05-01", until="2026-01-01")


# --- limit / offset validation ----------------------------------------------


def test_limit_out_of_range(v2_conn):
    with pytest.raises(MemhubError, match="limit must be between 1 and 50"):
        search(v2_conn, "x", limit=0)
    with pytest.raises(MemhubError, match="limit must be between 1 and 50"):
        search(v2_conn, "x", limit=51)


def test_negative_offset(v2_conn):
    with pytest.raises(MemhubError, match="offset must not be negative"):
        search(v2_conn, "x", offset=-1)


def test_operational_error_from_match_wrapped(v2_conn, monkeypatch):
    # parse_query normally prevents FTS5 syntax errors; if a bad expression ever
    # reaches the matcher, the raw sqlite error must not leak.
    monkeypatch.setattr("memhub.search.parse_query", lambda q: "AND OR ((")
    with pytest.raises(MemhubError, match="invalid search query"):
        search(v2_conn, "whatever")

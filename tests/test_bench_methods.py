"""Tests for bench.methods — memhub AND/keyword/OR retrieval methods."""

import pytest

from bench import core
from bench import methods


DOCS = [
    core.Doc(id="d1", text="alpha beta notes"),
    core.Doc(id="d2", text="alpha only here"),
    core.Doc(id="d3", text="beta alone"),
    core.Doc(id="d4", text="what is the time"),
    core.Doc(id="d5", text="catalog item"),
]


@pytest.fixture
def make():
    """Factory: create a registered method by name and ingest the 5-doc corpus."""
    built = []

    def _make(name: str) -> core.Method:
        m = core.create_method(name)
        m.ingest(DOCS)
        built.append(m)
        return m

    yield _make
    for m in built:
        m.close()


def test_and_ranks_both_words_first_and_excludes_alpha_only(make):
    m = make("memhub-and")
    result = m.search("alpha beta", 10)
    assert result[0] == "d1"
    assert "d2" not in result


def test_or_returns_union(make):
    m = make("memhub-or")
    result = m.search("alpha beta", 10)
    assert {"d1", "d2", "d3"} <= set(result)


def test_kw_stopword_query_falls_back_without_error(make):
    m = make("memhub-kw")
    result = m.search("what is the time", 10)  # no error
    assert "d4" in result


def test_and_unbalanced_quote_falls_back(make):
    m = make("memhub-and")
    result = m.search('"alpha', 10)  # must not raise
    assert "d1" in result


def test_or_matches_catalog(make):
    m = make("memhub-or")
    result = m.search("catalog", 10)
    assert result == ["d5"]


def test_search_depth_validated(make):
    m = make("memhub-and")
    with pytest.raises(core.BenchError, match="search depth k must be between 1 and 50"):
        m.search("alpha", 51)
    with pytest.raises(core.BenchError, match="search depth k must be between 1 and 50"):
        m.search("alpha", 0)


def test_close_removes_temp_dir(make):
    m = make("memhub-and")
    tmpdir = m._tmpdir
    assert tmpdir.is_dir()
    m.close()
    assert not tmpdir.exists()

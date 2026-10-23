"""memhub retrieval methods for the benchmark: AND, keyword, and OR variants."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Sequence

from memhub import search as msearch
from memhub import store
from memhub.store import MemhubError

from bench.core import BenchError, Doc, Method, register_method

# Ported verbatim from memhub-bench/bench_memory_datasets.py.
WORD_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = set("""a an and are as at be but by can could did do does for
from had has have how i if in is it its me my not of on or our so than that
the their them then there these they this to was we were what when where
which who why will with would you your us about after all also just like
more no now only other out some such than there up very""".split())


def _content_tokens(query: str) -> list[str]:
    """Deduped WORD_RE tokens minus STOPWORDS; if none survive, fall back to
    all deduped raw tokens, or ["x"] for a token-less query."""
    tokens = [t for t in dict.fromkeys(WORD_RE.findall(query.lower()))
              if t not in STOPWORDS]
    if not tokens:
        tokens = list(dict.fromkeys(WORD_RE.findall(query.lower()))) or ["x"]
    return tokens


class MemhubMethod(Method):
    """Base: a throwaway memhub store ingested from the benchmark corpus."""

    def __init__(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp())
        db = self._tmpdir / "memhub.db"
        store.init_store(db)
        self._conn = sqlite3.connect(db)
        self._ids: dict[str, str] = {}  # path -> doc.id

    def ingest(self, docs: Sequence[Doc]) -> None:
        for doc in docs:
            path = doc.path or f"/docs/{doc.id}"
            store.put(self._conn, path, doc.text.encode(), "text/markdown")
            # store.put normalizes the path; key the map with the same form
            # search returns, or hits for non-normalized paths drop silently.
            self._ids[store.normalize_path(path)] = doc.id
        self._conn.commit()
        store.reindex(self._conn)

    def search(self, query: str, k: int) -> list[str]:
        if (not isinstance(k, int) or isinstance(k, bool)
                or not 1 <= k <= 50):
            raise BenchError("search depth k must be between 1 and 50")
        return [self._ids[p] for p in self._search(query, k) if p in self._ids]

    def _search(self, query: str, k: int) -> list[str]:
        """Return up to k ranked paths for the query."""
        raise NotImplementedError

    def close(self) -> None:
        self._conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


@register_method("memhub-and")
class MemhubAnd(MemhubMethod):
    """Pass the query through memhub's parse_query (all terms ANDed)."""

    name = "memhub-and"

    def _search(self, query: str, k: int) -> list[str]:
        try:
            # Validate the query with memhub's parser; search() re-parses
            # internally and parse_query is NOT idempotent (a parsed "AND"
            # operator would be re-quoted as a term), so pass the raw query.
            msearch.parse_query(query)
        except MemhubError:
            # e.g. unbalanced quote in the dataset's query text
            query = " ".join(WORD_RE.findall(query.lower())) or "x"
        return [h.path for h in msearch.search(self._conn, query, limit=k).hits]


@register_method("memhub-kw")
class MemhubKw(MemhubMethod):
    """Stopword-stripped content words, ANDed via memhub's search API."""

    name = "memhub-kw"

    def _search(self, query: str, k: int) -> list[str]:
        q = " ".join(_content_tokens(query))
        return [h.path for h in msearch.search(self._conn, q, limit=k).hits]


@register_method("memhub-or")
class MemhubOr(MemhubMethod):
    """OR over content words, run as raw FTS5 SQL (memhub's public API
    cannot express OR; this is the reference point)."""

    name = "memhub-or"

    def _search(self, query: str, k: int) -> list[str]:
        tokens = _content_tokens(query)
        match = " OR ".join(f'"{t}"' for t in tokens)
        rows = self._conn.execute(
            "SELECT m.path, -bm25(memories_fts, 10.0, 1.0) s FROM memories_fts "
            "JOIN memories m ON m.rowid = memories_fts.rowid "
            "WHERE memories_fts MATCH ? ORDER BY s DESC LIMIT ?",
            (match, k),
        ).fetchall()
        return [r[0] for r in rows]

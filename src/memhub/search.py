"""Search over memhub memories backed by the FTS5 index.

parse_query converts raw user input into a safe FTS5 MATCH expression:

- Input is tokenized into bare words and "quoted phrases" (double quotes).
  An unclosed quote raises MemhubError('unbalanced quote in query').
- A bare word may carry a single trailing '*' marking a prefix search; the
  word is emitted as "escaped-word" + '*' when prefixed, else "escaped-word"
  (the '*' sits outside the quotes, which is FTS5's prefix syntax). Words that
  are empty after stripping the '*', or that contain no alphanumeric
  characters (pure punctuation/operators such as '(' or ':'), are dropped.
- A phrase is emitted as "escaped-phrase".
- Escaping wraps a term in double quotes, doubling any '"' inside, so no raw
  FTS5 operator (AND, OR, NOT, NEAR, ^, +, :, parentheses) can reach the
  matcher as syntax.
- Terms are joined with ' AND '.
- If no terms remain after parsing, MemhubError('search requires at least
  one term') is raised.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .store import MemhubError, normalize_path, schema_version


class Hit(NamedTuple):
    """One ranked search result."""

    path: str
    id: str
    score: float          # higher is better
    updated_at: str
    snippet: str | None   # None for path-only (binary) hits
    match: str            # "content" | "path"


class SearchResult(NamedTuple):
    """A page of hits plus the total match count (before limit/offset)."""

    hits: list[Hit]
    total: int


def _escape(term: str) -> str:
    """Wrap a term in double quotes, doubling any embedded double quotes."""
    return '"' + term.replace('"', '""') + '"'


def parse_query(text: str) -> str:
    """Parse user search text into a safe FTS5 MATCH expression.

    Returns the expression as a string; raises MemhubError on an unbalanced
    quote or when the text contains no searchable terms.
    """
    terms: list[str] = []
    buf: list[str] = []
    in_quote = False

    def flush_word() -> None:
        word = "".join(buf)
        buf.clear()
        if not word:
            return
        prefix = word.endswith("*")
        if prefix:
            word = word[:-1]
        if not word or not any(ch.isalnum() for ch in word):
            return
        terms.append(_escape(word) + ("*" if prefix else ""))

    def flush_phrase() -> None:
        phrase = "".join(buf)
        buf.clear()
        if phrase:
            terms.append(_escape(phrase))

    for ch in text:
        if ch == '"':
            if in_quote:
                flush_phrase()
            else:
                flush_word()
            in_quote = not in_quote
        elif ch.isspace() and not in_quote:
            flush_word()
        else:
            buf.append(ch)

    if in_quote:
        raise MemhubError("unbalanced quote in query")
    flush_word()

    if not terms:
        raise MemhubError("search requires at least one term")
    return " AND ".join(terms)


# --- ranked search -----------------------------------------------------------

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TZ_SUFFIX = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def parse_bound(value: str, kind: str) -> str:
    """Normalize a --since/--until bound to UTC second precision '...Z'.

    kind is "since" or "until". A date-only 'YYYY-MM-DD' becomes the start of
    that UTC day for 'since', or the start of the NEXT day for 'until' (an
    exclusive upper bound). A full timestamp must carry a timezone ('Z' or
    '±hh:mm') and is converted to UTC; search then treats it as an INCLUSIVE
    bound -- the second it names is part of the range. Anything else raises
    MemhubError naming the flag.
    """
    flag = f"--{kind}"
    if _DATE_ONLY.match(value):
        try:
            day = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise MemhubError(f"invalid {flag} date: {value!r}") from exc
        if kind == "until":
            day += timedelta(days=1)
        return day.strftime("%Y-%m-%dT00:00:00Z")
    if not _TZ_SUFFIX.search(value):
        raise MemhubError(
            f"{flag} timestamp must include a timezone (Z or ±hh:mm): {value!r}"
        )
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MemhubError(f"invalid {flag} timestamp: {value!r}") from exc
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# sqlite reports a bad MATCH expression as 'fts5: syntax error near ...' (an
# unbalanced literal as 'unterminated string'). Every other OperationalError --
# a missing memories_fts, a locked database -- is an operational problem and
# must reach the caller as the sqlite error main() renders.
_FTS_QUERY_MARKERS = ("fts5:", "unterminated string")


def _snippet_terms(query: str) -> list[str]:
    """Lowercased words from raw query text, used to locate the match in a
    snippet for the 200-char truncation. Quotes and trailing '*' are dropped."""
    out: list[str] = []
    for tok in re.findall(r'[^"\s]+', query):
        tok = tok.rstrip("*")
        if tok and any(c.isalnum() for c in tok):
            out.append(tok.lower())
    return out


def _truncate_snippet(snip: str, terms: list[str], cap: int = 200) -> str:
    """Hard-cap a snippet to `cap` chars, keeping the first matched term
    (case-insensitive) fully inside the returned window."""
    if len(snip) <= cap:
        return snip
    low = snip.lower()
    best: tuple[int, str] | None = None
    for t in terms:
        i = low.find(t)
        if i != -1 and (best is None or i < best[0]):
            best = (i, t)
    if best is None:
        return snip[:cap]
    pos, term = best
    start = max(0, min(pos, len(snip) - cap))
    end = start + cap
    if pos + len(term) > end:
        end = pos + len(term)
        start = max(0, end - cap)
    return snip[start:end]


def search(
    conn: sqlite3.Connection,
    query: str,
    since: str | None = None,
    until: str | None = None,
    path: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> SearchResult:
    """Run a ranked FTS5 search with optional date/path filters.

    Returns a SearchResult of at most `limit` hits (starting at `offset`) plus
    the total match count. `since`/`until` are raw user bounds (see
    parse_bound): both ends are inclusive, a path-only match reports
    `match="path"` with `snippet=None`. Raises MemhubError on an unindexed (v1)
    store, an invalid query, out-of-range limit/offset, or a reversed date
    range; a store whose index is missing or a locked database raises the
    underlying sqlite3 error.
    """
    if schema_version(conn) < 2:
        raise MemhubError("store is not indexed; run memhub reindex")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise MemhubError("limit must be between 1 and 50")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise MemhubError("offset must not be negative")

    since_b = parse_bound(since, "since") if since is not None else None
    until_b = parse_bound(until, "until") if until is not None else None
    if since_b is not None and until_b is not None and since_b > until_b:
        raise MemhubError("--since must not be after --until")
    if until_b is not None and not _DATE_ONLY.match(until):
        # A full-timestamp --until names a second that belongs to the range.
        # updated_at is stored at second precision, so '< bound + 1s' is exactly
        # '<= bound'. (A date-only --until is already the exclusive next
        # midnight -- the whole-day rule parse_bound documents.)
        until_b = (
            datetime.strptime(until_b, "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    match_expr = parse_query(query)

    # NOTE: FTS5 auxiliary functions (bm25/snippet) cannot appear in the same
    # SELECT as a window function in this SQLite build ("unable to use function
    # bm25 in the requested context"), so bm25/snippet are computed in an inner
    # query and the window count is applied outside. Behaviour is identical to
    # the brief's single-level SQL: total = count(*) OVER () over all matches,
    # score = -bm25 (path weight 10.0, content 1.0), ordered score desc then
    # path then id.
    inner = (
        "SELECT m.rowid AS rowid, m.path AS path, m.id AS id, "
        "-bm25(memories_fts, 10.0, 1.0) AS score, "
        "m.updated_at AS updated_at, "
        "snippet(memories_fts, 1, '', '', ' ', 12) AS snip "
        "FROM memories_fts "
        "JOIN memories m ON m.rowid = memories_fts.rowid "
        "WHERE memories_fts MATCH ?"
    )
    inner_params: list[object] = [match_expr]
    if since_b is not None:
        inner += " AND m.updated_at >= ?"
        inner_params.append(since_b)
    if until_b is not None:
        inner += " AND m.updated_at < ?"
        inner_params.append(until_b)
    if path is not None:
        p = normalize_path(path)
        esc = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        inner += " AND (m.path = ? OR m.path LIKE ? ESCAPE '\\')"
        inner_params.append(p)
        inner_params.append(esc + "/%")
    page = (
        f"SELECT s.*, count(*) OVER () AS total FROM ({inner}) s "
        "ORDER BY s.score DESC, s.path, s.id LIMIT ? OFFSET ?"
    )
    # snippet() returns the head of the content column even when only the path
    # matched, so a content hit needs its own verdict: the same expression run
    # against the content column alone (FTS5 column filter). It sits OUTSIDE
    # LIMIT/OFFSET on purpose -- once per returned row, not once per match.
    sql = (
        "SELECT p.path, p.id, p.score, p.updated_at, p.snip, p.total, "
        "EXISTS (SELECT 1 FROM memories_fts probe "
        "WHERE probe.rowid = p.rowid AND probe.content MATCH ?) AS hit_content "
        f"FROM ({page}) p"
    )
    # The probe's '?' sits in the outer SELECT list, i.e. BEFORE every
    # placeholder of the page subquery, so it binds first.
    outer_params = [match_expr, *inner_params, limit, offset]

    try:
        rows = conn.execute(sql, outer_params).fetchall()
    except sqlite3.OperationalError as exc:
        if not any(marker in str(exc) for marker in _FTS_QUERY_MARKERS):
            raise
        raise MemhubError("invalid search query") from exc

    terms = _snippet_terms(query)
    hits: list[Hit] = []
    total = 0
    if rows:
        total = rows[0][5]  # count(*) OVER (), identical on every row
    elif offset > 0:
        # Window count is only present on returned rows; when OFFSET skips
        # every match, re-count the full filtered set so total stays correct.
        try:
            total = conn.execute(
                f"SELECT count(*) FROM ({inner}) s", inner_params
            ).fetchone()[0]
        except sqlite3.OperationalError as exc:
            if not any(marker in str(exc) for marker in _FTS_QUERY_MARKERS):
                raise
            raise MemhubError("invalid search query") from exc
    for path_, id_, score, updated_at, snip, _, hit_content in rows:
        if hit_content:
            match = "content"
            snippet: str | None = _truncate_snippet(snip, terms)
        else:
            match = "path"
            snippet = None
        hits.append(
            Hit(
                path=path_,
                id=id_,
                score=score,
                updated_at=updated_at,
                snippet=snippet,
                match=match,
            )
        )
    return SearchResult(hits=hits, total=total)

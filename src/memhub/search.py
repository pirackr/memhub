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

from .store import MemhubError


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

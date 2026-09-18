"""Canonical virtual path validation and normalization.

Memhub stores a filesystem-like tree of text documents in SQLite. Every path a
caller hands to the storage library is validated and normalized here before it
reaches resolution. The virtual path language is intentionally small:

* Paths are absolute and always use ``/`` regardless of host OS.
* ``/`` names the root; repeated and trailing separators collapse to the
  simplest equivalent.
* ``.`` and ``..`` are not navigation components and are rejected.
* Name components may not contain NUL or ASCII control characters, and may not
  contain invalid Unicode such as lone surrogates.
* Names are case-sensitive and stored exactly as written. The caller must not
  expand ``~``, environment variables, or shell globs; every character that is
  neither a separator nor a rejected control character is an ordinary name
  character, including ones that begin with a dot.

The functions in this module consume and return plain ``str`` values. They never
touch the host filesystem and never decode bytes.
"""

from __future__ import annotations

from .errors import InvalidInput

__all__ = ["normalize_path"]

# Components that would mean navigation rather than a literal name.
_DOT_COMPONENTS = frozenset({".", ".."})


def _assert_valid_name(component: str) -> None:
    """Reject a name holding a NUL/C0 control character or an invalid surrogate."""
    for char in component:
        code = ord(char)
        if code < 0x20:  # NUL and every other C0 control is illegal in a name.
            raise InvalidInput(
                "path_control_character",
                f"path component {component!r} holds control character U+{code:04X}",
            )
        if 0xD800 <= code <= 0xDFFF:  # Lone surrogates are invalid Unicode.
            raise InvalidInput(
                "path_invalid_unicode",
                f"path component {component!r} holds invalid Unicode (surrogate)",
            )


def normalize_path(path: str) -> str:
    """Validate ``path`` and return its canonical form.

    The result is the shortest ``/``-separated absolute path equal to the input
    under separator-collapsing: repeated and trailing ``/`` are removed and the
    root is represented as ``/``. Name components are returned verbatim, so case,
    Unicode composition, and literal glob characters are all preserved.

    Raises :class:`InvalidInput` (exit status 2) for a non-string input, a
    relative path, a ``.`` or ``..`` component, or a component that holds a NUL /
    C0 control character or an invalid Unicode surrogate. The input is not
    otherwise modified; ``~``, environment variables, and shell globs are left
    literal.
    """
    if not isinstance(path, str):
        raise InvalidInput(
            "path_type",
            f"path must be a string, got {type(path).__name__}",
        )
    if not path.startswith("/"):
        raise InvalidInput(
            "path_not_absolute",
            f"virtual path must be absolute: {path!r}",
        )

    components: list[str] = []
    for component in path.split("/"):
        if component == "":
            continue  # Empty component comes from a repeated or trailing separator.
        if component in _DOT_COMPONENTS:
            raise InvalidInput(
                "path_navigation_component",
                f"path component {component!r} is not an allowed name",
            )
        _assert_valid_name(component)
        components.append(component)

    if not components:
        return "/"
    return "/" + "/".join(components)

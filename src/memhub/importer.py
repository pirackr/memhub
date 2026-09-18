"""Atomic import of safely traversed host text sources."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from .documents import write_in_transaction
from .encoding import decode_bytes
from .models import ImportResult, ImportWarning
from .paths import normalize_path
from .sources import iter_sources
from .tree import ensure_directories

__all__ = ["ImportResult", "ImportWarning", "import_source"]


def _join(destination: str, relative: str) -> str:
    if not relative:
        return destination
    suffix = "/".join(PurePosixPath(relative).parts)
    return normalize_path(("" if destination == "/" else destination) + "/" + suffix)


def import_source(vault, source: Path, destination: str,
                  on_warning: Optional[Callable[[ImportWarning], None]] = None) -> ImportResult:
    """Import one file exactly, or merge a directory's contents atomically."""
    destination = normalize_path(destination)
    source = Path(source)
    blocked = {vault.path, Path(str(vault.path) + "-journal"), Path(str(vault.path) + "-wal"), Path(str(vault.path) + "-shm")}
    files = 0
    counts: dict[str, int] = {}
    # Spill warning diagnostics after a small bounded in-memory buffer. They
    # cannot be emitted before commit, and are discarded automatically if the
    # transaction rolls back.
    warnings = tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode='w+t', encoding='utf-8')
    try:
        with vault.transaction(write=True) as connection:
            before = connection.execute("SELECT count(*) FROM entries WHERE kind='directory'").fetchone()[0]
            iterator = iter(iter_sources(source, blocked))
            first = next(iterator)
            if first.kind == "file":
                decoded = decode_bytes(first.data or b"")
                write_in_transaction(connection, destination, decoded.text, if_absent=True)
                files = 1
                counts[decoded.encoding] = 1
                if decoded.confidence < .80:
                    _store_warning(warnings, _warning(first.source_path, decoded.encoding, decoded.confidence))
            else:
                ensure_directories(connection, destination)
                for item in iterator:
                    target = _join(destination, item.relative_path)
                    if item.kind == "directory":
                        ensure_directories(connection, target)
                        continue
                    decoded = decode_bytes(item.data or b"")
                    write_in_transaction(connection, target, decoded.text, if_absent=True)
                    files += 1
                    counts[decoded.encoding] = counts.get(decoded.encoding, 0) + 1
                    if decoded.confidence < .80:
                        _store_warning(warnings, _warning(item.source_path, decoded.encoding, decoded.confidence))
            after = connection.execute("SELECT count(*) FROM entries WHERE kind='directory'").fetchone()[0]
            directories = after - before
        # Diagnostics are replayed only after the transaction committed.
        if on_warning:
            warnings.seek(0)
            for line in warnings:
                value = json.loads(line)
                on_warning(ImportWarning(**value))
        return ImportResult(files=files, directories=directories, encoding_counts=counts)
    finally:
        warnings.close()


def _store_warning(sink, warning: ImportWarning) -> None:
    sink.write(json.dumps({
        'source_path': warning.source_path, 'encoding': warning.encoding,
        'message': warning.message, 'confidence': warning.confidence,
    }, ensure_ascii=False) + '\n')


def _warning(path: str, encoding: str, confidence: float) -> ImportWarning:
    return ImportWarning(path, encoding, f"low-confidence encoding guess: {encoding} ({confidence:.2f})", confidence)

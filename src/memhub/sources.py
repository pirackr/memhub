"""Symlink-safe, incremental traversal of host import sources."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterator, Iterable

from .errors import Unsupported, VaultFailure
from .models import SourceItem

__all__ = ["SourceItem", "iter_sources"]

_REQUIRED = ("O_NOFOLLOW", "O_DIRECTORY")


def _unsupported(message: str) -> Unsupported:
    return Unsupported("unsupported_source", message)


def _blocked(fd: int, path: Path, blocked: set[tuple[int, int]], blocked_paths: set[Path]) -> bool:
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) in blocked:
        return True
    try:
        return path.resolve(strict=False) in blocked_paths
    except OSError:
        return False


def iter_sources(source: Path, blocked_paths: Iterable[Path] = ()) -> Iterator[SourceItem]:
    """Yield source root and descendants without following symbolic links.

    Every yielded file is read from a descriptor opened with ``O_NOFOLLOW`` and
    type-checked after opening. Directories remain open while their children are
    enumerated, preventing replacement of an ancestor from redirecting reads.
    """
    if any(not hasattr(os, name) for name in _REQUIRED) or not os.supports_dir_fd:
        raise _unsupported("safe descriptor-relative traversal is unavailable")
    source = Path(source)
    probe = source if source.is_absolute() else Path.cwd() / source
    blocked_paths_set = {Path(p).resolve(strict=False) for p in blocked_paths}
    blocked_inodes: set[tuple[int, int]] = set()
    for path in blocked_paths:
        try:
            st = Path(path).stat()
            blocked_inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    # Resolve every component descriptor-relatively from a trusted root. No
    # pathname is re-followed after validation, so replacing an ancestor can
    # only detach our open directory, never redirect traversal through a link.
    parts = probe.parts[1:] if probe.is_absolute() else probe.parts
    current = os.open('/', os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        for component in parts[:-1]:
            if component in ('', '.', '..'):
                raise _unsupported(f"unsafe source component: {component}")
            nxt = os.open(component, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=current)
            os.close(current)
            current = nxt
        if not parts:
            root_fd = os.dup(current)
        else:
            root_fd = os.open(parts[-1], flags, dir_fd=current)
    except FileNotFoundError as exc:
        raise VaultFailure("source_io", f"source does not exist: {source}") from exc
    except OSError as exc:
        raise _unsupported(f"cannot safely open source {source}: {exc}") from exc
    finally:
        os.close(current)
    try:
        if _blocked(root_fd, source, blocked_inodes, blocked_paths_set):
            raise _unsupported(f"source is blocked: {source}")
        mode = os.fstat(root_fd).st_mode
        if stat.S_ISREG(mode):
            yield SourceItem(str(source), "", "file", _read_regular(root_fd, source))
        elif stat.S_ISDIR(mode):
            yield SourceItem(str(source), "", "directory", None)
            yield from _walk_directory(root_fd, source, "", blocked_inodes, blocked_paths_set)
        else:
            raise _unsupported(f"source is not a regular file or directory: {source}")
    finally:
        os.close(root_fd)


def _read_regular(fd: int, path: Path) -> bytes:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise _unsupported(f"source is not a regular file: {path}")
    chunks = []
    while True:
        try:
            chunk = os.read(fd, 1024 * 1024)
        except OSError as exc:
            raise VaultFailure("source_io", f"failed reading source {path}: {exc}") from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _walk_directory(parent_fd: int, root: Path, relative: str,
                    blocked: set[tuple[int, int]], blocked_paths: set[Path]) -> Iterator[SourceItem]:
    try:
        names = sorted(entry.name for entry in os.scandir(parent_fd))
    except OSError as exc:
        raise VaultFailure("source_io", f"failed enumerating source {root}: {exc}") from exc
    for name in names:
        rel = f"{relative}/{name}" if relative else name
        path = root / rel
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _unsupported(f"cannot safely open source {path}: {exc}") from exc
        try:
            if _blocked(fd, path, blocked, blocked_paths):
                raise _unsupported(f"source is blocked: {path}")
            mode = os.fstat(fd).st_mode
            if stat.S_ISREG(mode):
                yield SourceItem(str(path), rel, "file", _read_regular(fd, path))
            elif stat.S_ISDIR(mode):
                yield SourceItem(str(path), rel, "directory", None)
                yield from _walk_directory(fd, root, rel, blocked, blocked_paths)
            else:
                raise _unsupported(f"source is not a regular file or directory: {path}")
        finally:
            os.close(fd)

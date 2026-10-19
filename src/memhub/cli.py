"""Command-line interface for memhub: fs-style commands over the raw store.

Store paths are absolute (start with '/'); anything else is a local file
path; '-' means stdin/stdout. Machine-readable JSON on stdout; errors are a
message on stderr with exit code 1 (no traceback). argparse usage errors
exit 2.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import config
from .store import (
    MemhubError,
    Memory,
    content_type_for,
    has_descendants,
    init_store,
    is_memory,
    ls,
    mv,
    normalize_path,
    open_store,
    put,
    read_content,
    rm,
    stat,
)


def _memory_dict(m: Memory) -> dict:
    return {
        "id": m.id,
        "path": m.path,
        "content_type": m.content_type,
        "size": m.size,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }


def _entry_dict(e) -> dict:
    m = e.memory
    return {
        "path": e.path,
        "is_dir": e.is_dir,
        "content_type": m.content_type if m else None,
        "size": m.size if m else None,
        "created_at": m.created_at if m else None,
        "updated_at": m.updated_at if m else None,
    }


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


# --- commands ---------------------------------------------------------------


def _cmd_init(args) -> int:
    name = args.name or "default"
    if args.path:
        db_path = Path(os.path.expandvars(os.path.expanduser(args.path)))
    else:
        db_path = config.config_path().parent / f"{name}.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_store(db_path)
    config.register_store(name, db_path, set_default=False)
    _print_json({"store": name, "path": str(db_path)})
    return 0


def _cmd_cp(args) -> int:
    src, dest = args.src, args.dest
    if src == "-":
        data: bytes | None = sys.stdin.buffer.read()
        base: str | None = None
        store_src = None
    elif os.path.exists(src):
        try:
            data = Path(src).read_bytes()
        except OSError as exc:
            raise MemhubError(f"cannot read {src!r}: {exc}") from exc
        base = os.path.basename(src.rstrip("/"))
        store_src = None
    else:
        # Not local: a store path -> store-to-store copy.
        data = None
        store_src = src
        base = os.path.basename(normalize_path(src))
    if dest.endswith("/"):
        if base is None:
            raise MemhubError("cannot use '-' with a directory destination")
        dest_path = dest + base
    else:
        dest_path = dest
    with open_store(config.resolve_store()) as conn:
        if store_src is not None:
            data = read_content(conn, store_src)
        mem = put(conn, dest_path, data, content_type=args.content_type)
        _print_json(_memory_dict(mem))
    return 0


def _cmd_cat(args) -> int:
    with open_store(config.resolve_store()) as conn:
        p = normalize_path(args.path)
        if not is_memory(conn, p):
            if has_descendants(conn, p):
                raise MemhubError(f"{p!r} is a directory; use ls")
            raise MemhubError(f"no memory at {p!r}")
        sys.stdout.buffer.write(read_content(conn, p))
        sys.stdout.buffer.flush()
    return 0


def _cmd_ls(args) -> int:
    with open_store(config.resolve_store()) as conn:
        entries = ls(conn, args.path, recursive=args.R)
        _print_json([_entry_dict(e) for e in entries])
    return 0


def _cmd_rm(args) -> int:
    with open_store(config.resolve_store()) as conn:
        n = rm(conn, args.path, recursive=args.r)
        _print_json({"removed": n})
    return 0


def _cmd_mv(args) -> int:
    with open_store(config.resolve_store()) as conn:
        mem = mv(conn, args.src, args.dest)
        _print_json(_memory_dict(mem))
    return 0


def _cmd_stat(args) -> int:
    with open_store(config.resolve_store()) as conn:
        _print_json(_memory_dict(stat(conn, args.path)))
    return 0


def _cmd_mkdir(args) -> int:
    # Directories are implicit; validate the path and succeed.
    normalize_path(args.path)
    return 0


# --- parser ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memhub",
        description="Raw path-addressed memory store in SQLite",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create and register a store")
    p.add_argument("name", nargs="?", default="default")
    p.add_argument("--path", help="database file (default ~/.memhub/<name>.sqlite)")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("cp", help="copy a file, stdin, or store path into the store")
    p.add_argument("src", help="local file, '-' for stdin, or store path")
    p.add_argument("dest", help="store path; trailing '/' means into that directory")
    p.add_argument("--content-type", help="content type hint (default: sniffed)")
    p.set_defaults(func=_cmd_cp)

    p = sub.add_parser("cat", help="write a memory's raw bytes to stdout")
    p.add_argument("path", help="store path")
    p.set_defaults(func=_cmd_cat)

    p = sub.add_parser("ls", help="list entries as JSON")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("-R", action="store_true", help="recursive")
    p.set_defaults(func=_cmd_ls)

    p = sub.add_parser("rm", help="remove a memory (or subtree with -r)")
    p.add_argument("path", help="store path")
    p.add_argument("-r", action="store_true", help="recursive")
    p.set_defaults(func=_cmd_rm)

    p = sub.add_parser("mv", help="move a memory within the store")
    p.add_argument("src", help="store path")
    p.add_argument("dest", help="store path")
    p.set_defaults(func=_cmd_mv)

    p = sub.add_parser("stat", help="metadata of one memory as JSON")
    p.add_argument("path", help="store path")
    p.set_defaults(func=_cmd_stat)

    p = sub.add_parser("mkdir", help="no-op: directories are implicit")
    p.add_argument("path", help="store path")
    p.set_defaults(func=_cmd_mkdir)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: usage errors exit 2, --help exits 0
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return args.func(args)
    except (MemhubError, sqlite3.Error) as exc:
        # sqlite3.Error covers a corrupt/unopenable database file, so no
        # traceback can escape the CLI (project constraint).
        print(f"memhub: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

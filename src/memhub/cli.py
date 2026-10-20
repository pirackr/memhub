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
from . import search as search_mod
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
    reindex,
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


def _store_path_arg(arg: str) -> str:
    """Reject store-path CLI args that do not start with '/'.

    Applied at the CLI boundary only; store.normalize_path stays lenient for
    library callers. Without this, `memhub cp /out.txt exported.txt`
    silently writes /exported.txt into the store instead of exporting.
    """
    if not arg.startswith("/"):
        raise MemhubError(f"store path must start with /: {arg}")
    return arg


# --- commands ---------------------------------------------------------------


def _cmd_init(args) -> int:
    name = args.name or "default"
    if args.path:
        db_path = Path(os.path.expandvars(os.path.expanduser(args.path)))
    else:
        db_path = config.config_path().parent / f"{name}.sqlite"
    existing = config.get_registered_path(name)
    if existing is not None and existing.resolve() != db_path.resolve():
        raise MemhubError(
            f"store {name!r} already registered at {existing}; "
            "edit the config to move it"
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_store(db_path)
    config.register_store(name, db_path, set_default=False)
    _print_json({"store": name, "path": str(db_path)})
    return 0


def _cmd_cp(args) -> int:
    src, dest = args.src, args.dest
    _store_path_arg(dest)
    # Resolve the store before reading stdin: a missing store must fail
    # immediately instead of appearing to hang waiting for input.
    db_path = config.resolve_store()
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
    with open_store(db_path) as conn:
        if store_src is not None:
            data = read_content(conn, store_src)
        mem = put(conn, dest_path, data, content_type=args.content_type)
        _print_json(_memory_dict(mem))
    return 0


def _cmd_cat(args) -> int:
    p = normalize_path(_store_path_arg(args.path))
    with open_store(config.resolve_store()) as conn:
        if not is_memory(conn, p):
            if has_descendants(conn, p):
                raise MemhubError(f"{p!r} is a directory; use ls")
            raise MemhubError(f"no memory at {p!r}")
        sys.stdout.buffer.write(read_content(conn, p))
        sys.stdout.buffer.flush()
    return 0


def _cmd_ls(args) -> int:
    path = _store_path_arg(args.path)
    with open_store(config.resolve_store()) as conn:
        entries = ls(conn, path, recursive=args.R)
        _print_json([_entry_dict(e) for e in entries])
    return 0


def _cmd_rm(args) -> int:
    path = _store_path_arg(args.path)
    with open_store(config.resolve_store()) as conn:
        n = rm(conn, path, recursive=args.r)
        _print_json({"removed": n})
    return 0


def _cmd_mv(args) -> int:
    src = _store_path_arg(args.src)
    dest = _store_path_arg(args.dest)
    with open_store(config.resolve_store()) as conn:
        mem = mv(conn, src, dest)
        _print_json(_memory_dict(mem))
    return 0


def _cmd_stat(args) -> int:
    path = _store_path_arg(args.path)
    with open_store(config.resolve_store()) as conn:
        _print_json(_memory_dict(stat(conn, path)))
    return 0


def _cmd_mkdir(args) -> int:
    # Directories are implicit; validate the path and succeed.
    normalize_path(_store_path_arg(args.path))
    return 0


def _cmd_search(args) -> int:
    query = " ".join(args.query)
    since = search_mod.parse_bound(args.since, "since") if args.since else None
    until = search_mod.parse_bound(args.until, "until") if args.until else None
    with open_store(config.resolve_store()) as conn:
        result = search_mod.search(
            conn,
            query,
            since=since,
            until=until,
            path=args.path,
            limit=args.limit,
            offset=args.offset,
        )
        hits = [h._asdict() for h in result.hits]
        hint: str | None = None
        if result.total == 0:
            paths = [e.path for e in ls(conn, "/")][:10]
            hint = "no matches; top-level paths: " + ", ".join(paths)
        elif args.offset + len(hits) < result.total:
            hint = (
                f"{result.total} matches; showing "
                f"{args.offset + 1}-{args.offset + len(hits)}; "
                "narrow with --since/--until/--path or paginate with --offset"
            )
        _print_json({"hits": hits, "total": result.total, "hint": hint})
    return 0


def _cmd_reindex(args) -> int:
    with open_store(config.resolve_store()) as conn:
        print("reindexing...", file=sys.stderr)
        report = reindex(conn)
        _print_json(
            {
                "upgraded": report.upgraded,
                "indexed": report.indexed,
                "backfilled": report.backfilled,
                "invalid_utf8": report.invalid_utf8,
            }
        )
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

    p = sub.add_parser("search", help="ranked full-text search as JSON")
    p.add_argument("query", nargs="+", help="search terms; quote phrases, word* prefixes")
    p.add_argument("--since", help="only memories updated on/after this date")
    p.add_argument("--until", help="only memories updated on/before this date (whole day)")
    p.add_argument("--path", help="restrict to this store path subtree")
    p.add_argument("--limit", type=int, default=10, help="max hits, 1-50 (default 10)")
    p.add_argument("--offset", type=int, default=0, help="skip this many hits")
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("reindex", help="build or rebuild the search index")
    p.set_defaults(func=_cmd_reindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: usage errors exit 2, --help exits 0
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return args.func(args)
    except BrokenPipeError:
        # Downstream reader closed the pipe (e.g. `memhub cat X | head`):
        # exit silently. Redirect stdout to devnull so the shutdown-time
        # flush of buffered data cannot raise either.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, AttributeError):  # captured stdout in tests has no usable fd
            pass
        os.close(devnull)
        return 0
    except (MemhubError, sqlite3.Error, UnicodeEncodeError) as exc:
        # sqlite3.Error covers a corrupt/unopenable database file and
        # UnicodeEncodeError a non-UTF-8 path reaching an encoder, so no
        # traceback can escape the CLI (project constraint).
        print(f"memhub: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

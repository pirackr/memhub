# memhub

A raw, path-addressed memory store for AI agents. Memories are stored like
files — `/notes/idea.txt`, `/logs/2026-10-18.md` — inside a single SQLite
database file, with a filesystem-flavored CLI (`cp`, `cat`, `ls`, `rm`, `mv`,
`stat`).

This is **layer 1: the raw store**. It stores bytes and nothing more — no
search, no embeddings, no interpretation. Higher layers (retrieval, links,
summaries) build on top of these records later and never rewrite them.

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install memhub   # or: uv pip install memhub
```

From a checkout of this repo:

```sh
uv sync && uv run memhub --help
```

## Quick example

```sh
$ memhub init personal
{"store": "personal", "path": "/home/you/.memhub/personal.sqlite"}

$ echo "buy more oat milk" | memhub cp - /notes/groceries.txt
{"id": "9f2c...", "path": "/notes/groceries.txt", "content_type": "text/plain",
 "size": 18, "created_at": "2026-10-18T10:00:00Z", "updated_at": "2026-10-18T10:00:00Z"}

$ memhub cp ~/drafts/post.md /blog/
{"id": "a11b...", "path": "/blog/post.md", ...}   # trailing '/' keeps the source basename

$ memhub cat /notes/groceries.txt
buy more oat milk

$ memhub ls
[{"path": "/blog", "is_dir": true, "content_type": null, "size": null, "created_at": null, "updated_at": null},
 {"path": "/notes", "is_dir": true, "content_type": null, "size": null, "created_at": null, "updated_at": null}]

$ memhub ls -R
[{"path": "/blog/post.md", "is_dir": false, "content_type": "text/plain", ...},
 {"path": "/notes/groceries.txt", "is_dir": false, "content_type": "text/plain", ...}]
```

Directories are implicit in paths — `mkdir` is accepted but does nothing.

## Commands

| Command | Meaning |
|---|---|
| `memhub init [NAME] [--path PATH]` | create a store and register it (first store becomes the default) |
| `memhub cp SRC DEST` | write a memory from a local file, `-` (stdin), or another store path; `DEST` ending in `/` means "into that directory" |
| `memhub cat PATH` | raw bytes to stdout, byte-exact |
| `memhub ls [PATH] [-R]` | list entries as JSON (`-R` recursive) |
| `memhub rm PATH [-r]` | remove a memory (`-r` a whole subtree) |
| `memhub mv SRC DEST` | move/rename a memory |
| `memhub stat PATH` | metadata for one memory |
| `memhub mkdir PATH` | no-op (directories are implicit) |

Store paths always start with `/`; anything else is a local file. All
commands print JSON on stdout (except `cat`, which prints raw bytes).
Errors print a message on stderr and exit 1.

## Store configuration

A store is one SQLite file. `memhub init` creates the file and registers it
in `~/.memhub/config.toml`:

```toml
default_store = "personal"

[stores]
personal = "/home/you/.memhub/personal.sqlite"
work = "/srv/memhub/work.sqlite"
```

Resolution order for every command:

1. `MEMHUB_DB=/path/to/store.sqlite` — environment override for one-offs.
2. `default_store` in the config file.

`MEMHUB_HOME` relocates the `~/.memhub` directory (config file and default
store location) — useful in scripts and tests.

## Merging stores: just `cp` the file

Stores are plain SQLite files and every command checkpoints the WAL before
exiting, so a store file is always safe to copy. The intended way to move
memories between machines (e.g. personal → work) is to copy the file:

```sh
# on the personal machine
memhub cp report.md /work/report.md
scp ~/.memhub/personal.sqlite work:~/incoming/personal.sqlite

# on the work machine — read from the copied file directly
MEMHUB_DB=~/incoming/personal.sqlite memhub cat /work/report.md
```

Deliberately, there is no export/import/merge command: the filesystem is the
merge protocol.

## Design notes

- **Raw store only.** Layer 1 keeps content byte-exact. Higher layers
  (retrieval, links, embeddings) attach to the same file later via each
  record's immutable `id` — never by rewriting content or paths.
- **Concurrency-safe.** Every command uses a short-lived WAL-mode connection,
  so multiple agents can hit the same store.
- **Machine-friendly.** JSON on stdout everywhere, so agents can parse it.

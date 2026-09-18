# Memhub

A local, text-only SQLite document tree. Requires Python 3.11+.

```sh
python -m pip install .
memhub --vault vault.db init
memhub --vault vault.db --help
python -m pytest -q
```

Commands are `init`, `ls`, `read`, `write`, `edit`, `rm`, and `import`. Virtual paths are absolute; the vault host path is always explicit. See `skills/memhub/references/commands.md` for safe workflows.

V1 deliberately has no search/FTS, POSIX expansion, mount, daemon, binary storage, links, revisions, or implicit vault initialization. Imports guess legacy encodings and may be uncertain. Concurrent writers serialize with a five-second timeout.

# Memhub

A local, text-only SQLite document tree. Requires Python 3.11+.

```sh
python -m pip install .
memhub --vault vault.db init
memhub --vault vault.db --help
python -m pytest -q
```

Commands are `init`, `ls`, `read`, `write`, `edit`, `rm`, and `import`. Virtual paths are absolute; the vault host path is always explicit. `--verify` is a global option that runs a full audit before an existing-vault command; with `init`, it audits the newly created vault before reporting success, without adding an eighth command. Library callers can use `audit_vault(path)` or `open_vault(path, audit=True)`.

Normal open is intentionally fast: it checks vault identity/version, exact canonical schema and columns, and the canonical root, but does not certify every database page, foreign key, or stored entry. Audit untrusted or suspected-corrupt vaults. Schema constraints enforce normal writes; they cannot prove existing data has not been corrupted or modified by a connection that bypassed enforcement. See `skills/memhub/references/commands.md` for safe workflows.

V1 deliberately has no search/FTS, POSIX expansion, mount, daemon, binary storage, links, revisions, or implicit vault initialization. Imports guess legacy encodings and may be uncertain. Concurrent writers serialize with a five-second timeout.

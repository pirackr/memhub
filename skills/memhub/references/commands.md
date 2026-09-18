# Commands and setup

Install with `python -m pip install .`. Copy `skills/memhub/` to `.claude/skills/memhub/`, `.opencode/skills/memhub/`, or `.pi/skills/memhub/`; pi may also use explicit `--skill` loading. This does not alter permissions or agent configuration.

```sh
memhub --vault ./knowledge.db init
printf '%s\n' 'hello' | memhub --vault ./knowledge.db --json write /notes/today --if-absent
memhub --vault ./knowledge.db --json ls /notes --limit 20 --offset 0
memhub --vault ./knowledge.db --json read /notes/today --start-line 1 --lines 20
printf '%s\n' '[{"old_text":"hello","new_text":"hello world"}]' | memhub --vault ./knowledge.db --json edit /notes/today --if-match HASH
memhub --vault ./knowledge.db import ./host-notes /archive
memhub --vault ./knowledge.db rm /notes/today
```

For multiline content use a single-quoted heredoc delimiter. `--if-match` detects content changes, not revision identity. Exit 4 means conflict: reread and recalculate. Imports are atomic and reject links/binary-like text; low-confidence encoding guesses warn on stderr. SQLite may create rollback journals. Host database permissions are the access boundary. JSON diagnostics remain on stderr; raw `read` emits only requested content.

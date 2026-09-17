-- memhub entries: an adjacency-list directory tree of UTF-8 text documents.
--
-- One SQLite database backs a whole vault. Parents and files live in a single
-- table; full virtual paths are never stored, only one path component per row.
--
-- Integrity is enforced with foreign keys, a unique sibling index, an explicit
-- root-uniqueness constraint, and (in later tasks) row triggers. See the design
-- spec section 3 for the full contract.

CREATE TABLE entries (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES entries (id)
);

-- Sibling uniqueness: a directory owns at most one child with any given name.
-- (NULL parent_id is the root, which needs a separate constraint below.)
CREATE UNIQUE INDEX entries_sibling_unique
    ON entries (parent_id, name);

-- Root uniqueness. A nullable-parent uniqueness rule alone is insufficient
-- because SQLite treats NULLs as distinct inside a unique index, so several
-- rows could share parent_id IS NULL. Index the boolean root identity so at
-- most one row is the root and every other row is NULL (and therefore allowed).
CREATE UNIQUE INDEX entries_root_unique
    ON entries ((CASE WHEN parent_id IS NULL THEN 1 ELSE NULL END));

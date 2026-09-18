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
    FOREIGN KEY (parent_id) REFERENCES entries (id),
    CHECK (kind IN ('file', 'directory')),
    CHECK ((kind = 'file' AND content IS NOT NULL) OR (kind = 'directory' AND content IS NULL)),
    CHECK ((parent_id IS NULL AND name = '' AND kind = 'directory' AND content IS NULL)
        OR (parent_id IS NOT NULL AND name <> ''
            AND instr(name, '/') = 0
            AND name NOT GLOB '*[' || char(0) || '-' || char(31) || char(127) || ']*'))
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

-- Row triggers fill the gaps a declarative constraint cannot: they can compare
-- a new row against its parent, reject identity changes on update, and guard
-- the root from deletion. Every trigger aborts the surrounding statement, which
-- the storage layer surfaces as a typed Conflict (exit status 4).

-- A child's parent must exist (foreign key) and be a directory. A row under a
-- file, or under a non-existent parent, is rejected; the root has no parent
-- and slips past this trigger untouched.
CREATE TRIGGER entries_parent_kind
    BEFORE INSERT ON entries
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM entries WHERE id = NEW.parent_id AND kind = 'directory'
        )
    BEGIN
        SELECT RAISE(ABORT, 'parent must be a directory');
    END;

-- Kinds are closed: a row is a file or a directory. V1 excludes symlinks and
-- hard links entirely, so ``link`` and anything else is a corrupt write,
-- whether it arrives through the storage library or raw SQL.
CREATE TRIGGER entries_kind_check
    BEFORE INSERT ON entries
    FOR EACH ROW
    WHEN NEW.kind NOT IN ('file', 'directory')
    BEGIN
        SELECT RAISE(ABORT, 'invalid entry kind');
    END;

-- Only files store content; directories must leave it NULL. A non-NULL
-- content column on a non-file entry is an invalid write.
CREATE TRIGGER entries_content_kind
    BEFORE INSERT ON entries
    FOR EACH ROW
    WHEN (NEW.kind = 'file' AND NEW.content IS NULL)
        OR (NEW.kind = 'directory' AND NEW.content IS NOT NULL)
    BEGIN
        SELECT RAISE(ABORT, 'file content is required and directory content must be null');
    END;

-- Non-root entries must carry a usable name. The root keeps an empty name by
-- definition, so this trigger only fires for children.
CREATE TRIGGER entries_name_check
    BEFORE INSERT ON entries
    FOR EACH ROW
    WHEN NEW.parent_id IS NOT NULL AND (
        NEW.name IS NULL OR NEW.name = '' OR instr(NEW.name, '/') > 0
        OR NEW.name GLOB '*[' || char(0) || '-' || char(31) || char(127) || ']*'
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid entry name');
    END;

-- An entry cannot reference itself as its parent.
CREATE TRIGGER entries_self_parent
    BEFORE UPDATE ON entries
    FOR EACH ROW
    WHEN NEW.parent_id = NEW.id
    BEGIN
        SELECT RAISE(ABORT, 'entry cannot be its own parent');
    END;

-- Identity is immutable once an entry exists: id, parent, name, kind, and the
-- original created-at stamp never change. Every entry is protected the same
-- way; the root is the same trigger's subject, so it cannot be relocated or
-- retyped either.
CREATE TRIGGER entries_identity_protect
    BEFORE UPDATE OF id, parent_id, name, kind, created_at ON entries
    FOR EACH ROW
    WHEN NEW.id <> OLD.id
        OR NEW.parent_id <> OLD.parent_id
        OR NEW.name <> OLD.name
        OR NEW.kind <> OLD.kind
        OR NEW.created_at <> OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'entry identity is immutable');
    END;

-- Content rules also apply to raw UPDATE statements.
CREATE TRIGGER entries_content_update
    BEFORE UPDATE OF content ON entries
    FOR EACH ROW
    WHEN (NEW.kind = 'file' AND NEW.content IS NULL)
        OR (NEW.kind = 'directory' AND NEW.content IS NOT NULL)
    BEGIN
        SELECT RAISE(ABORT, 'file content is required and directory content must be null');
    END;

-- The root can never be deleted; every other entry's deletion is left to the
-- caller so subtree removal can be planned explicitly.
CREATE TRIGGER entries_root_delete_protect
    BEFORE DELETE ON entries
    FOR EACH ROW
    WHEN OLD.parent_id IS NULL
    BEGIN
        SELECT RAISE(ABORT, 'the root entry cannot be deleted');
    END;


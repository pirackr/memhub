"""Custom JSONL benchmark format: data_dir/<name>/corpus.jsonl + queries.jsonl.

corpus.jsonl rows: ``{"id","text","path"?,"date"?}``
queries.jsonl rows: ``{"id","query","truth","group"?}`` (truth = list of
doc ids). Every item uses store key ``"all"``; group defaults to
``"custom"``. Malformed lines raise ``BenchError`` naming the file and the
1-based line number. ``core.create_dataset`` dispatches here when
``data_dir/<name>/corpus.jsonl`` exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterator

from bench.core import BenchError, Dataset, Doc, Item


def _load_jsonl(path: Path, required: tuple[str, ...],
                validate: Callable[[dict], None] | None = None) -> list[dict]:
    """Parse a JSONL file; every line must be an object with `required` keys.

    Missing file, bad JSON, non-object lines, missing keys, or validator
    failures raise BenchError with the file and 1-based line number.
    """
    if not path.is_file():
        raise BenchError(f"missing dataset file {path}")
    rows: list[dict] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise BenchError(f"{path.name} line {lineno}: invalid JSON: {e}") from e
        if not isinstance(row, dict):
            raise BenchError(f"{path.name} line {lineno}: expected a JSON object")
        missing = [key for key in required if key not in row]
        if missing:
            raise BenchError(
                f"{path.name} line {lineno}: missing key(s): "
                f"{', '.join(missing)}")
        if validate is not None:
            try:
                validate(row)
            except ValueError as e:
                raise BenchError(f"{path.name} line {lineno}: {e}") from e
        rows.append(row)
    return rows


def _validate_query(row: dict) -> None:
    if not isinstance(row["truth"], list):
        raise ValueError("'truth' must be a list of doc ids")


class CustomDataset(Dataset):
    """Benchmark dataset in the custom JSONL format (see gen_synthetic)."""

    def __init__(self, data_dir: Path, name: str):
        self.data_dir = Path(data_dir)
        self.name = name
        d = self.data_dir / name
        self.corpus_path = d / "corpus.jsonl"
        self.queries_path = d / "queries.jsonl"
        self._corpus = _load_jsonl(self.corpus_path, ("id", "text"))
        self._queries = _load_jsonl(self.queries_path,
                                    ("id", "query", "truth"),
                                    _validate_query)

    def items(self) -> Iterator[Item]:
        for q in self._queries:
            yield Item(
                id=str(q["id"]),
                group=q.get("group", "custom"),
                query=q["query"],
                truth=frozenset(q["truth"]),
                store="all",
            )

    def corpus(self, store_key: str) -> Iterator[Doc]:
        if store_key != "all":
            raise BenchError(
                f"{self.name}: unknown store {store_key!r}; "
                f"custom datasets use store 'all'")
        for row in self._corpus:
            yield Doc(id=str(row["id"]), text=row["text"],
                      path=row.get("path"), date=row.get("date"))

    def source_files(self) -> list[Path]:
        return [self.corpus_path, self.queries_path]

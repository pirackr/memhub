"""Core types and registries for the memhub benchmark system."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence


class BenchError(Exception):
    """Raised for any benchmark-specific failure (bad names, bad input)."""


@dataclass(frozen=True)
class Doc:
    id: str
    text: str
    path: str | None = None
    date: str | None = None


@dataclass(frozen=True)
class Item:
    """One benchmark query. `store` is the store key: items sharing a key
    share one ingested corpus."""

    id: str
    group: str
    query: str
    truth: frozenset[str]
    store: str


class Method(ABC):
    """A retrieval method: ingest a corpus, then search it."""

    name: str = ""

    @abstractmethod
    def ingest(self, docs: Sequence[Doc]) -> None: ...

    @abstractmethod
    def search(self, query: str, k: int) -> list[str]:
        """Return up to k ranked doc ids for the query."""

    def close(self) -> None:
        """Release resources; default no-op."""


class Dataset(ABC):
    """A benchmark dataset: queries (items) plus corpora keyed by store."""

    name: str = ""

    @abstractmethod
    def items(self) -> Iterator[Item]: ...

    @abstractmethod
    def corpus(self, store_key: str) -> Iterator[Doc]: ...

    def source_files(self) -> list[Path]:
        """Underlying dataset files (for hashing/provenance); default none."""
        return []


_METHODS: dict[str, type[Method]] = {}
_DATASETS: dict[str, type[Dataset]] = {}


def register_method(name: str):
    """Class decorator registering a Method under `name`."""

    def deco(cls: type[Method]) -> type[Method]:
        _METHODS[name] = cls
        return cls

    return deco


def register_dataset(name: str):
    """Class decorator registering a Dataset under `name`.

    Registered dataset classes are constructed as ``cls(data_dir, name)``.
    """

    def deco(cls: type[Dataset]) -> type[Dataset]:
        _DATASETS[name] = cls
        return cls

    return deco


def create_method(name: str) -> Method:
    if name not in _METHODS:
        available = ", ".join(sorted(_METHODS)) or "none"
        raise BenchError(f"unknown method {name!r}; available: {available}")
    return _METHODS[name]()


def create_dataset(name: str, data_dir: Path) -> Dataset:
    """Create a dataset by name.

    If ``data_dir/<name>/corpus.jsonl`` exists the directory is treated as a
    custom-format dataset (``bench.custom.CustomDataset``); otherwise the
    registry is consulted. Unknown names raise ``BenchError``.
    """
    if (data_dir / name / "corpus.jsonl").is_file():
        from bench import custom  # lazy: avoids import until a custom dir exists

        return custom.CustomDataset(data_dir, name)
    if name not in _DATASETS:
        available = ", ".join(sorted(_DATASETS)) or "none"
        raise BenchError(f"unknown dataset {name!r}; available: {available}")
    return _DATASETS[name](data_dir, name)


def list_datasets(data_dir: Path) -> list[str]:
    """Sorted names of custom datasets in `data_dir` plus registered ones."""
    names = set(_DATASETS)
    if data_dir.is_dir():
        for child in data_dir.iterdir():
            if child.is_dir() and (child / "corpus.jsonl").is_file():
                names.add(child.name)
    return sorted(names)

"""Tests for bench.core — types, registries, dispatch."""

import dataclasses
from pathlib import Path

import pytest

from bench import core


@pytest.fixture
def clean_registries():
    """Snapshot and restore the global registries around a test."""
    methods = dict(core._METHODS)
    datasets = dict(core._DATASETS)
    yield
    core._METHODS.clear()
    core._METHODS.update(methods)
    core._DATASETS.clear()
    core._DATASETS.update(datasets)


class DummyMethod(core.Method):
    name = "dummy"

    def ingest(self, docs):
        pass

    def search(self, query, k):
        return []


class DummyDataset(core.Dataset):
    def __init__(self, data_dir, name):
        self.data_dir = data_dir
        self.name = name

    def items(self):
        yield from ()

    def corpus(self, store_key):
        yield from ()


def test_doc_defaults_and_frozen():
    d = core.Doc(id="d1", text="hello")
    assert d.path is None and d.date is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.text = "x"


def test_item_frozen():
    i = core.Item(id="q1", group="g", query="q", truth=frozenset({"d1"}), store="all")
    assert i.truth == frozenset({"d1"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        i.group = "h"


def test_method_close_default_noop():
    DummyMethod().close()  # must not raise


def test_dataset_source_files_default():
    class Bare(core.Dataset):
        name = "bare"

        def items(self):
            yield from ()

        def corpus(self, store_key):
            yield from ()

    assert Bare().source_files() == []


def test_method_and_dataset_are_abc():
    with pytest.raises(TypeError):
        core.Method()
    with pytest.raises(TypeError):
        core.Dataset()


def test_register_and_create_method(clean_registries):
    core.register_method("dummy")(DummyMethod)
    m = core.create_method("dummy")
    assert isinstance(m, DummyMethod)
    assert isinstance(m, core.Method)


def test_create_method_unknown_lists_available(clean_registries):
    core.register_method("aaa-dummy")(DummyMethod)
    with pytest.raises(core.BenchError) as ei:
        core.create_method("nope")
    assert "aaa-dummy" in str(ei.value)
    assert "nope" in str(ei.value)


def test_register_and_create_dataset(clean_registries, tmp_path):
    core.register_dataset("dummy-ds")(DummyDataset)
    ds = core.create_dataset("dummy-ds", tmp_path)
    assert isinstance(ds, DummyDataset)
    assert ds.data_dir == tmp_path
    assert ds.name == "dummy-ds"


def test_create_dataset_unknown_lists_available(clean_registries, tmp_path):
    core.register_dataset("zzz-ds")(DummyDataset)
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("nope", tmp_path)
    msg = str(ei.value)
    assert "zzz-ds" in msg
    assert "nope" in msg


def test_create_dataset_error_message_sorted(clean_registries, tmp_path):
    core._DATASETS.clear()
    core.register_dataset("zzz-ds")(DummyDataset)
    core.register_dataset("aaa-ds")(DummyDataset)
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("nope", tmp_path)
    msg = str(ei.value)
    assert msg.index("aaa-ds") < msg.index("zzz-ds")


def test_list_datasets_finds_custom_dir(clean_registries, tmp_path):
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "corpus.jsonl").write_text("")
    (tmp_path / "bar").mkdir()  # no corpus.jsonl -> not a dataset
    names = core.list_datasets(tmp_path)
    assert "foo" in names
    assert "bar" not in names


def test_list_datasets_includes_registry_sorted(clean_registries):
    core._DATASETS.clear()
    core.register_dataset("reg-ds")(DummyDataset)
    names = core.list_datasets(Path("/nonexistent-bench-dir"))
    assert names == ["reg-ds"]

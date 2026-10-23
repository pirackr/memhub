"""Tests for bench.runner and bench.cli — matrix runner and CLI."""

import json

import pytest

from bench import cli, core, runner


class ToyMethod(core.Method):
    """Substring match over ingested docs; class-level counter pins the
    lazy per-store ingest (one ingest call per distinct store key)."""

    name = "toy"
    ingest_calls = 0

    def __init__(self):
        self.docs = []

    def ingest(self, docs):
        type(self).ingest_calls += 1
        self.docs.extend(docs)

    def search(self, query, k):
        return [d.id for d in self.docs if query in d.text][:k]


class FailingMethod(ToyMethod):
    """Raises on the 'beta' query (item i2) to exercise error recording."""

    name = "toy-fail"

    def search(self, query, k):
        if query == "beta":
            raise RuntimeError("boom")
        return super().search(query, k)


class ToyDataset(core.Dataset):
    """3 items, 2 store keys (s1 shared by i1+i2), groups g1/g2,
    one empty-truth item (i3)."""

    name = "toy"

    def __init__(self, data_dir, name):
        self.data_dir = data_dir
        self.name = name

    def items(self):
        yield core.Item("i1", "g1", "alpha", frozenset({"d1"}), "s1")
        yield core.Item("i2", "g1", "beta", frozenset({"d2"}), "s1")
        yield core.Item("i3", "g2", "gamma", frozenset(), "s2")

    def corpus(self, store_key):
        if store_key == "s1":
            yield core.Doc("d1", "alpha text")
            yield core.Doc("d2", "beta text")
        elif store_key == "s2":
            yield core.Doc("d3", "gamma text")


@pytest.fixture
def toy_registry():
    """Register toy method/datasets; restore registries and counters."""
    methods = dict(core._METHODS)
    datasets = dict(core._DATASETS)
    calls = ToyMethod.ingest_calls
    core._METHODS["toy"] = ToyMethod
    core._METHODS["toy-fail"] = FailingMethod
    core._DATASETS["toy"] = ToyDataset
    ToyMethod.ingest_calls = 0
    yield
    core._METHODS.clear()
    core._METHODS.update(methods)
    core._DATASETS.clear()
    core._DATASETS.update(datasets)
    ToyMethod.ingest_calls = calls


def make_cfg(tmp_path, **kw):
    defaults = dict(
        methods=["toy"], datasets=["toy"], data_dir=tmp_path, limit=None,
        ks=(1, 5, 10), out=tmp_path / "out.json",
    )
    defaults.update(kw)
    return runner.RunConfig(**defaults)


def rows_by_group(result):
    return {r["group"]: r for r in result["results"][0]["rows"]}


class TestRun:
    def test_writes_json_with_meta_and_rows(self, tmp_path, toy_registry):
        result = runner.run(make_cfg(tmp_path))
        data = json.loads((tmp_path / "out.json").read_text())
        assert data == result
        meta = data["meta"]
        assert meta["date"]
        assert meta["memhub_version"]
        assert meta["ks"] == [1, 5, 10]
        assert meta["limit"] is None
        assert meta["datasets"] == {"toy": {}}  # toy has no source files
        assert meta["methods"] == ["toy"]
        res = data["results"][0]
        assert res["dataset"] == "toy"
        assert res["method"] == "toy"
        assert res["n_items"] == 3
        assert res["errors"] == []
        rows = res["rows"]
        # groups sorted, ALL last
        assert [r["group"] for r in rows] == ["g1", "g2", "ALL"]
        assert [r["n"] for r in rows] == [2, 1, 3]

    def test_metric_aggregates(self, tmp_path, toy_registry):
        # i1/i2 hit their truth exactly at rank 1; i3 has empty truth and
        # the method returns d3 -> every metric 0.0 (empty-truth convention).
        result = runner.run(make_cfg(tmp_path))
        rows = rows_by_group(result)
        assert rows["g1"]["recall@1"] == pytest.approx(1.0)
        assert rows["g1"]["hit@1"] == pytest.approx(1.0)
        assert rows["g1"]["mrr"] == pytest.approx(1.0)
        assert rows["g2"]["recall@1"] == pytest.approx(0.0)
        assert rows["g2"]["hit@1"] == pytest.approx(0.0)
        assert rows["ALL"]["recall@1"] == pytest.approx(2 / 3)
        assert rows["ALL"]["ndcg@10"] == pytest.approx(2 / 3)
        # every metric key from metrics.score is present
        sample = {"recall@1", "precision@1", "recall@5", "precision@5",
                  "recall@10", "precision@10", "hit@1", "mrr", "ndcg@10"}
        assert sample <= set(rows["ALL"])

    def test_lazy_ingest_once_per_store_key(self, tmp_path, toy_registry):
        runner.run(make_cfg(tmp_path))
        # 3 items but only 2 distinct store keys -> exactly 2 ingest calls
        assert ToyMethod.ingest_calls == 2

    def test_limit_truncates_items_and_ingests(self, tmp_path, toy_registry):
        result = runner.run(make_cfg(tmp_path, limit=2))
        res = result["results"][0]
        assert res["n_items"] == 2
        assert result["meta"]["limit"] == 2
        rows = rows_by_group(result)
        assert [r["group"] for r in res["rows"]] == ["g1", "ALL"]
        assert rows["ALL"]["n"] == 2
        # only s1 was touched -> lazy ingest did not run s2
        assert ToyMethod.ingest_calls == 1

    def test_item_error_recorded_and_excluded(self, tmp_path, toy_registry):
        result = runner.run(make_cfg(tmp_path, methods=["toy-fail"]))
        res = result["results"][0]
        assert res["errors"] == [{"item": "i2", "error": "boom"}]
        assert res["n_items"] == 2
        rows = rows_by_group(result)
        assert rows["g1"]["n"] == 1
        assert rows["g1"]["recall@1"] == pytest.approx(1.0)
        assert rows["ALL"]["n"] == 2  # i1 (1.0) + i3 (empty truth, 0.0)
        assert rows["ALL"]["recall@1"] == pytest.approx(0.5)

    def test_no_out_writes_no_file(self, tmp_path, toy_registry):
        out = tmp_path / "out.json"
        result = runner.run(make_cfg(tmp_path, out=None))
        assert not out.exists()
        assert result["results"][0]["n_items"] == 3

    def test_prints_table_per_dataset_method(self, tmp_path, toy_registry,
                                             capsys):
        runner.run(make_cfg(tmp_path, out=None))
        out = capsys.readouterr().out
        assert "toy" in out
        assert "ALL" in out
        assert "recall@1" in out


class TestCli:
    def test_run_no_write_prints_table(self, tmp_path, toy_registry, capsys):
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--no-write"])
        assert code == 0
        assert "ALL" in capsys.readouterr().out

    def test_run_writes_out_file(self, tmp_path, toy_registry):
        out = tmp_path / "sub" / "res.json"
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--out", str(out)])
        assert code == 0
        data = json.loads(out.read_text())
        assert data["meta"]["methods"] == ["toy"]

    def test_unknown_method_exits_2(self, tmp_path, toy_registry, capsys):
        code = cli.main(["run", "--datasets", "toy", "--methods", "nope",
                         "--data-dir", str(tmp_path), "--no-write"])
        assert code == 2
        assert "unknown method" in capsys.readouterr().err

    def test_unknown_dataset_exits_2(self, tmp_path, toy_registry, capsys):
        code = cli.main(["run", "--datasets", "nope", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--no-write"])
        assert code == 2
        assert "unknown dataset" in capsys.readouterr().err

    def test_k_flag_parsed(self, tmp_path, toy_registry):
        out = tmp_path / "res.json"
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--k", "3,7",
                         "--out", str(out)])
        assert code == 0
        data = json.loads(out.read_text())
        assert data["meta"]["ks"] == [3, 7]
        row = data["results"][0]["rows"][-1]
        assert "recall@7" in row and "ndcg@7" in row

    def test_negative_limit_exits_2(self, tmp_path, toy_registry, capsys):
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--no-write",
                         "--limit=-1"])
        assert code == 2
        err = capsys.readouterr().err
        assert "limit" in err
        assert "Traceback" not in err

    def test_k_below_1_exits_2(self, tmp_path, toy_registry, capsys):
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--no-write",
                         "--k", "0"])
        assert code == 2
        err = capsys.readouterr().err
        assert "k" in err
        assert "Traceback" not in err

    def test_k_above_50_exits_2(self, tmp_path, toy_registry, capsys):
        # 50 is memhub's search ceiling; k>50 can never return more.
        code = cli.main(["run", "--datasets", "toy", "--methods", "toy",
                         "--data-dir", str(tmp_path), "--no-write",
                         "--k", "1,51"])
        assert code == 2
        err = capsys.readouterr().err
        assert "k" in err
        assert "Traceback" not in err

    def test_download_unknown_exits_2(self, tmp_path, capsys):
        code = cli.main(["download", "nope", "--data-dir", str(tmp_path)])
        assert code == 2
        assert "nope" in capsys.readouterr().err

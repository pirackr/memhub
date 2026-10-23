"""Tests for the custom JSONL benchmark format and the synthetic generator."""

import json
import re

import pytest

from bench import core
from bench import gen_synthetic
from bench.custom import CustomDataset

GROUPS = {"word", "prefix", "phrase", "path", "multi", "nomatch"}


@pytest.fixture(scope="module")
def synthetic():
    return gen_synthetic.generate(docs=40, seed=7)


@pytest.fixture
def corpus_by_id(synthetic):
    corpus, _ = synthetic
    return {row["id"]: row for row in corpus}


# --- generate() ---------------------------------------------------------------


def test_generate_returns_json_serializable_rows(synthetic):
    corpus, queries = synthetic
    for row in corpus + queries:
        json.dumps(row)  # must not raise


def test_generate_doc_ids(synthetic):
    corpus, _ = synthetic
    assert corpus[0]["id"] == "doc0000"
    for row in corpus:
        assert re.fullmatch(r"doc\d{4}", row["id"])
        assert row["text"]


def test_generate_all_six_groups_present(synthetic):
    _, queries = synthetic
    assert {q["group"] for q in queries} == GROUPS


def test_generate_nomatch_truth_empty(synthetic):
    _, queries = synthetic
    nomatch = [q for q in queries if q["group"] == "nomatch"]
    assert nomatch
    assert all(q["truth"] == [] for q in nomatch)


def test_generate_nonempty_truth(synthetic):
    _, queries = synthetic
    for q in queries:
        if q["group"] != "nomatch":
            assert q["truth"], q


def test_generate_word_truth_contains_probe(synthetic):
    corpus, queries = synthetic
    by_id = {row["id"]: row for row in corpus}
    word = [q for q in queries if q["group"] == "word"]
    assert word
    for q in word:
        for doc_id in q["truth"]:
            assert q["query"] in by_id[doc_id]["text"].split()


def test_generate_prefix_query_format(synthetic):
    _, queries = synthetic
    prefix = [q for q in queries if q["group"] == "prefix"]
    assert prefix
    assert all(q["query"].endswith("*") for q in prefix)


def test_generate_phrase_truth_contiguous(synthetic):
    corpus, queries = synthetic
    by_id = {row["id"]: row for row in corpus}
    phrase = [q for q in queries if q["group"] == "phrase"]
    assert phrase
    for q in phrase:
        assert q["query"].startswith('"') and q["query"].endswith('"')
        pair = q["query"][1:-1]
        assert " " in pair
        for doc_id in q["truth"]:
            assert pair in by_id[doc_id]["text"]


def test_generate_path_truth_text_lacks_probe(synthetic):
    corpus, queries = synthetic
    by_id = {row["id"]: row for row in corpus}
    path = [q for q in queries if q["group"] == "path"]
    assert path
    for q in path:
        probe = q["query"]
        assert probe  # non-empty truth guaranteed by other test
        for doc_id in q["truth"]:
            assert probe not in by_id[doc_id]["text"].split()


def test_generate_multi_truth_contains_both(synthetic):
    corpus, queries = synthetic
    by_id = {row["id"]: row for row in corpus}
    multi = [q for q in queries if q["group"] == "multi"]
    assert multi
    for q in multi:
        terms = q["query"].split()
        assert len(terms) == 2
        for doc_id in q["truth"]:
            words = set(by_id[doc_id]["text"].split())
            assert set(terms) <= words


def test_generate_nomatch_probe_absent_from_corpus(synthetic):
    corpus, queries = synthetic
    all_text = " ".join(row["text"] for row in corpus)
    for q in queries:
        if q["group"] == "nomatch":
            assert q["query"] not in all_text.split()


def test_generate_deterministic():
    a = gen_synthetic.generate(docs=20, seed=11)
    b = gen_synthetic.generate(docs=20, seed=11)
    assert a == b
    c = gen_synthetic.generate(docs=20, seed=12)
    assert a[0] != c[0]


# --- CustomDataset round-trip ---------------------------------------------------


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_roundtrip_via_create_dataset(tmp_path, synthetic):
    corpus, queries = synthetic
    d = tmp_path / "synth"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", corpus)
    _write_jsonl(d / "queries.jsonl", queries)

    ds = core.create_dataset("synth", tmp_path)
    assert isinstance(ds, CustomDataset)
    assert ds.name == "synth"

    items = list(ds.items())
    assert len(items) == len(queries)
    assert all(item.store == "all" for item in items)
    assert all(item.group in GROUPS for item in items)

    docs = list(ds.corpus("all"))
    assert len(docs) == len(corpus)
    assert docs[0].id == "doc0000"
    assert {d.id for d in docs} == {row["id"] for row in corpus}
    assert ds.source_files() == [d / "corpus.jsonl", d / "queries.jsonl"]


def test_roundtrip_item_fields_match_rows(tmp_path):
    corpus = [{"id": "doc0000", "text": "alpha beta", "path": "/docs/a.md",
               "date": "2026-01-02"},
              {"id": "doc0001", "text": "gamma"}]
    queries = [{"id": "q1", "query": "alpha", "truth": ["doc0000"],
                "group": "word"},
               {"id": "q2", "query": "zzz", "truth": []}]  # group defaults
    d = tmp_path / "mini"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", corpus)
    _write_jsonl(d / "queries.jsonl", queries)

    ds = core.create_dataset("mini", tmp_path)
    items = list(ds.items())
    assert items[0].id == "q1"
    assert items[0].group == "word"
    assert items[0].truth == frozenset({"doc0000"})
    assert items[0].store == "all"
    assert items[1].group == "custom"
    assert items[1].truth == frozenset()

    docs = {d.id: d for d in ds.corpus("all")}
    assert docs["doc0000"].path == "/docs/a.md"
    assert docs["doc0000"].date == "2026-01-02"
    assert docs["doc0001"].path is None
    assert docs["doc0001"].date is None


def test_corpus_unknown_store_raises(tmp_path):
    d = tmp_path / "mini"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", [{"id": "doc0000", "text": "x"}])
    _write_jsonl(d / "queries.jsonl", [])
    ds = core.create_dataset("mini", tmp_path)
    with pytest.raises(core.BenchError):
        list(ds.corpus("nope"))


# --- malformed input --------------------------------------------------------------


def test_malformed_query_json_reports_line(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", [{"id": "doc0000", "text": "x"}])
    (d / "queries.jsonl").write_text(
        '{"id": "q1", "query": "x", "truth": []}\n'
        '{"id": "q2", "query": "y", oops}\n'
    )
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("bad", tmp_path)
    msg = str(ei.value)
    assert "queries.jsonl" in msg
    assert "line 2" in msg


def test_malformed_query_missing_key_reports_line(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", [{"id": "doc0000", "text": "x"}])
    (d / "queries.jsonl").write_text(
        '{"id": "q1", "query": "x", "truth": []}\n'
        '{"id": "q2", "truth": []}\n'
    )
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("bad", tmp_path)
    msg = str(ei.value)
    assert "queries.jsonl" in msg
    assert "line 2" in msg
    assert "query" in msg


def test_malformed_corpus_json_reports_line(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "corpus.jsonl").write_text('{"id": "doc0000", "text": "x"}\nnot json\n')
    _write_jsonl(d / "queries.jsonl", [])
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("bad", tmp_path)
    msg = str(ei.value)
    assert "corpus.jsonl" in msg
    assert "line 2" in msg


def test_missing_queries_file_raises(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    _write_jsonl(d / "corpus.jsonl", [{"id": "doc0000", "text": "x"}])
    with pytest.raises(core.BenchError) as ei:
        core.create_dataset("bad", tmp_path)
    assert "queries.jsonl" in str(ei.value)


# --- main() -----------------------------------------------------------------------


def test_main_writes_jsonl_files(tmp_path):
    out = tmp_path / "syn"
    rc = gen_synthetic.main(["--out", str(out), "--docs", "10", "--seed", "3"])
    assert rc == 0
    corpus = [json.loads(l) for l in (out / "corpus.jsonl").read_text().splitlines()]
    queries = [json.loads(l) for l in (out / "queries.jsonl").read_text().splitlines()]
    assert corpus == gen_synthetic.generate(10, 3)[0]
    assert queries == gen_synthetic.generate(10, 3)[1]


def test_main_defaults_docs_seed(tmp_path):
    # defaults: docs=500 seed=42 — just check argparse accepts --out alone
    # on a tiny override instead of generating 500 docs here.
    out = tmp_path / "syn2"
    rc = gen_synthetic.main(["--out", str(out), "--docs", "5"])
    assert rc == 0
    assert (out / "corpus.jsonl").is_file()

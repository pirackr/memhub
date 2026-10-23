"""Tests for bench.datasets — LongMemEval and LoCoMo adapters."""

import json

import pytest

from bench import core
from bench import datasets


# --- fixtures -----------------------------------------------------------------

LME_DATA = [
    {
        "question_id": "q1",
        "question_type": "temporal-reasoning",
        "question": "When did I get my car serviced?",
        "answer": "March 15th",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": [
            "2023-04-10 (Mon) 17:50",
            "2023-04-11 (Tue) 09:00",
        ],
        "haystack_sessions": [
            [{"role": "user", "content": "Car service on March 15"}],
            [
                {"role": "user", "content": "Detailing my car soon"},
                {"role": "assistant", "content": "Nice!"},
            ],
        ],
        "answer_session_ids": ["s2"],
    },
    {
        "question_id": "q2",
        "question_type": "single-session-user",
        "question": "What kind of dog do I have?",
        "answer": "beagle",
        "haystack_session_ids": ["s3", "s4"],
        "haystack_dates": [
            "2023-04-12 (Wed) 10:00",
            "2023-04-13 (Thu) 11:00",
        ],
        "haystack_sessions": [
            [{"role": "user", "content": "My beagle Max is great"}],
            [{"role": "user", "content": "Weather is nice"}],
        ],
        "answer_session_ids": ["s3", "s4"],
    },
]

LOCOMO_DATA = [
    {
        "sample_id": "conv-x",
        "conversation": {
            "speaker_a": "Mel",
            "speaker_b": "Caroline",
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            "session_1": [
                {"speaker": "Caroline", "dia_id": "D1:1", "text": "Hi Mel"},
                {"speaker": "Melanie", "dia_id": "D1:2", "text": "Hey!"},
            ],
            "session_2_date_time": "10:00 am on 15 May, 2023",
            "session_2": [
                {"speaker": "Caroline", "dia_id": "D2:1",
                 "text": "The support group was Monday"},
            ],
        },
        "qa": [
            {"question": "When was the support group?", "answer": "7 May",
             "evidence": ["D2:1"], "category": 2},
            {"question": "Unanswerable question?", "answer": "N/A",
             "evidence": [], "category": 5},
            {"question": "What did Caroline say first?", "answer": "Hi Mel",
             "evidence": "['D1:1']", "category": 1},
        ],
    },
]


@pytest.fixture
def lme_dir(tmp_path):
    (tmp_path / "longmemeval_oracle.json").write_text(json.dumps(LME_DATA))
    return tmp_path


@pytest.fixture
def locomo_dir(tmp_path):
    (tmp_path / "locomo10.json").write_text(json.dumps(LOCOMO_DATA))
    return tmp_path


# --- registry -----------------------------------------------------------------

def test_registered_names():
    for name in ("lme-oracle", "lme-s", "lme-m", "locomo"):
        assert name in core._DATASETS
    assert core._DATASETS["lme-oracle"] is datasets.LmeDataset
    assert core._DATASETS["locomo"] is datasets.LocomoDataset


def test_create_dataset_dispatch(lme_dir, locomo_dir):
    ds = core.create_dataset("lme-oracle", lme_dir)
    assert isinstance(ds, datasets.LmeDataset)
    ds = core.create_dataset("locomo", locomo_dir)
    assert isinstance(ds, datasets.LocomoDataset)


# --- LongMemEval ---------------------------------------------------------------

def test_lme_items(lme_dir):
    ds = datasets.LmeDataset(lme_dir, "lme-oracle")
    items = list(ds.items())
    assert [i.id for i in items] == ["q1", "q2"]
    assert [i.group for i in items] == ["temporal-reasoning",
                                        "single-session-user"]
    assert [i.query for i in items] == [
        "When did I get my car serviced?", "What kind of dog do I have?"]
    assert items[0].truth == frozenset({"s2"})
    assert items[1].truth == frozenset({"s3", "s4"})
    assert [i.store for i in items] == ["q1", "q2"]


def test_lme_corpus(lme_dir):
    ds = datasets.LmeDataset(lme_dir, "lme-oracle")
    docs = list(ds.corpus("q1"))
    assert [d.id for d in docs] == ["s1", "s2"]
    assert [d.path for d in docs] == ["/sessions/s1.md", "/sessions/s2.md"]
    assert docs[0].text == (
        "Date: 2023-04-10 (Mon) 17:50\nuser: Car service on March 15")
    assert docs[1].text == (
        "Date: 2023-04-11 (Tue) 09:00\n"
        "user: Detailing my car soon\nassistant: Nice!")
    assert list(ds.corpus("q2"))[0].id == "s3"
    assert list(ds.corpus("nope")) == []


def test_lme_source_files(lme_dir):
    ds = datasets.LmeDataset(lme_dir, "lme-oracle")
    assert ds.source_files() == [lme_dir / "longmemeval_oracle.json"]


def test_lme_split_from_name(tmp_path):
    # split is derived from the name suffix after "lme-"
    with pytest.raises(core.BenchError) as ei:
        datasets.LmeDataset(tmp_path, "lme-s")
    assert "longmemeval_s.json" in str(ei.value)


def test_lme_missing_file(tmp_path):
    with pytest.raises(core.BenchError) as ei:
        datasets.LmeDataset(tmp_path, "lme-oracle")
    assert "bench download" in str(ei.value)
    assert "longmemeval_oracle.json" in str(ei.value)


# --- LoCoMo ---------------------------------------------------------------------

def test_locomo_items(locomo_dir):
    ds = datasets.LocomoDataset(locomo_dir, "locomo")
    items = list(ds.items())
    # the empty-evidence item (index 1) is skipped; ids keep original indices
    assert [i.id for i in items] == ["conv-x:0", "conv-x:2"]
    assert [i.group for i in items] == ["cat2", "cat1"]
    assert [i.query for i in items] == [
        "When was the support group?", "What did Caroline say first?"]
    assert items[0].truth == frozenset({"conv-x/session2"})
    assert items[1].truth == frozenset({"conv-x/session1"})
    assert [i.store for i in items] == ["conv-x", "conv-x"]


def test_locomo_bad_evidence_string_skipped(locomo_dir):
    data = json.loads(json.dumps(LOCOMO_DATA))
    data[0]["qa"][0]["evidence"] = "not python ["
    (locomo_dir / "locomo10.json").write_text(json.dumps(data))
    ds = datasets.LocomoDataset(locomo_dir, "locomo")
    assert [i.id for i in ds.items()] == ["conv-x:2"]


def test_locomo_corpus(locomo_dir):
    ds = datasets.LocomoDataset(locomo_dir, "locomo")
    docs = list(ds.corpus("conv-x"))
    assert [d.id for d in docs] == ["conv-x/session1", "conv-x/session2"]
    assert [d.path for d in docs] == [
        "/conv/conv-x/session1.md", "/conv/conv-x/session2.md"]
    assert docs[0].text == (
        "Date: 1:56 pm on 8 May, 2023\nCaroline: Hi Mel\nMelanie: Hey!")
    assert docs[1].text == (
        "Date: 10:00 am on 15 May, 2023\n"
        "Caroline: The support group was Monday")
    assert list(ds.corpus("other")) == []


def test_locomo_source_files(locomo_dir):
    ds = datasets.LocomoDataset(locomo_dir, "locomo")
    assert ds.source_files() == [locomo_dir / "locomo10.json"]


def test_locomo_missing_file(tmp_path):
    with pytest.raises(core.BenchError) as ei:
        datasets.LocomoDataset(tmp_path, "locomo")
    assert "bench download" in str(ei.value)
    assert "locomo10.json" in str(ei.value)

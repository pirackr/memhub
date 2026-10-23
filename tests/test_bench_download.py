"""Tests for bench.download — pinned dataset downloader with sha256 verification."""

import hashlib

import pytest

from bench import core
from bench import download


# --- helpers ------------------------------------------------------------------

class FakeResponse:
    """Minimal stand-in for urllib response: context manager with .read(n)."""

    def __init__(self, content: bytes):
        self.content = content
        self.pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        chunk = self.content[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


class RecordingOpener:
    """Opener that serves fixed content and records the URLs requested."""

    def __init__(self, content: bytes):
        self.content = content
        self.urls: list[str] = []

    def __call__(self, url: str) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(self.content)


def never_opener(url: str):
    raise AssertionError(f"opener must not be called, got: {url}")


# --- PINNED table ---------------------------------------------------------------

def test_pinned_entries():
    assert set(download.PINNED) == {"lme-oracle", "lme-s", "lme-m", "locomo"}
    commit = "2ec2a557f339b6c0369619b1ed5793734cc87533"
    hf = f"https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/{commit}"
    assert download.PINNED["lme-oracle"] == {
        "url": f"{hf}/longmemeval_oracle",
        "target": "longmemeval_oracle.json",
        "sha256": "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c",
    }
    assert download.PINNED["lme-s"] == {
        "url": f"{hf}/longmemeval_s",
        "target": "longmemeval_s.json",
        "sha256": "08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894",
    }
    assert download.PINNED["lme-m"] == {
        "url": f"{hf}/longmemeval_m",
        "target": "longmemeval_m.json",
        "sha256": "fb5413e3b077c62927daab794836991a2fcfa61ceacab57dc679fb02daaff2d9",
    }
    assert download.PINNED["locomo"] == {
        "url": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        "target": "locomo10.json",
        "sha256": "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
    }


# --- sha256_of -------------------------------------------------------------------

def test_sha256_of_known_content(tmp_path):
    p = tmp_path / "file.txt"
    p.write_bytes(b"{}")
    assert download.sha256_of(p) == hashlib.sha256(b"{}").hexdigest()


# --- fetch: happy path and cache --------------------------------------------------

def test_fetch_downloads_and_writes_target(tmp_path, monkeypatch):
    monkeypatch.setitem(download.PINNED["locomo"], "sha256", hashlib.sha256(b"{}").hexdigest())
    opener = RecordingOpener(b"{}")
    path = download.fetch("locomo", tmp_path, opener=opener)
    assert path == tmp_path / "locomo10.json"
    assert path.read_bytes() == b"{}"
    assert opener.urls == [download.PINNED["locomo"]["url"]]
    assert not (tmp_path / "locomo10.json.part").exists()


def test_fetch_cache_hit_skips_network(tmp_path, monkeypatch):
    # Patch PINNED so b'{}' is the "good" content for this test.
    sha = hashlib.sha256(b"{}").hexdigest()
    monkeypatch.setitem(download.PINNED["locomo"], "sha256", sha)
    opener = RecordingOpener(b"{}")
    download.fetch("locomo", tmp_path, opener=opener)
    assert opener.urls == [download.PINNED["locomo"]["url"]]
    # Second fetch must not hit the network.
    path = download.fetch("locomo", tmp_path, opener=never_opener)
    assert path == tmp_path / "locomo10.json"


def test_fetch_force_redownloads(tmp_path, monkeypatch):
    sha = hashlib.sha256(b"{}").hexdigest()
    monkeypatch.setitem(download.PINNED["locomo"], "sha256", sha)
    opener = RecordingOpener(b"{}")
    download.fetch("locomo", tmp_path, opener=opener)
    download.fetch("locomo", tmp_path, force=True, opener=opener)
    assert len(opener.urls) == 2


def test_fetch_existing_file_wrong_sha_redownloads(tmp_path, monkeypatch):
    target = tmp_path / "locomo10.json"
    target.write_bytes(b"garbage")
    sha = hashlib.sha256(b"{}").hexdigest()
    monkeypatch.setitem(download.PINNED["locomo"], "sha256", sha)
    opener = RecordingOpener(b"{}")
    path = download.fetch("locomo", tmp_path, opener=opener)
    assert path.read_bytes() == b"{}"
    assert opener.urls == [download.PINNED["locomo"]["url"]]


# --- fetch: failure modes ----------------------------------------------------------

def test_fetch_sha_mismatch_raises_and_leaves_no_trace(tmp_path, monkeypatch):
    monkeypatch.setitem(download.PINNED["locomo"], "sha256", "0" * 64)
    opener = RecordingOpener(b"{}")
    with pytest.raises(core.BenchError) as excinfo:
        download.fetch("locomo", tmp_path, opener=opener)
    msg = str(excinfo.value)
    assert "0" * 64 in msg  # expected sha
    assert hashlib.sha256(b"{}").hexdigest() in msg  # got sha
    assert not (tmp_path / "locomo10.json.part").exists()
    assert not (tmp_path / "locomo10.json").exists()


def test_fetch_unknown_name_raises(tmp_path):
    with pytest.raises(core.BenchError):
        download.fetch("nope", tmp_path, opener=never_opener)

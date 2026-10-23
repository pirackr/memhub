"""Pinned dataset downloader with mandatory sha256 verification."""

import hashlib
import os
import urllib.request
from pathlib import Path

from bench.core import BenchError

PINNED: dict[str, dict] = {
    "lme-oracle": {
        "url": (
            "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/"
            "2ec2a557f339b6c0369619b1ed5793734cc87533/longmemeval_oracle"
        ),
        "target": "longmemeval_oracle.json",
        "sha256": "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c",
    },
    "lme-s": {
        "url": (
            "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/"
            "2ec2a557f339b6c0369619b1ed5793734cc87533/longmemeval_s"
        ),
        "target": "longmemeval_s.json",
        "sha256": "08d8dad4be43ee2049a22ff5674eb86725d0ce5ff434cde2627e5e8e7e117894",
    },
    "lme-m": {
        "url": (
            "https://huggingface.co/datasets/xiaowu0162/longmemeval/resolve/"
            "2ec2a557f339b6c0369619b1ed5793734cc87533/longmemeval_m"
        ),
        "target": "longmemeval_m.json",
        "sha256": "fb5413e3b077c62927daab794836991a2fcfa61ceacab57dc679fb02daaff2d9",
    },
    "locomo": {
        "url": "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        "target": "locomo10.json",
        "sha256": "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
    },
}


def sha256_of(path: Path) -> str:
    """Hex sha256 digest of a file's contents, streamed in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def fetch(
    name: str,
    data_dir: Path,
    force: bool = False,
    opener=urllib.request.urlopen,
) -> Path:
    """Download a pinned dataset into data_dir, verifying its sha256.

    Returns the path to the target file. If the file already exists with the
    expected sha256 (and force is False), returns it without network access.
    Downloads stream to a .part file, are verified, then atomically renamed;
    a sha256 mismatch raises BenchError and leaves no .part or bad target.
    """
    if name not in PINNED:
        raise BenchError(f"unknown dataset: {name!r}")
    entry = PINNED[name]
    target = data_dir / entry["target"]
    expected = entry["sha256"]

    if not force and target.exists() and sha256_of(target) == expected:
        return target

    data_dir.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    try:
        with opener(entry["url"]) as resp, open(part, "wb") as out:
            while chunk := resp.read(65536):
                out.write(chunk)
        got = sha256_of(part)
        if got != expected:
            raise BenchError(
                f"sha256 mismatch for {name}: expected {expected}, got {got}"
            )
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target

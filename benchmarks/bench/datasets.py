"""Public benchmark dataset adapters: LongMemEval and LoCoMo.

Ported from memhub-bench/bench_memory_datasets.py (lme_sessions,
iter_longmemeval, iter_locomo). Truth sets contain doc ids, not paths.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Iterator

from bench.core import BenchError, Dataset, Doc, Item, register_dataset


def _load(path: Path):
    if not path.is_file():
        raise BenchError(
            f"missing dataset file {path}; run bench download")
    return json.loads(path.read_text())


@register_dataset("lme-oracle")
@register_dataset("lme-s")
@register_dataset("lme-m")
class LmeDataset(Dataset):
    """LongMemEval; the split comes from the registered name suffix."""

    name = "lme"

    def __init__(self, data_dir: Path, name: str):
        self.data_dir = Path(data_dir)
        self.name = name
        self.split = name.removeprefix("lme-")
        self.path = self.data_dir / f"longmemeval_{self.split}.json"
        self._questions = _load(self.path)

    def items(self) -> Iterator[Item]:
        for q in self._questions:
            yield Item(
                id=q["question_id"],
                group=q["question_type"],
                query=q["question"],
                truth=frozenset(q["answer_session_ids"]),
                store=q["question_id"],
            )

    def corpus(self, store_key: str) -> Iterator[Doc]:
        for q in self._questions:
            if q["question_id"] != store_key:
                continue
            for sid, date, turns in zip(q["haystack_session_ids"],
                                        q["haystack_dates"],
                                        q["haystack_sessions"]):
                body = "\n".join(f"{t['role']}: {t['content']}"
                                 for t in turns)
                yield Doc(id=sid, path=f"/sessions/{sid}.md",
                          text=f"Date: {date}\n{body}")
            return

    def source_files(self) -> list[Path]:
        return [self.path]


@register_dataset("locomo")
class LocomoDataset(Dataset):
    """LoCoMo: 10 long multi-session conversations with turn-level evidence."""

    name = "locomo"

    def __init__(self, data_dir: Path, name: str):
        self.data_dir = Path(data_dir)
        self.name = name
        self.path = self.data_dir / "locomo10.json"
        self._convs = _load(self.path)

    def items(self) -> Iterator[Item]:
        for conv in self._convs:
            sample_id = conv["sample_id"]
            for idx, qa in enumerate(conv["qa"]):
                ev_raw = qa.get("evidence") or []
                if isinstance(ev_raw, str):
                    try:
                        ev_raw = ast.literal_eval(ev_raw)
                    except (ValueError, SyntaxError):
                        ev_raw = []
                truth = frozenset(
                    f"{sample_id}/session{m.group(1)}"
                    for e in ev_raw
                    if (m := re.match(r"D(\d+):", str(e))))
                if not truth:
                    continue  # adversarial/unanswerable: no retrieval truth
                yield Item(
                    id=f"{sample_id}:{idx}",
                    group=f"cat{qa['category']}",
                    query=qa["question"],
                    truth=truth,
                    store=sample_id,
                )

    def corpus(self, store_key: str) -> Iterator[Doc]:
        for conv in self._convs:
            if conv["sample_id"] != store_key:
                continue
            c = conv["conversation"]
            n = max(int(k.split("_")[1]) for k in c
                    if re.fullmatch(r"session_\d+", k))
            for i in range(1, n + 1):
                turns = c.get(f"session_{i}", [])
                date = c.get(f"session_{i}_date_time", "")
                body = "\n".join(f"{t['speaker']}: {t['text']}"
                                 for t in turns)
                yield Doc(id=f"{conv['sample_id']}/session{i}",
                          path=f"/conv/{conv['sample_id']}/session{i}.md",
                          text=f"Date: {date}\n{body}")
            return

    def source_files(self) -> list[Path]:
        return [self.path]

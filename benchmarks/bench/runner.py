"""Matrix runner: every (dataset, method) pair, scored and aggregated.

Loads each dataset once, creates each method fresh per (dataset, method),
lazily ingests store keys (one ingest per distinct store key per method
run), searches at depth max(ks), scores via metrics.score, and aggregates
per-group means plus a trailing ALL row. Per-item errors are recorded and
excluded from aggregates; the run continues.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from statistics import fmean
from typing import Iterator

from bench.core import Dataset, Item, Method, create_dataset, create_method
from bench.download import sha256_of
from bench.metrics import score


@dataclass
class RunConfig:
    methods: list[str]
    datasets: list[str]
    data_dir: Path
    limit: int | None
    ks: tuple[int, ...] = (1, 5, 10)
    out: Path | None = None


def metric_names(ks: tuple[int, ...]) -> list[str]:
    """Metric keys in display order, matching metrics.score's output."""
    names: list[str] = []
    for k in ks:
        names += [f"recall@{k}", f"precision@{k}"]
    depth = max(ks)
    names += ["hit@1", "mrr", f"ndcg@{depth}"]
    return names


def _aggregate(group: str, scores: list[dict[str, float]],
               ks: tuple[int, ...]) -> dict:
    row: dict = {"group": group, "n": len(scores)}
    for metric in metric_names(ks):
        row[metric] = fmean(s[metric] for s in scores) if scores else 0.0
    return row


def _run_pair(dataset: Dataset, method: Method, items: list[Item],
              ks: tuple[int, ...]) -> dict:
    depth = max(ks)
    ingested: set[str] = set()
    groups: dict[str, list[dict[str, float]]] = {}
    errors: list[dict] = []
    scored: list[dict[str, float]] = []
    for item in items:
        try:
            if item.store not in ingested:
                method.ingest(dataset.corpus(item.store))
                ingested.add(item.store)
            ranked = method.search(item.query, depth)
            item_scores = score(ranked, item.truth, ks)
        except Exception as e:  # per-item errors: record, exclude, continue
            errors.append({"item": item.id, "error": str(e)})
            continue
        groups.setdefault(item.group, []).append(item_scores)
        scored.append(item_scores)
    rows = [_aggregate(g, groups[g], ks) for g in sorted(groups)]
    rows.append(_aggregate("ALL", scored, ks))
    return {"n_items": len(scored), "rows": rows, "errors": errors}


def _format_table(rows: list[dict], ks: tuple[int, ...]) -> str:
    metrics = metric_names(ks)
    header = ["group", "n", *metrics]
    table = [[r["group"], str(r["n"]), *(f"{r[m]:.3f}" for m in metrics)]
             for r in rows]
    widths = [max(len(header[i]), max(len(r[i]) for r in table))
              for i in range(len(header))]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r))
              for r in table]
    return "\n".join(lines)


def run(cfg: RunConfig) -> dict:
    """Run the full dataset x method matrix; print tables; return result dict.

    Writes JSON to cfg.out when set (parent directories are created).
    """
    meta: dict = {
        "date": datetime.now(timezone.utc).isoformat(),
        "memhub_version": version("memhub"),
        "ks": list(cfg.ks),
        "limit": cfg.limit,
        "datasets": {},
        "methods": list(cfg.methods),
    }
    results: list[dict] = []
    for dname in cfg.datasets:
        dataset = create_dataset(dname, cfg.data_dir)
        meta["datasets"][dname] = {p.name: sha256_of(p)
                                   for p in dataset.source_files()}
        items_iter: Iterator[Item] = dataset.items()
        if cfg.limit is not None:
            items_iter = itertools.islice(items_iter, cfg.limit)
        items = list(items_iter)
        for mname in cfg.methods:
            method = create_method(mname)
            try:
                outcome = _run_pair(dataset, method, items, cfg.ks)
            finally:
                method.close()
            print(f"\n=== dataset={dname} method={mname} "
                  f"n_items={outcome['n_items']} "
                  f"errors={len(outcome['errors'])} ===")
            print(_format_table(outcome["rows"], cfg.ks))
            results.append({"dataset": dname, "method": mname, **outcome})
    result = {"meta": meta, "results": results}
    if cfg.out is not None:
        cfg.out.parent.mkdir(parents=True, exist_ok=True)
        cfg.out.write_text(json.dumps(result, indent=2))
    return result

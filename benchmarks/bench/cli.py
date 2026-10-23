"""CLI for the benchmark package: run the matrix, download pinned datasets.

Usage: python -m bench run --datasets a,b --methods a,b [--limit N]
           [--k 1,5,10] [--data-dir DIR] [--out FILE | --no-write]
       python -m bench download NAME [--force] [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from bench import datasets as _datasets  # noqa: F401: register datasets
from bench import methods as _methods  # noqa: F401: register methods
from bench.core import BenchError
from bench.download import fetch
from bench.runner import RunConfig, run

# Defaults resolve next to the package: benchmarks/data, benchmarks/results.
_PKG_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = _PKG_DIR / "data"
DEFAULT_RESULTS_DIR = _PKG_DIR / "results"


def _parse_csv_ints(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(","))


def _cmd_run(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 0:
        raise BenchError(f"--limit must be >= 0, got {args.limit}")
    if not all(1 <= k <= 50 for k in args.k):
        raise BenchError(
            f"--k values must be between 1 and 50 (memhub search ceiling), "
            f"got {','.join(str(k) for k in args.k)}")
    data_dir = Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR
    datasets = args.datasets.split(",")
    methods = args.methods.split(",")
    if args.no_write:
        out = None
    elif args.out:
        out = Path(args.out)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = DEFAULT_RESULTS_DIR / (
            f"{'+'.join(datasets)}-{'+'.join(methods)}-{stamp}.json")
    cfg = RunConfig(methods=methods, datasets=datasets, data_dir=data_dir,
                    limit=args.limit, ks=args.k, out=out)
    run(cfg)
    if out is not None:
        print(f"\nwrote {out}")
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir) if args.data_dir else DEFAULT_DATA_DIR
    path = fetch(args.name, data_dir, force=args.force)
    print(f"{args.name}: {path}")
    return 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the dataset x method matrix")
    p_run.add_argument("--datasets", required=True,
                       help="comma-separated dataset names")
    p_run.add_argument("--methods", required=True,
                       help="comma-separated method names")
    p_run.add_argument("--limit", type=int, default=None,
                       help="only the first N items per dataset")
    p_run.add_argument("--k", type=_parse_csv_ints, default=(1, 5, 10),
                       metavar="1,5,10",
                       help="comma-separated cutoffs (default 1,5,10)")
    p_run.add_argument("--data-dir", default=None,
                       help=f"dataset directory (default {DEFAULT_DATA_DIR})")
    p_run.add_argument("--out", default=None,
                       help="result JSON path (default benchmarks/results/"
                            "<datasets>-<methods>-<UTC stamp>.json)")
    p_run.add_argument("--no-write", action="store_true",
                       help="print tables only, write no JSON")

    p_dl = sub.add_parser("download", help="download a pinned dataset")
    p_dl.add_argument("name", help="pinned dataset name")
    p_dl.add_argument("--force", action="store_true",
                      help="re-download even if the file exists")
    p_dl.add_argument("--data-dir", default=None,
                      help=f"dataset directory (default {DEFAULT_DATA_DIR})")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "run":
            return _cmd_run(args)
        return _cmd_download(args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

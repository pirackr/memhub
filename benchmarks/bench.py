#!/usr/bin/env python3
"""Benchmark memhub layer-1 operations against the real filesystem.

Every measurement is a real subprocess invocation (the honest unit an agent
pays): memhub CLI vs coreutils doing the same job on a plain directory.
Both sides run on the same storage medium (same parent temp dir).

Usage: uv run python benchmarks/bench.py [--iters N] [--out results.json]
"""

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MEMHUB = ["uv", "run", "memhub"]

SIZES = {"small (1 KB)": 1024, "medium (100 KB)": 100 * 1024, "large (10 MB)": 10 * 1024 * 1024}
MANY_FILES = 200


def run(cmd, env=None, stdin=None):
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd, env=env, input=stdin,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    dt = (time.perf_counter() - t0) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {proc.stderr.decode(errors='replace')[:200]}")
    return dt


def timed(runs):
    med = statistics.median(runs)
    mean = statistics.mean(runs)
    return {"median_ms": round(med, 3), "mean_ms": round(mean, 3), "runs": len(runs)}


def memhub_env(root: Path) -> dict:
    import os
    env = dict(os.environ)
    env["MEMHUB_HOME"] = str(root / "home")
    env["MEMHUB_DB"] = str(root / "store.sqlite")
    return env


def setup_memhub(root: Path):
    env = memhub_env(root)
    subprocess.run(MEMHUB + ["init", "bench", "--path", str(root / "store.sqlite")],
                   env=env, check=True, stdout=subprocess.DEVNULL)
    return env


def bench_write(iters, tmp: Path, results):
    for label, size in SIZES.items():
        src = tmp / "src.bin"
        src.write_bytes(b"x" * size)
        mh, fs = [], []
        for i in range(iters):
            mh.append(run(MEMHUB + ["cp", str(src), f"/w/{label}-{i}.bin"], env=setup_env))
            d = tmp / "fs" / "w"
            d.mkdir(parents=True, exist_ok=True)
            fs.append(run(["cp", str(src), str(d / f"{label}-{i}.bin")]))
        results.append({"op": "write", "workload": label, "memhub": timed(mh), "fs": timed(fs)})


def bench_read(iters, tmp: Path, results):
    for label, size in SIZES.items():
        mh, fs = [], []
        p = tmp / "fs" / "r" / f"{label}.bin"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"y" * size)
        run(MEMHUB + ["cp", str(p), f"/r/{label}.bin"], env=setup_env)
        for _ in range(iters):
            mh.append(run(MEMHUB + ["cat", f"/r/{label}.bin"], env=setup_env))
            fs.append(run(["cat", str(p)]))
        results.append({"op": "read", "workload": label, "memhub": timed(mh), "fs": timed(fs)})


def bench_many_writes(iters, tmp: Path, results):
    """Whole-batch timing: create MANY_FILES small memories vs files."""
    src = tmp / "small.bin"
    src.write_bytes(b"z" * 256)
    mh, fs = [], []
    for i in range(iters):
        t0 = time.perf_counter()
        for n in range(MANY_FILES):
            run(MEMHUB + ["cp", str(src), f"/many/m{n}.bin"], env=setup_env)
        mh.append((time.perf_counter() - t0) * 1000)
        d = tmp / "fs" / "many"
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        for n in range(MANY_FILES):
            run(["cp", str(src), str(d / f"m{n}.bin")])
        fs.append((time.perf_counter() - t0) * 1000)
    results.append({"op": f"write x{MANY_FILES}", "workload": "256 B each",
                    "memhub": timed(mh), "fs": timed(fs)})


def bench_list(iters, tmp: Path, results):
    mh, fs = [], []
    for _ in range(iters):
        mh.append(run(MEMHUB + ["ls", "/many"], env=setup_env))
        fs.append(run(["ls", str(tmp / "fs" / "many")]))
    results.append({"op": "list", "workload": f"{MANY_FILES} entries",
                    "memhub": timed(mh), "fs": timed(fs)})


def bench_stat(iters, tmp: Path, results):
    p = tmp / "fs" / "r" / "small (1 KB).bin"
    mh, fs = [], []
    for _ in range(iters):
        mh.append(run(MEMHUB + ["stat", "/r/small (1 KB).bin"], env=setup_env))
        fs.append(run(["stat", "-c", "%s %Y", str(p)]))
    results.append({"op": "stat", "workload": "1 KB", "memhub": timed(mh), "fs": timed(fs)})


def bench_mv(iters, tmp: Path, results):
    mh, fs = [], []
    for i in range(iters):
        run(MEMHUB + ["cp", "/r/small (1 KB).bin", "/mv-here.bin"], env=setup_env)
        mh.append(run(MEMHUB + ["mv", "/mv-here.bin", "/mv-there.bin"], env=setup_env))
        f = tmp / "fs" / "mv-here.bin"
        f.write_bytes(b"q" * 1024)
        fs.append(run(["mv", str(f), str(tmp / "fs" / "mv-there.bin")]))
    results.append({"op": "mv", "workload": "1 KB", "memhub": timed(mh), "fs": timed(fs)})


def bench_rm(iters, tmp: Path, results):
    src = tmp / "src.bin"
    src.write_bytes(b"w" * 1024)
    mh, fs = [], []
    for i in range(iters):
        run(MEMHUB + ["cp", str(src), f"/del/mh{i}.bin"], env=setup_env)
        mh.append(run(MEMHUB + ["rm", f"/del/mh{i}.bin"], env=setup_env))
        f = tmp / "fs" / f"del{i}.bin"
        f.write_bytes(b"w" * 1024)
        fs.append(run(["rm", str(f)]))
    results.append({"op": "rm", "workload": "1 KB", "memhub": timed(mh), "fs": timed(fs)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=25)
    ap.add_argument("--iters-many", type=int, default=3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="memhub-bench-"))
    global setup_env
    setup_env = setup_memhub(tmp)
    results = []
    try:
        bench_write(args.iters, tmp, results)
        bench_read(args.iters, tmp, results)
        bench_many_writes(args.iters_many, tmp, results)
        bench_list(args.iters, tmp, results)
        bench_stat(args.iters, tmp, results)
        bench_mv(args.iters, tmp, results)
        bench_rm(args.iters, tmp, results)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))

    hdr = f"{'op':<16} {'workload':<14} {'memhub ms':>10} {'fs ms':>10} {'ratio':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        ratio = r["memhub"]["median_ms"] / max(r["fs"]["median_ms"], 1e-9)
        print(f"{r['op']:<16} {r['workload']:<14} "
              f"{r['memhub']['median_ms']:>10.2f} {r['fs']['median_ms']:>10.2f} {ratio:>7.1f}x")


if __name__ == "__main__":
    sys.exit(main())

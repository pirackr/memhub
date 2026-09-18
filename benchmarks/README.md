# Benchmarks

Run deterministic warm-cache profiles:

```sh
.venv/bin/python -m benchmarks.run small --seed 20260917 --output benchmarks/results/v1-small.json
.venv/bin/python -m benchmarks.run scale --seed 20260917 --output benchmarks/results/v1-scale.json
```

Small is 1,000 documents/10,000,000 input bytes; scale is 100,000 documents/1,000,000,000 bytes. Reports preserve raw samples, median/nearest-rank p95, environment, throughput inputs, peak RSS, live database/filesystem sizes, journal observations, checks, and caveats. SQLite uses rollback journal/FULL synchronous durability. Ordinary-file mutations fsync file and parent and serialize in-process, but bulk copying has no equivalent multi-file atomic rollback. Native timestamps/IDs are excluded from equivalence.

Runs are warm OS-cache measurements. Fresh CLI processes include startup but do not imply cold caches. Cold-cache work is optional and requires an isolated documented cache-control method; never drop machine-wide caches. Bulk sample counts and semantic mismatches are disclosed rather than presented as speed parity. Encoding-detector cost is represented by import; direct UTF-8 uses its deterministic fast path.

## Recorded observations

The regenerated scale run completed 100,000 documents / 1,000,000,000 input bytes in 714.85 s overall. Every ordinary operation has 30 actual samples and bulk ingestion has three actual samples in all four mode/backend combinations. The live SQLite file was 1,093,283,840 bytes (9.33% over source bytes), versus 1,000,000,000 bytes of file content. Peak runner RSS was 247,752 KiB. Warm library medians were 0.021 ms versus 0.024 ms for immediate listing, 0.220 ms versus 0.039 ms for whole reads, and 40.58 s for atomic SQLite bulk ingestion versus 22.56 s for non-atomic ordinary-file bulk copying. Fresh-process CLI bulk medians were 40.24 s and 22.93 s respectively. These are workload-specific measurements, not a parity or universal-speed claim.

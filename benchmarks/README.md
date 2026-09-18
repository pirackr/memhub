# Benchmarks

Run deterministic warm-cache profiles:

```sh
.venv/bin/python -m benchmarks.run small --seed 20260917 --output benchmarks/results/v1-small.json
.venv/bin/python -m benchmarks.run scale --seed 20260917 --output benchmarks/results/v1-scale.json
```

Small is 1,000 documents/10,000,000 input bytes; scale is 100,000 documents/1,000,000,000 bytes. Reports preserve raw samples, median/nearest-rank p95, environment, throughput inputs, peak RSS, live database/filesystem sizes, journal observations, checks, and caveats. SQLite uses rollback journal/FULL synchronous durability. Ordinary-file mutations fsync file and parent and serialize in-process, but bulk copying has no equivalent multi-file atomic rollback. Native timestamps/IDs are excluded from equivalence.

Runs are warm OS-cache measurements. Fresh CLI processes include startup but do not imply cold caches. Cold-cache work is optional and requires an isolated documented cache-control method; never drop machine-wide caches. Bulk sample counts and semantic mismatches are disclosed rather than presented as speed parity. Encoding measurements time decoding only: source reads and destination ingestion are excluded for both direct UTF-8 and legacy detector paths. Reports also separate 30 fast opens from three full audits.

Storage evidence comes from `findmnt` and, where exposed, Linux block-device rotational metadata. An overlay, tmpfs, virtual device, or unknown medium is reported as such and is never called an SSD. Do not run or claim an SSD profile unless an actual SSD-backed mount is available.

## Recorded observations

The regenerated scale run completed 100,000 documents / 1,000,000,000 input bytes in 629.41 s overall on detected tmpfs. Every ordinary operation has 30 actual samples and bulk ingestion has three actual samples in all four mode/backend combinations. The live SQLite file was 1,093,283,840 bytes (9.33% over source bytes), versus 1,000,000,000 bytes of file content. Fast-open median was 0.102 ms; explicit full-audit median was 458.79 ms. Warm library medians were 0.225 ms versus 0.038 ms for whole reads, and 40.76 s for atomic SQLite bulk ingestion versus 22.70 s for non-atomic ordinary-file bulk copying. Fresh-process CLI whole-read median was 56.72 ms. These are workload-specific tmpfs measurements, not SSD evidence, parity, or a universal-speed claim.

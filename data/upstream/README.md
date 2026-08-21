# Pinned benchmark data

This directory records the provenance of the benchmark material used by the
Journey. It contains pinned upstream commits, licenses, checksums, source notes,
and the small local projections checked by station 0.

The repository does not ship the historical data-preparation pipeline or full
benchmark downloads. `manifest.json` documents how the committed projections
were produced; it is not a runnable build recipe. Runtime checks only read the
committed files listed under each source's `fixture_files` and verify their
SHA256 digests.

- STATE-Bench supplies the executable customer-support tasks and environment.
- ABCD supplies role-playing language and intent material.
- tau2-bench supplies fixed deduplication and difficulty signals.

These sources are benchmark or role-playing data, not production logs. Do not
replace a pinned file without updating its license, source note, checksum, and
the review evidence that refers to it.

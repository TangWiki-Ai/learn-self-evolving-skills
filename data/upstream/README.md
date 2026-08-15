# Pinned benchmark data

This directory keeps the machine-readable source manifest, upstream licenses, source notes, and the small projections used by offline CI. Full benchmark downloads and generated candidate artifacts stay ignored.

Verify the manifest, licenses, and committed fixture bytes without network access:

```bash
uv run python scripts/prepare_data.py --verify-only
```

Run the small role-playing benchmark projection and write auditable candidate artifacts:

```bash
uv run python scripts/prepare_data.py --profile fixture
```

Fetch and process every pinned full asset only with both explicit flags:

```bash
uv run python scripts/prepare_data.py \
  --download-full \
  --allow-network \
  --profile full
```

The downloader retries transient failures, checks byte counts and SHA256 digests, and atomically installs each completed asset. The processing path uses local TF-IDF adapters. It does not call an embedding service or a paid model.

The output contains scrubbed ABCD role-playing benchmark records, per-record cluster assignments and confidence, deterministic cluster representative samples, label comparisons, task-level tau2 difficulty buckets, a non-executable candidate list, funnel counts, and an artifact manifest. It does not create cases, gold data, or dataset splits.

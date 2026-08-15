# tau2-bench source note

- Repository: `https://github.com/sierra-research/tau2-bench`
- Container commit: `c3398666e6559e3a063da3fc04b5acf7f941464e`
- License: MIT; see `LICENSE` in this directory.
- Role: read-only benchmark deduplication and difficulty signals.

The pinned retail task snapshot has 114 tasks. Four pinned 4-trial result files contain 456 runs each, for 1,824 trajectories total. The pipeline keys each run by result asset, task ID, and trial; requires trials 0–3 from every asset; aggregates 16 runs per task; and only then calculates pass rate and a difficulty bucket. It never treats the 1,824 trajectories as independent questions or executable shop cases.

Three result files record generation commit `c30d59aaa71c65f9b9eb6a8f8636b48945028fcf`; the gpt-4.1-mini result records `ade39493be54aad326a4c65295f77fe09780329b`. Their embedded task payload differs from the pinned current task snapshot even though the 114 task IDs match. The pipeline therefore joins only by stable task ID and preserves both commit layers.

The committed fixture projects tasks 27 and 53 and their exact reward/run identity fields from all four result assets. It retains 16 runs per task while omitting message bodies and tool payloads from offline CI. The two tasks provide medium and easy signals for the small candidate audit.

Fixture filenames are short, but the manifest maps each one to the same `result_asset_id` used by the full profile. Run provenance therefore stays stable across fixture and full processing.

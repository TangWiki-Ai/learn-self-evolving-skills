# STATE-Bench source note

- Repository: `https://github.com/microsoft/STATE-Bench`
- Commit: `5644b1838d96bc4483da29642d058ecaa6f80f7f`
- License: MIT; see `LICENSE` in this directory.
- Role: executable benchmark source. This project uses only its exact return-item slice.

The full source has 150 customer-support task JSON files. The committed data preparation read each JSON object, kept only records whose `task_type` equals `return_item`, then joined train trajectories by `<task_id>.json` filename. This produced 33 tasks and 21 matching train trajectories; it did not select by filename wording.

The committed fixture is a field projection of three pinned task records plus the first unchanged user/assistant role/content pair from one pinned trajectory. It exists only for offline CI; it does not replace the full benchmark slice.

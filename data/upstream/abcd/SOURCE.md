# ABCD source note

- Repository: `https://github.com/asappresearch/abcd`
- Commit: `6b8700ce67c6b37b062dd7a60abc76d7ef832a97`
- License: MIT; see `LICENSE` in this directory.
- Role: role-playing benchmark language, intent clustering, and label comparison only.

The full gzip contains 10,042 conversations across `train`, `dev`, and `test`. The exact nested predicate `scenario.flow == product_defect` produces 1,070 records. The pipeline retains `convo_id`, upstream partition, `flow`, `subflow`, original turns, and delexed turns. It aligns original and delexed arrays by position and speaker while preserving the upstream delexed `turn_count`; it does not use `turn_count` as a join key.

The committed fixture projects selected, unchanged turn pairs from the three-record upstream sample. All three records trace to the full benchmark's `train` partition, which the fixture records explicitly for profile-stable source IDs. Two records pass the exact predicate and one is a negative filter control. This fixture is role-playing benchmark data, not an executable shop case.

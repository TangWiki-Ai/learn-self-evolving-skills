# Issue #6 candidate-mining contract proposal

Status: proposal only. This change does not edit `ses.contracts.RecordType`.

The data-mining module produces these records. A later contracts-owner change can
promote their discriminators into the shared `RecordType` enum without changing the
wire shape described below. All top-level records use `schema_version = "v1alpha1"`,
inherit the canonical `VersionedRecord` serializer, reject extra fields, and remain
deeply frozen.

## Proposed `RecordType` additions

| Enum member | Wire value |
| --- | --- |
| `SCRUBBED_ABCD_CONVERSATION` | `scrubbed_abcd_conversation` |
| `CLUSTER_ASSIGNMENT` | `cluster_assignment` |
| `CLUSTER_SUMMARY` | `cluster_summary` |
| `CLUSTER_LABEL_COMPARISON_SET` | `cluster_label_comparison_set` |
| `TAU2_TASK_DIFFICULTY` | `tau2_task_difficulty` |
| `TESTSET_CANDIDATE` | `testset_candidate` |
| `CANDIDATE_MINING_FUNNEL` | `candidate_mining_funnel` |
| `CANDIDATE_ARTIFACT_MANIFEST` | `candidate_artifact_manifest` |

These additions are additive. They do not change any Issue #2 record discriminator.

## Producers and consumers

`ses.testset` is the sole producer for every record in this proposal. The data-prep
CLI serializes them with `artifact_json_bytes` and publishes a checksummed candidate
artifact bundle. Testset reviewers, later curriculum work, and audit tooling may read
the bundle. No consumer may treat a candidate as an executable case. No record
contains an answer, case split, case definition, or evaluator input.

## Record fields

Types below use `NonEmptyStr`, `Sha256Digest`, `RelativeArtifactPath`,
`StrictNonNegativeInt`, and strict scalar semantics from canonical contracts.

### `ScrubbedConversationArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["scrubbed_abcd_conversation"]` |
| `source_id` | `NonEmptyStr` |
| `upstream_id` | `NonEmptyStr` |
| `source_commit` | `NonEmptyStr` |
| `source_split` | `NonEmptyStr` |
| `flow` | `NonEmptyStr` |
| `subflow` | `NonEmptyStr` |
| `original` | `tuple[DialogueTurnArtifact, ...]` |
| `delexed` | `tuple[DialogueTurnArtifact, ...]` |
| `normalized_text` | `NonEmptyStr` |
| `pair_sha256` | `Sha256Digest` |
| `dedup_sha256` | `Sha256Digest` |
| `duplicate_source_ids` | `tuple[NonEmptyStr, ...]` |
| `label_conflict` | `bool` |

`DialogueTurnArtifact` has `speaker: str`, `text: str`, and
`turn_count: StrictNonNegativeInt | None`.

Invariants: original and delexed turns are non-empty, have equal lengths, and preserve
speaker alignment. Duplicate source IDs are unique and exclude `source_id`. The
producer preserves upstream flow, subflow, and paired text rather than rewriting
intent.

### `ClusterAssignmentArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["cluster_assignment"]` |
| `item_id` | `NonEmptyStr` |
| `cluster_id` | `NonEmptyStr` |
| `confidence` | `float[0, 1] | None` |

Invariants: each published scrubbed record has exactly one assignment. Cluster IDs
come from stable cluster membership rather than adapter-local labels.

### `ClusterSummaryArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["cluster_summary"]` |
| `cluster_id` | `NonEmptyStr` |
| `member_count` | `int > 0` |
| `representative_selection_method` | `NonEmptyStr` |
| `representative_samples` | `tuple[ClusterRepresentativeArtifact, ...]` |

`ClusterRepresentativeArtifact` has `rank: int > 0`, `item_id: NonEmptyStr`,
`text: str`, `source_kind: NonEmptyStr`, `confidence: float[0, 1] | None`, and
`selection_reason: NonEmptyStr`.

Invariants: a summary has at least one representative, representative count does not
exceed member count, ranks are contiguous from one, and sample IDs are unique. The
producer uses confidence descending, missing confidence last, then source ID ascending
to make output independent of input order.

### `ClusterLabelComparisonSetArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["cluster_label_comparison_set"]` |
| `flow` | `LabelComparisonArtifact` |
| `subflow` | `LabelComparisonArtifact` |

`LabelComparisonArtifact` has `label_name: Literal["flow", "subflow"]`,
`evaluated_count: int > 0`, `excluded_missing_label_count: StrictNonNegativeInt`,
`true_label_count: int > 0`, `cluster_count: int > 0`,
`contingency: tuple[ContingencyCellArtifact, ...]`,
`adjusted_rand_index: float[-1, 1]`, and the unit-interval metrics
`normalized_mutual_info`, `homogeneity`, `completeness`, and `v_measure`. It also has
`informative: bool` and `reason: str | None`. A contingency cell has
`reference_label: NonEmptyStr`, `cluster_id: NonEmptyStr`, and `count: int > 0`.

Invariants: the set contains exactly one flow comparison and one subflow comparison.
Each contingency sum equals its evaluated count. Per-record cluster ID and confidence
remain available in the assignment artifact.

### `TauDifficultyArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["tau2_task_difficulty"]` |
| `source_id` | `NonEmptyStr` |
| `task_id` | `NonEmptyStr` |
| `task_text` | `str` |
| `run_count` | `int > 0` |
| `success_count` | `StrictNonNegativeInt` |
| `pass_rate` | `float[0, 1]` |
| `pass_rate_decimal` | `NonEmptyStr` |
| `mean_reward` | `float[0, 1]` |
| `difficulty_score` | `float[0, 1]` |
| `difficulty_bucket` | `Literal["hard", "medium", "easy"]` |
| `per_asset` | `tuple[PerAssetDifficultyArtifact, ...]` |
| `generation_commits` | `tuple[NonEmptyStr, ...]` |

`PerAssetDifficultyArtifact` has `result_asset_id: NonEmptyStr`,
`success_count: StrictNonNegativeInt`, and `run_count: int > 0`.

Invariants: the producer aggregates all 16 runs for a task before computing
difficulty. Per-asset counts reconcile to the task counts, success never exceeds runs,
the decimal pass rate matches counts, mean reward equals the binary pass rate, and
difficulty score equals one minus pass rate. Trajectories never become independent
questions.

### `CandidateArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["testset_candidate"]` |
| `candidate_id` | `NonEmptyStr` |
| `source_id` | `NonEmptyStr` |
| `duplicate_source_ids` | `tuple[NonEmptyStr, ...]` |
| `cluster_id` | `NonEmptyStr` |
| `flow` | `NonEmptyStr` |
| `subflow` | `NonEmptyStr` |
| `semantic_group_id` | `NonEmptyStr` |
| `label_frequency` | `int > 0` |
| `long_tail` | `bool` |
| `label_conflict` | `bool` |
| `tau_task_id` | `NonEmptyStr | None` |
| `tau_run_count` | `int > 0 | None` |
| `tau_success_count` | `StrictNonNegativeInt | None` |
| `tau_pass_rate` | `NonEmptyStr | None` |
| `difficulty_bucket` | `Literal["hard", "medium", "easy"] | None` |
| `similarity` | `float[0, 1] | None` |
| `retention_reasons` | `tuple[NonEmptyStr, ...]` |
| `executable` | `Literal[False]` |

Invariants: ABCD-to-ABCD semantic grouping controls duplicate lineage. Tau task mapping
only carries difficulty provenance and never merges candidates. Tau provenance fields
are either all present or all absent. Duplicate IDs are unique and exclude the retained
source. `executable` is always false.

### `MiningFunnelArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["candidate_mining_funnel"]` |
| `profile` | `Literal["fixture", "full"]` |
| `state` | `StateFunnelArtifact` |
| `abcd` | `AbcdFunnelArtifact` |
| `tau` | `TauFunnelArtifact` |

`StateFunnelArtifact` has non-negative integer fields `source_tasks`,
`return_item_tasks`, `source_trajectories`, and `return_item_trajectories`.

`AbcdFunnelArtifact` has non-negative integer fields `source_conversations`,
`exact_product_defect`, `dropped_empty`, `dropped_misaligned`, `dropped_invalid`,
`dropped_encoding`, `dropped_duplicates`, `scrubbed_unique`, `clustered`,
`semantic_duplicates_removed`, `candidate_pool`, `candidate_cap_removed`, and
`candidates`.

`TauFunnelArtifact` has non-negative integer fields `source_tasks`, `result_files`,
`trajectory_runs`, `task_aggregates`, `hard_tasks`, `medium_tasks`, and `easy_tasks`.

Invariants: every filtered count stays within its source count; clustered equals
scrubbed unique; semantic removals plus candidate pool equals clustered; cap removals
plus candidates equals candidate pool; and difficulty buckets partition task
aggregates.

### `ArtifactManifestArtifact`

| Field | Type |
| --- | --- |
| `schema_version` | `Literal["v1alpha1"]` |
| `record_type` | `Literal["candidate_artifact_manifest"]` |
| `transformation_version` | `NonEmptyStr` |
| `profile` | `Literal["fixture", "full"]` |
| `seed` | `StrictInt` |
| `mining_config` | `MiningConfigArtifact` |
| `cluster_adapter_id` | `NonEmptyStr` |
| `stratify_adapter_id` | `NonEmptyStr` |
| `upstream_manifest_sha256` | `Sha256Digest` |
| `input_sha256` | `Mapping[NonEmptyStr, Sha256Digest]` |
| `parsed_input_digest_algorithm` | `NonEmptyStr` |
| `parsed_input_sha256` | `Mapping[NonEmptyStr, Sha256Digest]` |
| `source_commits` | `Mapping[NonEmptyStr, NonEmptyStr]` |
| `artifacts` | `tuple[ArtifactEntryArtifact, ...]` |

`MiningConfigArtifact` has `candidate_count: StrictNonNegativeInt | None` and
`seed: StrictInt`. `ArtifactEntryArtifact` has `path: RelativeArtifactPath`,
`records: StrictNonNegativeInt`, `bytes: StrictNonNegativeInt`, and
`sha256: Sha256Digest`.

Invariants: all checksum inventories are non-empty, artifact paths are unique and
safe relative POSIX paths, and SHA-256 values cover the exact published bytes. The
manifest and all listed artifacts become visible as one validated bundle.

## Migration impact

The current producer-owned models already emit the proposed wire values. Promoting
the eight string literals to shared `RecordType` members requires only replacing each
local `Literal["..."]` annotation with the matching enum literal. Canonical JSON bytes
remain unchanged because `RecordType` serializes to the same string value. Readers can
adopt the new enum members additively; no data rewrite or compatibility adapter is
needed. Until the contracts owner accepts this proposal, consumers should parse these
records through `ses.testset.artifacts` and must not extend the shared enum themselves.

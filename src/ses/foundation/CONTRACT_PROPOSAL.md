# Foundation contract proposal

Foundation can run Issue #2 without changing the frozen Engine contracts. The
integrator still needs three Foundation-owned records before it persists or passes
runtime state across modules. This branch keeps equivalent objects internal; it does
not add substitute models to `ses.contracts`.

## Proposed records

### `RuntimeConfig`

- Fields: `schema_version: Literal["v1alpha1"]`,
  `record_type: Literal["runtime_config"]`, `models_lock: RelativeArtifactPath`,
  `data_manifest: RelativeArtifactPath`, `workspace_root: RelativeArtifactPath`,
  `claude_executable: NonEmptyStr`.
- Producer: Foundation Runtime.
- Consumers: Engine factory, Evaluator, CLI.
- Invariants: frozen, unknown fields rejected, project paths are relative and cannot
  traverse parents, and credential/provider-secret fields are forbidden.

### `ModelLock`

- Fields: `schema_version`, `record_type: Literal["model_lock"]`,
  `engine: Literal["claude-code"]`, `engine_version: NonEmptyStr`, and an exact map
  from `main | creator | simulator | judge` to `{model_id, base_url}`.
- Producer: Foundation Runtime.
- Consumers: Engine factory, Runner metadata, CLI doctor.
- Invariants: every role exists exactly once; endpoint is absolute HTTPS without
  userinfo, query, or fragment; no credentials or request headers appear.

### `WorkspaceRef`

- Fields: `schema_version`, `record_type: Literal["workspace_ref"]`, `run_id`,
  `case_id`, `iteration_id`, `workspace_path: RelativeArtifactPath`, and
  `claude_config_path: RelativeArtifactPath`.
- Producer: Foundation Runtime.
- Consumers: Evaluator and Runner.
- Invariants: paths are relative to the configured workspace root, both paths stay
  inside one unique case directory, and the record never exposes a host absolute
  path. MCP config remains an artifact reference only when a consumer must retain it.

## Migration and tests

These are additive alpha records; no persisted artifacts currently require a
migration. The contract owner should add round-trip, frozen/extra-forbid, relative
path, endpoint, role completeness, canonical hash, and credential-rejection tests.
The integrator can then replace Foundation's internal Pydantic config models at the
module boundary without changing `Engine.stream(EngineRequest)` or emitted
`EngineEvent` values.

`EngineResult` is not proposed for Issue #2. A terminal `completed` event already
carries session and exit status, while the latest `usage` event carries cumulative
usage. Adding a second summary record now would duplicate those facts.

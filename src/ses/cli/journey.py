"""Thin CLI adapter for the live-first eight-station learner journey."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from ses.dashboard import DEFAULT_HOST, DEFAULT_PORT, create_dashboard_server
from ses.foundation.config import ProviderId, load_model_lock, load_runtime_config
from ses.foundation.credentials import CredentialError, credential_values, redact
from ses.journey import (
    DEFAULT_STATION_COMMANDS,
    ExperimentCostSource,
    ExperimentMode,
    JourneyStateError,
    JourneyStatusStore,
)
from ses.journey.course import (
    JourneyCourseError,
    JourneyMode,
    StationRun,
    artifact_ref,
    journey_usage_from_reports,
    parse_assignments,
    run_station_0,
    run_station_1,
    run_station_2,
    run_station_3,
    run_station_4,
    run_station_5,
    run_station_6,
    run_station_7,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _safe_message(exc: Exception) -> str:
    return redact(str(exc), credential_values(os.environ)) or type(exc).__name__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses journey",
        description="Run and resume the live-first one-day Skill evolution journey.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    station = commands.add_parser("station", help="Run one station from 0 through 7.")
    station.add_argument("number", type=int, choices=range(8))
    station.add_argument("--workspace", type=Path, default=Path.cwd())
    station.add_argument("--project-root", type=Path, default=_project_root())
    station.add_argument("--mode", choices=("live", "fixed"), default="live")
    station.add_argument("--provider", choices=tuple(ProviderId))
    station.add_argument("--timeout", type=float, default=300)
    station.add_argument("--select", action="append")
    station.add_argument("--attribution", action="append", default=[])
    station.add_argument("--diagnosis", action="append", default=[])
    station.add_argument("--location", action="append", default=[])
    station.add_argument("--rationale", default="")
    station.add_argument(
        "--decision", choices=("follow-gate", "refine", "hold"), default="follow-gate"
    )
    station.add_argument(
        "--action",
        dest="release_action",
        choices=("release", "release-rollback-restore", "defer"),
    )
    station.add_argument("--json", action="store_true", dest="as_json")

    dashboard = commands.add_parser(
        "dashboard", help="Start the local read-only dashboard."
    )
    dashboard.add_argument("--workspace", type=Path, default=Path.cwd())
    dashboard.add_argument("--host", default=DEFAULT_HOST)
    dashboard.add_argument("--port", type=int, default=DEFAULT_PORT)
    dashboard.add_argument("--no-open", action="store_true")

    status = commands.add_parser(
        "status", help="Print the current canonical journey state."
    )
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    return parser


def _selection(values: list[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    if "none" in values:
        if values != ["none"]:
            raise JourneyCourseError("--select none cannot be combined with a case ID")
        return ()
    return tuple(values)


def _execute(args: argparse.Namespace, *, provider: ProviderId | None) -> StationRun:
    workspace = args.workspace.resolve()
    project_root = args.project_root.resolve(strict=True)
    mode: JourneyMode = args.mode
    if args.timeout <= 0:
        raise JourneyCourseError("--timeout must be greater than zero")
    if args.number == 0:
        return run_station_0(
            workspace=workspace,
            project_root=project_root,
            mode=mode,
            timeout=args.timeout,
            provider=provider or ProviderId.SILICONFLOW,
        )
    if args.number == 1:
        return run_station_1(
            workspace=workspace,
            selected_case_ids=_selection(args.select),
        )
    if args.number == 2:
        return run_station_2(
            workspace=workspace,
            assignments=parse_assignments(args.attribution),
        )
    if args.number == 3:
        return run_station_3(
            workspace=workspace,
            diagnoses=parse_assignments(args.diagnosis),
            locations=parse_assignments(args.location),
        )
    if args.number == 4:
        return run_station_4(workspace=workspace, rationale=args.rationale)
    if args.number == 5:
        return run_station_5(
            workspace=workspace,
            project_root=project_root,
            mode=mode,
            timeout=args.timeout,
            decision=args.decision,
            provider=provider or ProviderId.SILICONFLOW,
        )
    if args.number == 6:
        if args.release_action is None:
            raise JourneyCourseError(
                "station 6 needs --action release, release-rollback-restore, or defer"
            )
        return run_station_6(workspace=workspace, action=args.release_action)
    return run_station_7(workspace=workspace)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _station_commands(provider: ProviderId | None) -> tuple[str, ...]:
    if provider is None:
        return DEFAULT_STATION_COMMANDS
    return tuple(
        f"uv run ses journey station {number} --provider {provider.value}"
        for number in range(8)
    )


def _status_store(
    workspace: Path,
    *,
    project_root: Path,
    mode: JourneyMode,
    requested_provider: str | None,
) -> tuple[JourneyStatusStore, ProviderId | None]:
    store = JourneyStatusStore(workspace)
    requested_mode = ExperimentMode(mode)
    if store.status_path.is_file():
        state = store.load()
        if state.experiment_mode != requested_mode:
            raise JourneyStateError(
                "journey mode differs from this workspace; use a fresh workspace"
            )
        if mode == "fixed":
            if requested_provider is not None:
                raise JourneyStateError("fixed journey does not use a live provider")
            return store, None
        provider = state.experiment_provider
        if provider is None:
            raise JourneyStateError("live journey has no persisted provider")
        if (
            requested_provider is not None
            and ProviderId(requested_provider) is not provider
        ):
            raise JourneyStateError(
                "journey provider differs from this workspace; use a fresh workspace"
            )
        config = load_runtime_config(project_root / "ses.json")
        lock_path = project_root / config.models_lock_for(provider)
        lock = load_model_lock(lock_path)
        if (
            lock.provider is not provider
            or _sha256(lock_path) != state.model_lock_sha256
        ):
            raise JourneyStateError(
                "journey model lock differs from this workspace; use a fresh workspace"
            )
        return store, provider

    if mode == "fixed":
        if requested_provider is not None:
            raise JourneyStateError("fixed journey does not use a live provider")
        store.initialize(
            DEFAULT_STATION_COMMANDS,
            cost_currency="CNY",
            experiment_mode=requested_mode,
            experiment_provider=None,
            model_lock_sha256=None,
            cost_source=ExperimentCostSource.SYNTHETIC_CI,
        )
        return store, None
    if requested_provider is None:
        raise JourneyStateError(
            "a new live journey requires --provider siliconflow or chatanywhere"
        )
    provider = ProviderId(requested_provider)
    config = load_runtime_config(project_root / "ses.json")
    lock_path = project_root / config.models_lock_for(provider)
    lock = load_model_lock(lock_path)
    if lock.provider is not provider:
        raise JourneyStateError("selected provider differs from its model lock")
    store.initialize(
        _station_commands(provider),
        cost_currency="CNY" if provider is ProviderId.CHATANYWHERE else "USD",
        experiment_mode=requested_mode,
        experiment_provider=provider,
        model_lock_sha256=_sha256(lock_path),
        cost_source=(
            ExperimentCostSource.UNAVAILABLE
            if provider is ProviderId.CHATANYWHERE
            else ExperimentCostSource.CLAUDE_CODE_ESTIMATE
        ),
    )
    return store, provider


def _record_result(
    store: JourneyStatusStore,
    result: StationRun,
) -> None:
    decision_refs = tuple(
        artifact_ref(path, workspace=store.workspace) for path in result.decisions
    )
    artifact_refs = tuple(
        artifact_ref(path, workspace=store.workspace) for path in result.artifacts
    )
    cumulative_usage = journey_usage_from_reports(store.workspace)
    if cumulative_usage is not None:
        store.replace_usage(cumulative_usage)
    if result.status == "completed":
        store.complete_station(
            result.number,
            decision_refs=decision_refs,
            artifact_refs=artifact_refs,
        )
        return
    store.record_station_refs(
        result.number,
        decision_refs=decision_refs,
        artifact_refs=artifact_refs,
    )
    store.mark_needs_attention(
        result.number,
        result.reason or "station needs attention",
    )


def _print_result(
    result: StationRun, store: JourneyStatusStore, *, as_json: bool
) -> None:
    state = store.load()
    relative_artifacts = tuple(
        path.relative_to(store.workspace).as_posix() for path in result.artifacts
    )
    primary_artifacts = tuple(
        path for path in relative_artifacts if "/artifacts/" not in f"/{path}"
    )
    raw_artifact_count = len(relative_artifacts) - len(primary_artifacts)
    payload = {
        "artifacts": list(primary_artifacts),
        "decisions": [
            path.relative_to(store.workspace).as_posix() for path in result.decisions
        ],
        "journey_status": state.status.value,
        "metrics": dict(result.metrics),
        "reason": result.reason,
        "raw_artifact_count": raw_artifact_count,
        "station": result.number,
        "station_status": result.status,
        "status_path": store.status_path.relative_to(store.workspace).as_posix(),
        "usage": result.usage.model_dump(mode="json"),
    }
    if as_json:
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return
    print(f"station={result.number}")
    print(f"status={result.status}")
    for name, value in result.metrics.items():
        print(f"{name}={value}")
    for path in primary_artifacts:
        print(f"artifact={path}")
    if raw_artifact_count:
        print(f"raw_artifacts={raw_artifact_count} (linked from the HTML report)")
    if result.reason:
        print(f"next={result.reason}")


def _station_main(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    store: JourneyStatusStore | None = None
    try:
        store, provider = _status_store(
            workspace,
            project_root=args.project_root.resolve(strict=True),
            mode=args.mode,
            requested_provider=args.provider,
        )
        store.start_station(args.number)
        result = _execute(args, provider=provider)
        _record_result(store, result)
    except (
        JourneyCourseError,
        JourneyStateError,
        CredentialError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        message = _safe_message(exc)
        if store is not None:
            try:
                store.mark_needs_attention(args.number, message)
            except (JourneyStateError, OSError, TypeError, ValueError):
                pass
        print(f"journey_error:{message}", file=sys.stderr)
        return 1
    assert store is not None
    _print_result(result, store, as_json=args.as_json)
    return 0 if result.status == "completed" else 2


def _dashboard_main(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve(strict=True)
    store = JourneyStatusStore(workspace)
    if store.status_path.exists():
        store.load()
    try:
        with create_dashboard_server(
            workspace, host=args.host, port=args.port
        ) as server:
            host, port = server.server_address[:2]
            host_text = host.decode() if isinstance(host, bytes) else str(host)
            url = f"http://{host_text}:{int(port)}/"
            print(f"dashboard={url}")
            if not args.no_open:
                webbrowser.open(url)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(f"dashboard_error:{_safe_message(exc)}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse learner journey arguments and return a shell exit code."""

    args = _build_parser().parse_args(argv)
    if args.action == "station":
        return _station_main(args)
    if args.action == "dashboard":
        return _dashboard_main(args)
    try:
        state = JourneyStatusStore(args.workspace).load()
    except (JourneyStateError, OSError, TypeError, ValueError) as exc:
        print(f"journey_status_error:{_safe_message(exc)}", file=sys.stderr)
        return 1
    print(state.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

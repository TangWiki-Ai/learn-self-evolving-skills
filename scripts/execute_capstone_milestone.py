#!/usr/bin/env python3
"""Execute one locked capstone target through its selected milestone module."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast

from ses.contracts import CapstoneMilestonePolicyCheck, artifact_json_bytes
from ses.release.capstone import TARGET_COMMANDS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--variant", choices=("starter", "solution"), required=True)
    parser.add_argument("--milestone", required=True)
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--policy-receipt", type=Path, required=True)
    parser.add_argument("target", nargs=argparse.REMAINDER)
    return parser


def _load_module(root: Path, variant: str, milestone: str) -> ModuleType:
    path = root / "course" / "capstone-shopping-assistant" / variant / f"{milestone}.py"
    if not path.is_file() or path.is_symlink():
        raise ValueError("selected milestone implementation is not a regular file")
    spec = importlib.util.spec_from_file_location(
        f"shopping_capstone_{variant}_{milestone}",
        path,
    )
    if spec is None or spec.loader is None:
        raise ValueError("selected milestone implementation cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _policy_fixture(root: Path, milestone: str) -> tuple[dict[str, object], str]:
    path = (
        root
        / "course"
        / "capstone-shopping-assistant"
        / "fixtures"
        / "milestone-policy-v1.json"
    )
    content = path.read_bytes()
    value = json.loads(content)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "v1alpha1"
        or value.get("record_type") != "shopping_capstone_milestone_policy_fixture"
    ):
        raise ValueError("milestone policy fixture identity is invalid")
    milestones = value.get("milestones")
    if not isinstance(milestones, dict):
        raise ValueError("milestone policy fixture inventory is invalid")
    row = milestones.get(milestone)
    if not isinstance(row, dict):
        raise ValueError("milestone policy probe is missing")
    return row, hashlib.sha256(content).hexdigest()


def _write_receipt(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError("milestone policy receipt already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target_definition = {target.command_id: target for target in TARGET_COMMANDS}.get(
        args.command_id
    )
    if target_definition is None or target_definition.milestone != args.milestone:
        raise ValueError("target command is not owned by the selected milestone")
    command = tuple(args.target)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("milestone target command is missing")
    root = args.root.resolve(strict=True)
    module = _load_module(root, args.variant, args.milestone)
    fixture, fixture_sha256 = _policy_fixture(root, args.milestone)
    probe = fixture.get("probe")
    expected_policy = fixture.get("expected")
    if not isinstance(probe, dict) or not isinstance(expected_policy, dict):
        raise ValueError("milestone policy probe or expected result is invalid")
    execute_target = cast(
        Callable[
            [
                str,
                dict[str, object],
                Callable[[object], str],
                Callable[[], int],
            ],
            int,
        ],
        module.execute_target,
    )
    validation_calls = 0
    execution_calls = 0
    policy_result_sha256: str | None = None
    target_exit_code: int | None = None

    def validate_policy(result: object) -> str:
        nonlocal validation_calls, policy_result_sha256
        validation_calls += 1
        if validation_calls != 1:
            raise RuntimeError("milestone validated its policy more than once")
        content = _canonical_json_bytes(result)
        normalized = json.loads(content)
        if normalized != expected_policy:
            raise ValueError("milestone policy result differs from the locked fixture")
        policy_result_sha256 = hashlib.sha256(content).hexdigest()
        return policy_result_sha256

    def execute_once() -> int:
        nonlocal execution_calls, target_exit_code
        if validation_calls != 1 or policy_result_sha256 is None:
            raise RuntimeError("milestone must validate policy before target execution")
        execution_calls += 1
        if execution_calls != 1:
            raise RuntimeError("milestone attempted the target more than once")
        target_exit_code = subprocess.run(command, check=False).returncode
        return target_exit_code

    result = execute_target(args.command_id, probe, validate_policy, execute_once)
    if validation_calls != 1:
        raise RuntimeError("milestone did not validate its policy exactly once")
    if execution_calls != 1:
        raise RuntimeError("milestone did not execute its target exactly once")
    if type(result) is not int:
        raise TypeError("milestone target result must be an integer exit code")
    if result != target_exit_code:
        raise RuntimeError("milestone changed the target exit code")
    implementation = (
        root
        / "course"
        / "capstone-shopping-assistant"
        / args.variant
        / f"{args.milestone}.py"
    )
    assert policy_result_sha256 is not None
    assert target_exit_code is not None
    receipt = CapstoneMilestonePolicyCheck.model_validate(
        {
            "schema_version": "v1alpha1",
            "record_type": "capstone_milestone_policy_check",
            "milestone": args.milestone,
            "command_id": args.command_id,
            "implementation_variant": args.variant,
            "implementation_path": implementation.relative_to(root).as_posix(),
            "implementation_sha256": hashlib.sha256(
                implementation.read_bytes()
            ).hexdigest(),
            "fixture_path": (
                "course/capstone-shopping-assistant/fixtures/milestone-policy-v1.json"
            ),
            "fixture_sha256": fixture_sha256,
            "policy_result_sha256": policy_result_sha256,
            "status": "passed",
            "target_exit_code": target_exit_code,
        }
    )
    _write_receipt(args.policy_receipt, artifact_json_bytes(receipt))
    return result


if __name__ == "__main__":
    raise SystemExit(main())

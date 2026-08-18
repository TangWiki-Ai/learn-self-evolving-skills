"""Atomic state and write-ahead intents for resumable auto-evolution."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ses.contracts import (
    AutoEvolveConfig,
    AutoEvolveState,
    AutoLoopStatus,
    SchemaVersion,
    artifact_json_bytes,
    content_sha256,
)

_SAFE_STEPS = frozenset({"rollout", "reflect", "patch", "gate", "final"})
_SHA256_LENGTH = 64


class AutoStateError(ValueError):
    """The loop state is inconsistent or a paid step may be incomplete."""


@dataclass(frozen=True, slots=True)
class StepBudgetUsage:
    """Observed usage bound to one completed orchestration step."""

    cost_amount: Decimal
    cost_currency: str
    cost_complete: bool
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise AutoStateError("step cost must be finite and nonnegative")
        if not self.cost_currency or self.cost_currency != self.cost_currency.upper():
            raise AutoStateError("step cost currency must be an uppercase code")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise AutoStateError("step token usage must be nonnegative")

    def to_json(self) -> dict[str, object]:
        return {
            "cost_amount": str(self.cost_amount),
            "cost_complete": self.cost_complete,
            "cost_currency": self.cost_currency,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_json(cls, payload: object) -> StepBudgetUsage:
        if not isinstance(payload, dict):
            raise AutoStateError("step budget receipt is invalid")
        try:
            if not isinstance(payload["cost_amount"], str):
                raise ValueError
            if type(payload["cost_complete"]) is not bool:
                raise ValueError
            if not isinstance(payload["cost_currency"], str):
                raise ValueError
            if type(payload["input_tokens"]) is not int:
                raise ValueError
            if type(payload["output_tokens"]) is not int:
                raise ValueError
            return cls(
                cost_amount=Decimal(payload["cost_amount"]),
                cost_currency=payload["cost_currency"],
                cost_complete=payload["cost_complete"],
                input_tokens=payload["input_tokens"],
                output_tokens=payload["output_tokens"],
            )
        except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
            raise AutoStateError("step budget receipt is invalid") from exc


@dataclass(frozen=True, slots=True)
class StepReceipt:
    """Verified journal receipt for one completed step."""

    round_number: int
    step: str
    config_sha256: str
    input_hashes: Mapping[str, str]
    output_hashes: Mapping[str, str]
    intent_sha256: str
    budget: StepBudgetUsage | None


class AutoStateStore:
    """Own one experiment's canonical config, state, and step intents."""

    def __init__(self, root: Path) -> None:
        if ".." in root.parts or root.is_symlink() or root.absolute() != root.resolve():
            raise AutoStateError("auto-evolve root must be a canonical real path")
        self.root = root.resolve()
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.journal_root = self.root / ".journal"
        self.freeze_path = self.root / "FREEZE"
        self.lock_path = self.root / ".auto-evolve.lock"
        self._active_journal_descriptor: int | None = None
        if self.root.exists() and (
            self.journal_root.exists() or self.journal_root.is_symlink()
        ):
            with self._journal_descriptor(create=False) as descriptor:
                if descriptor is None:
                    raise AutoStateError("auto-evolve journal is invalid")

    @contextmanager
    def experiment_lock(self) -> Iterator[None]:
        """Hold an OS-released exclusive lock for one complete run attempt."""

        self._create_root()
        root_descriptor = self._open_root_descriptor()
        lock_descriptor: int | None = None
        journal_descriptor: int | None = None
        try:
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                lock_descriptor = os.open(
                    self.lock_path.name,
                    flags,
                    0o600,
                    dir_fd=root_descriptor,
                )
            except OSError as exc:
                raise AutoStateError(
                    "auto-evolve experiment lock cannot be opened"
                ) from exc
            if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
                raise AutoStateError("experiment lock must be a regular file")
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AutoStateError(
                    "auto-evolve experiment is already running"
                ) from exc
            journal_descriptor = self._open_journal_descriptor(
                root_descriptor,
                create=True,
            )
            assert journal_descriptor is not None
            self._active_journal_descriptor = journal_descriptor
            yield
        finally:
            self._active_journal_descriptor = None
            if journal_descriptor is not None:
                os.close(journal_descriptor)
            if lock_descriptor is not None:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
            os.close(root_descriptor)

    def initialize(
        self,
        config: AutoEvolveConfig,
        *,
        accepted_skill_sha256: str,
    ) -> AutoEvolveState:
        """Create the initial state or verify an exact compatible resume."""

        self._create_root()
        config_bytes = artifact_json_bytes(config)
        if self.config_path.exists():
            if self._read_regular(self.config_path) != config_bytes:
                raise AutoStateError("resume config differs from the locked experiment")
        else:
            self._atomic_write(self.config_path, config_bytes)
        if self.state_path.exists():
            state = self.load()
            if state.config_sha256 != content_sha256(config):
                raise AutoStateError("loop state uses another config")
            return state
        state = AutoEvolveState(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="auto_evolve_state",
            experiment_id=config.experiment_id,
            config_sha256=content_sha256(config),
            status=AutoLoopStatus.RUNNING,
            current_accepted_skill_sha256=accepted_skill_sha256,
            completed_rounds=0,
            rounds=(),
            total_cost_amount=Decimal(0),
            cost_currency=config.cost_currency,
            cost_complete=True,
            total_input_tokens=0,
            total_output_tokens=0,
            consecutive_rejections=0,
        )
        self.write(state)
        return state

    def load(self) -> AutoEvolveState:
        """Read and validate the canonical state snapshot."""

        try:
            payload = self._read_regular(self.state_path)
            state = AutoEvolveState.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise AutoStateError("auto-evolve state is invalid") from exc
        if artifact_json_bytes(state) != payload:
            raise AutoStateError("auto-evolve state is not canonical")
        return state

    def write(self, state: AutoEvolveState) -> None:
        """Atomically replace the replayable aggregate state."""

        self._create_root()
        self._atomic_write(self.state_path, artifact_json_bytes(state))

    def freeze_requested(self) -> bool:
        """Return whether an operator placed the explicit local freeze marker."""

        if not self.freeze_path.exists():
            return False
        if self.freeze_path.is_symlink() or not self.freeze_path.is_file():
            raise AutoStateError("freeze marker must be a regular file")
        return True

    def begin_step(
        self,
        *,
        round_number: int,
        step: str,
        expected_outputs: Sequence[Path],
        input_hashes: Mapping[str, str] | None = None,
    ) -> bool:
        """Create an exclusive intent or resume only from hash-bound outputs."""

        self._validate_step_identity(round_number=round_number, step=step)
        normalized_inputs = self._validate_input_hashes(input_hashes or {})
        output_paths = self._normalize_outputs(expected_outputs)
        intent_payload = self._intent_payload(
            round_number=round_number,
            step=step,
            input_hashes=normalized_inputs,
            output_paths=output_paths,
        )
        intent_name = self._journal_name(round_number=round_number, step=step)
        receipt_name = self._journal_name(
            round_number=round_number,
            step=step,
            receipt=True,
        )
        with self._journal_descriptor(create=True) as journal_descriptor:
            assert journal_descriptor is not None
            if self._journal_entry_exists(journal_descriptor, receipt_name):
                self._load_receipt(
                    journal_descriptor,
                    round_number=round_number,
                    step=step,
                    expected_intent=intent_payload,
                    verify_outputs=True,
                )
                return False
            if self._journal_entry_exists(journal_descriptor, intent_name):
                self._validate_existing_intent(
                    journal_descriptor,
                    intent_name,
                    intent_payload,
                )
                if self._outputs_complete(output_paths):
                    return False
                raise AutoStateError(
                    "auto-evolve step is interrupted with missing output; refusing to "
                    "repeat a possibly paid action"
                )
            if any(path.exists() or path.is_symlink() for path in output_paths):
                raise AutoStateError(
                    "step output exists without its write-ahead intent"
                )
            try:
                self._journal_exclusive_write(
                    journal_descriptor,
                    intent_name,
                    self._json_bytes(intent_payload),
                    mode=0o600,
                )
            except FileExistsError:
                self._validate_existing_intent(
                    journal_descriptor,
                    intent_name,
                    intent_payload,
                )
                raise AutoStateError(
                    "auto-evolve step is interrupted or concurrently running"
                ) from None
        return True

    def complete_step(
        self,
        *,
        round_number: int,
        step: str,
        expected_outputs: Sequence[Path],
        input_hashes: Mapping[str, str] | None = None,
        budget: StepBudgetUsage | None = None,
    ) -> StepReceipt:
        """Commit an O_EXCL completion receipt over exact input and output hashes."""

        self._validate_step_identity(round_number=round_number, step=step)
        normalized_inputs = self._validate_input_hashes(input_hashes or {})
        output_paths = self._normalize_outputs(expected_outputs)
        intent_payload = self._intent_payload(
            round_number=round_number,
            step=step,
            input_hashes=normalized_inputs,
            output_paths=output_paths,
        )
        intent_name = self._journal_name(round_number=round_number, step=step)
        receipt_name = self._journal_name(
            round_number=round_number,
            step=step,
            receipt=True,
        )
        with self._journal_descriptor(create=True) as journal_descriptor:
            assert journal_descriptor is not None
            self._validate_existing_intent(
                journal_descriptor,
                intent_name,
                intent_payload,
            )
            if not self._outputs_complete(output_paths):
                raise AutoStateError("cannot complete a step with missing output")
            output_hashes = {
                self._relative_path(path): self._sha256_bytes(path.read_bytes())
                for path in output_paths
            }
            intent_bytes = self._read_journal_regular(
                journal_descriptor,
                intent_name,
            )
            payload: dict[str, object] = {
                "budget": None if budget is None else budget.to_json(),
                "config_sha256": intent_payload["config_sha256"],
                "input_hashes": normalized_inputs,
                "intent_sha256": self._sha256_bytes(intent_bytes),
                "output_hashes": output_hashes,
                "round_number": round_number,
                "status": "complete",
                "step": step,
            }
            if self._journal_entry_exists(journal_descriptor, receipt_name):
                existing = self._load_receipt(
                    journal_descriptor,
                    round_number=round_number,
                    step=step,
                    expected_intent=intent_payload,
                    verify_outputs=True,
                )
                if existing.budget != budget:
                    raise AutoStateError("completed step budget receipt changed")
                return existing
            try:
                self._journal_exclusive_write(
                    journal_descriptor,
                    receipt_name,
                    self._json_bytes(payload),
                    mode=0o600,
                )
            except FileExistsError:
                pass
            return self._load_receipt(
                journal_descriptor,
                round_number=round_number,
                step=step,
                expected_intent=intent_payload,
                verify_outputs=True,
            )

    def step_receipt(self, *, round_number: int, step: str) -> StepReceipt:
        """Load and verify one completed journal receipt."""

        intent_name = self._journal_name(round_number=round_number, step=step)
        with self._journal_descriptor(create=False) as journal_descriptor:
            if journal_descriptor is None:
                raise AutoStateError("auto-evolve journal does not exist")
            payload = self._load_journal_json_record(
                journal_descriptor,
                intent_name,
                label="step intent",
            )
            return self._load_receipt(
                journal_descriptor,
                round_number=round_number,
                step=step,
                expected_intent=payload,
                verify_outputs=True,
            )

    def can_reconcile_interrupted(self) -> bool:
        """Return whether every unfinished intent has all outputs to validate."""

        with self._journal_descriptor(create=False) as journal_descriptor:
            if journal_descriptor is None:
                return True
            try:
                names = sorted(os.listdir(journal_descriptor))
            except OSError as exc:
                raise AutoStateError("auto-evolve journal cannot be listed") from exc
            for intent_name in names:
                if (
                    not intent_name.startswith("round-")
                    or not intent_name.endswith(".json")
                    or intent_name.endswith(".receipt.json")
                ):
                    continue
                payload = self._load_journal_json_record(
                    journal_descriptor,
                    intent_name,
                    label="step intent",
                )
                round_number, step, output_paths = self._intent_identity(payload)
                receipt_name = self._journal_name(
                    round_number=round_number,
                    step=step,
                    receipt=True,
                )
                if self._journal_entry_exists(journal_descriptor, receipt_name):
                    continue
                if not self._outputs_complete(output_paths):
                    return False
        return True

    def intent_path(self, *, round_number: int, step: str) -> Path:
        return self.journal_root / self._journal_name(
            round_number=round_number,
            step=step,
        )

    def receipt_path(self, *, round_number: int, step: str) -> Path:
        return self.journal_root / self._journal_name(
            round_number=round_number,
            step=step,
            receipt=True,
        )

    def _intent_payload(
        self,
        *,
        round_number: int,
        step: str,
        input_hashes: Mapping[str, str],
        output_paths: Sequence[Path],
    ) -> dict[str, object]:
        return {
            "config_sha256": self.load().config_sha256,
            "expected_outputs": [self._relative_path(path) for path in output_paths],
            "input_hashes": dict(sorted(input_hashes.items())),
            "round_number": round_number,
            "status": "in_progress",
            "step": step,
        }

    def _validate_existing_intent(
        self,
        journal_descriptor: int,
        name: str,
        expected: Mapping[str, object],
    ) -> None:
        if (
            self._load_journal_json_record(
                journal_descriptor,
                name,
                label="step intent",
            )
            != expected
        ):
            raise AutoStateError("step intent differs from its locked inputs")

    def _load_receipt(
        self,
        journal_descriptor: int,
        *,
        round_number: int,
        step: str,
        expected_intent: Mapping[str, object],
        verify_outputs: bool,
    ) -> StepReceipt:
        receipt_name = self._journal_name(
            round_number=round_number,
            step=step,
            receipt=True,
        )
        payload = self._load_journal_json_record(
            journal_descriptor,
            receipt_name,
            label="step receipt",
        )
        try:
            output_hashes = payload["output_hashes"]
            input_hashes = payload["input_hashes"]
            if not isinstance(output_hashes, dict) or not isinstance(
                input_hashes, dict
            ):
                raise ValueError
            if (
                payload["status"] != "complete"
                or payload["round_number"] != round_number
                or payload["step"] != step
                or payload["config_sha256"] != expected_intent["config_sha256"]
                or input_hashes != expected_intent["input_hashes"]
            ):
                raise ValueError
            intent_name = self._journal_name(round_number=round_number, step=step)
            if payload["intent_sha256"] != self._sha256_bytes(
                self._read_journal_regular(journal_descriptor, intent_name)
            ):
                raise ValueError
            normalized_outputs = {
                str(key): str(value) for key, value in output_hashes.items()
            }
            expected_outputs = expected_intent["expected_outputs"]
            if not isinstance(expected_outputs, list) or not all(
                isinstance(value, str) for value in expected_outputs
            ):
                raise ValueError
            if set(normalized_outputs) != set(expected_outputs):
                raise ValueError
            if verify_outputs:
                for relative, expected_hash in normalized_outputs.items():
                    path = self.root / relative
                    if (
                        not self._regular_file(path)
                        or self._sha256_bytes(path.read_bytes()) != expected_hash
                    ):
                        raise AutoStateError("completed step output hash changed")
            budget_payload = payload.get("budget")
            budget = (
                None
                if budget_payload is None
                else StepBudgetUsage.from_json(budget_payload)
            )
            return StepReceipt(
                round_number=round_number,
                step=step,
                config_sha256=str(payload["config_sha256"]),
                input_hashes={
                    str(key): str(value) for key, value in input_hashes.items()
                },
                output_hashes=normalized_outputs,
                intent_sha256=str(payload["intent_sha256"]),
                budget=budget,
            )
        except AutoStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise AutoStateError("step completion receipt is invalid") from exc

    def _intent_identity(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, str, tuple[Path, ...]]:
        try:
            round_number = payload["round_number"]
            step = payload["step"]
            expected = payload["expected_outputs"]
            if type(round_number) is not int or not isinstance(step, str):
                raise ValueError
            if not isinstance(expected, list) or not all(
                isinstance(value, str) for value in expected
            ):
                raise ValueError
            self._validate_step_identity(round_number=round_number, step=step)
            paths = tuple(self.root / value for value in expected)
            self._normalize_outputs(paths)
            return round_number, step, paths
        except (KeyError, TypeError, ValueError) as exc:
            raise AutoStateError("step intent is invalid") from exc

    def _normalize_outputs(self, paths: Sequence[Path]) -> tuple[Path, ...]:
        if not paths:
            raise AutoStateError("auto-evolve step requires an expected output")
        normalized: list[Path] = []
        for path in paths:
            candidate = path.resolve(strict=False)
            try:
                candidate.relative_to(self.root)
            except ValueError as exc:
                raise AutoStateError("step output escapes the experiment root") from exc
            if path.is_symlink():
                raise AutoStateError("step output cannot be a symlink")
            normalized.append(candidate)
        if len(normalized) != len(set(normalized)):
            raise AutoStateError("step outputs must be unique")
        return tuple(normalized)

    @staticmethod
    def _validate_step_identity(*, round_number: int, step: str) -> None:
        if step not in _SAFE_STEPS or round_number < 1:
            raise AutoStateError("auto-evolve intent identity is invalid")

    @staticmethod
    def _validate_input_hashes(values: Mapping[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if (
                not key
                or not key.replace("_", "").isalnum()
                or len(value) != _SHA256_LENGTH
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise AutoStateError("step input hash is invalid")
            normalized[key] = value
        return dict(sorted(normalized.items()))

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve(strict=False).relative_to(self.root).as_posix()
        except ValueError as exc:
            raise AutoStateError("journal path escapes the experiment root") from exc

    @staticmethod
    def _outputs_complete(paths: Sequence[Path]) -> bool:
        return all(AutoStateStore._regular_file(path) for path in paths)

    @staticmethod
    def _regular_file(path: Path) -> bool:
        return path.is_file() and not path.is_symlink()

    @staticmethod
    def _json_bytes(payload: Mapping[str, object]) -> bytes:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _journal_name(
        *,
        round_number: int,
        step: str,
        receipt: bool = False,
    ) -> str:
        AutoStateStore._validate_step_identity(
            round_number=round_number,
            step=step,
        )
        suffix = ".receipt.json" if receipt else ".json"
        return f"round-{round_number:03d}-{step}{suffix}"

    def _open_root_descriptor(self) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lexical = self.root
        descriptor: int | None = None
        try:
            descriptor = os.open(lexical.anchor, flags)
            for component in lexical.parts[1:]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise AutoStateError("auto-evolve root must be a real directory")
            return descriptor
        except AutoStateError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise AutoStateError(
                "auto-evolve root contains a symlink or unsafe component"
            ) from exc

    @staticmethod
    def _open_journal_descriptor(
        root_descriptor: int,
        *,
        create: bool,
    ) -> int | None:
        if create:
            try:
                os.mkdir(".journal", mode=0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise AutoStateError("auto-evolve journal cannot be created") from exc
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(".journal", flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            if not create:
                return None
            raise AutoStateError("auto-evolve journal disappeared") from None
        except OSError as exc:
            raise AutoStateError(
                "auto-evolve journal must be a real directory"
            ) from exc
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise AutoStateError("auto-evolve journal must be a real directory")
        return descriptor

    @contextmanager
    def _journal_descriptor(self, *, create: bool) -> Iterator[int | None]:
        root_descriptor = self._open_root_descriptor()
        descriptor: int | None = None
        try:
            if self._active_journal_descriptor is not None:
                current = self._open_journal_descriptor(
                    root_descriptor,
                    create=False,
                )
                if current is None:
                    raise AutoStateError("active auto-evolve journal disappeared")
                try:
                    active_stat = os.fstat(self._active_journal_descriptor)
                    current_stat = os.fstat(current)
                    if (active_stat.st_dev, active_stat.st_ino) != (
                        current_stat.st_dev,
                        current_stat.st_ino,
                    ):
                        raise AutoStateError("active auto-evolve journal was replaced")
                finally:
                    os.close(current)
                descriptor = os.dup(self._active_journal_descriptor)
            else:
                descriptor = self._open_journal_descriptor(
                    root_descriptor,
                    create=create,
                )
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(root_descriptor)

    @staticmethod
    def _journal_entry_exists(descriptor: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise AutoStateError(
                "auto-evolve journal entry cannot be inspected"
            ) from exc
        return True

    @staticmethod
    def _read_journal_regular(descriptor: int, name: str) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_descriptor = os.open(name, flags, dir_fd=descriptor)
        except OSError as exc:
            raise AutoStateError("auto-evolve journal record cannot be opened") from exc
        try:
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise AutoStateError(
                    "auto-evolve journal record must be a regular file"
                )
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(file_descriptor)

    @staticmethod
    def _load_journal_json_record(
        descriptor: int,
        name: str,
        *,
        label: str,
    ) -> dict[str, Any]:
        try:
            raw = AutoStateStore._read_journal_regular(descriptor, name)
            payload = json.loads(raw)
        except (AutoStateError, UnicodeError, json.JSONDecodeError) as exc:
            raise AutoStateError(f"{label} is invalid") from exc
        if not isinstance(payload, dict) or AutoStateStore._json_bytes(payload) != raw:
            raise AutoStateError(f"{label} is not canonical")
        return payload

    @staticmethod
    def _sha256_bytes(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _journal_exclusive_write(
        journal_descriptor: int,
        name: str,
        payload: bytes,
        *,
        mode: int,
    ) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, mode, dir_fd=journal_descriptor)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.fsync(journal_descriptor)

    def _create_root(self) -> None:
        if self.root.is_symlink() or self.root.absolute() != self.root.resolve():
            raise AutoStateError("auto-evolve root cannot contain symlinks")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise AutoStateError("auto-evolve root must be a directory")

    @staticmethod
    def _read_regular(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise AutoStateError("auto-evolve record must be a regular file")
        return path.read_bytes()

    @staticmethod
    def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, mode)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


__all__ = [
    "AutoStateError",
    "AutoStateStore",
    "StepBudgetUsage",
    "StepReceipt",
]

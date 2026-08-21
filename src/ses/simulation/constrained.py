"""Constrained, offline user simulation for reproducible evaluations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

_FORBIDDEN_PATTERNS = (
    re.compile(r"\bgold\b", re.IGNORECASE),
    re.compile(r"\bjudge\b", re.IGNORECASE),
    re.compile(r"\breference (?:step|trace|trajectory|answer)\b", re.IGNORECASE),
    re.compile(r"\b(?:call|invoke|use)\s+[a-z][a-z0-9_]*", re.IGNORECASE),
)
_WRITE_TOOL_WORDS = frozenset(
    {"create", "delete", "process", "refund", "return", "update", "write"}
)
_REQUEST_CUES = re.compile(
    r"\b(?:can|could|give|may|need|please|provide|require|send|share|specify|"
    r"tell|what|which|would)\b",
    re.IGNORECASE,
)


class SimulatorTurnKind(StrEnum):
    """The only two outputs a simulator may produce."""

    MESSAGE = "message"
    END = "end"


@dataclass(frozen=True, slots=True)
class SimulatorTurn:
    """One user message or an explicit end signal."""

    kind: SimulatorTurnKind
    message: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is SimulatorTurnKind.MESSAGE) != (self.message is not None):
            raise ValueError("message turns require text and end turns forbid text")
        if self.message is not None and not self.message.strip():
            raise ValueError("simulator messages must not be blank")


@dataclass(frozen=True, slots=True)
class UserIntent:
    """Private user want plus facts that the simulator may say aloud."""

    want: str
    allowed_facts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.want.strip():
            raise ValueError("user intent must not be blank")
        if any(
            not key.strip() or not value.strip()
            for key, value in self.allowed_facts.items()
        ):
            raise ValueError("allowed facts require non-blank names and values")


def _safe_want(value: str) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", value.strip())
    safe = [
        sentence.strip()
        for sentence in sentences
        if sentence.strip()
        and not any(pattern.search(sentence) for pattern in _FORBIDDEN_PATTERNS)
    ]
    if not safe:
        raise ValueError("intent has no safe user-facing want")
    return " ".join(safe)


def _requests_fact(messages: Sequence[str], key: str) -> bool:
    label = key.replace("_", " ").casefold()
    subject = label.removesuffix(" id")
    terms = {label}
    identifier_fact = label.endswith(" id")
    if identifier_fact:
        terms.update({f"{subject} identifier", f"{subject} number"})
    for message in messages:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", message.casefold())
        for sentence in sentences:
            if identifier_fact and re.search(
                rf"\b(?:what|which)\b[^?.!]{{0,40}}\b{re.escape(subject)}\b",
                sentence,
            ):
                return True
            if not any(
                re.search(rf"\b{re.escape(term)}\b", sentence) for term in terms
            ):
                continue
            if "?" in sentence or _REQUEST_CUES.search(sentence):
                return True
    return False


class ConstrainedUserSimulator:
    """Reveal a want and allow-listed facts without any Shop tool capability."""

    def __init__(
        self,
        intent: UserIntent,
        *,
        allowed_tools: Sequence[str] = (),
    ) -> None:
        if allowed_tools:
            raise ValueError("the user simulator cannot receive Shop write tools")
        self._intent = intent
        self._safe_initial_message = _safe_want(intent.want)
        self._facts = tuple(sorted(intent.allowed_facts.items()))
        self._next_fact = 0
        self._started = False

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        """The simulator has no callable tools, especially no Shop writes."""
        return ()

    def next_turn(self, assistant_messages: Sequence[str]) -> SimulatorTurn:
        """Return the next allow-listed user message or stop."""
        if not self._started:
            self._started = True
            return SimulatorTurn(SimulatorTurnKind.MESSAGE, self._safe_initial_message)
        if (
            self._next_fact < len(self._facts)
            and assistant_messages
            and _requests_fact(assistant_messages, self._facts[self._next_fact][0])
        ):
            key, value = self._facts[self._next_fact]
            self._next_fact += 1
            label = key.replace("_", " ")
            return SimulatorTurn(SimulatorTurnKind.MESSAGE, f"The {label} is {value}.")
        return SimulatorTurn(SimulatorTurnKind.END)


class FakeSimulator(ConstrainedUserSimulator):
    """Named deterministic simulator used by offline tests and examples."""

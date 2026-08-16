"""The first-course Creator seam and its deterministic offline implementation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .installer import normalized_skill_sha256

COURSE_CREATOR_PROMPT = """\
You are the Lesson 1 Skill Creator.

Use only the approved seed traces supplied to you. They are successful seed
traces, not hidden evaluation material. Infer reusable return-support behavior
from them, and write one concise SKILL.md plus optional references.

Do not copy case IDs, order IDs, customer data, fixed answers, gold, eval
material, traces, hidden data, credentials, or unsupported tools into the
Skill. The Skill must explain when it applies, inspect before acting, follow the
preview-then-confirm pattern, and verify the final state. Keep the result
generic enough to transfer to another return case.
"""


class CreatorError(ValueError):
    """The Creator could not produce a candidate Skill."""


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """A generated candidate before it is installed into an Agent workspace."""

    source: Path
    version: str
    sha256: str


class FakeCreator:
    """Generate a fixed, safe candidate without reading the network or Keys."""

    def __init__(self, *, failure: str | None = None) -> None:
        self._failure = failure

    def create(
        self,
        output_dir: Path,
        *,
        seed_traces: Sequence[Path],
    ) -> SkillCandidate:
        """Create the course candidate in a new directory.

        ``seed_traces`` is deliberately accepted at the seam so a live Creator
        can later consume the approved seed set. The offline implementation
        stays deterministic and does not open those files.
        """
        del seed_traces
        if self._failure is not None:
            raise CreatorError(self._failure)
        if output_dir.exists():
            raise CreatorError(f"candidate directory already exists: {output_dir}")
        output_dir.mkdir(parents=True)
        (output_dir / "references").mkdir()
        (output_dir / "SKILL.md").write_text(
            """---\nname: return-support-demo\ndescription: Apply to customer return requests.\nversion: demo-v1\n---\n\n# Return support demo\n\n- Inspect the order and the applicable return policy before changing state.\n- Preview the return with the customer's reason before confirming it.\n- Confirm only after the customer-visible amount and item match the request.\n- Report what the tools actually returned and verify the terminal state.\n- Do not invent case-specific answers or claim a refund without tool evidence.\n""",
            encoding="utf-8",
        )
        (output_dir / "references" / "return-checklist.md").write_text(
            """# Generic return checklist\n\n1. Identify the requested item from the conversation.\n2. Read policy before making a change.\n3. Preview, then confirm, and inspect the result.\n""",
            encoding="utf-8",
        )
        return SkillCandidate(
            source=output_dir,
            version="demo-v1",
            sha256=normalized_skill_sha256(output_dir),
        )

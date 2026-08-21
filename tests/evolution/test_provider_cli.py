from __future__ import annotations

from pathlib import Path

import pytest

from ses.cli import evolution

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "fixtures" / "seed" / "skill" / "v0"
EVIDENCE = ROOT / "tests" / "fixtures" / "evolution" / "synthetic-failure-evidence.json"


def test_evolve_live_explicit_provider_never_falls_back_to_another_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CHATANYWHERE_API_KEY", raising=False)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "wrong-provider-key")

    def must_not_start_workflow(**_kwargs: object) -> None:
        raise AssertionError("paid workflow must not start without the selected key")

    monkeypatch.setattr(evolution, "run_evolution_workflow", must_not_start_workflow)

    result = evolution.evolve_main(
        [
            "--parent",
            str(PARENT),
            "--evidence",
            str(EVIDENCE),
            "--output",
            str(tmp_path / "candidate"),
            "--mode",
            "live",
            "--provider",
            "chatanywhere",
            "--project-root",
            str(ROOT),
        ]
    )

    assert result == 1
    assert "missing CHATANYWHERE_API_KEY" in capsys.readouterr().err


def test_evolve_live_uses_runtime_default_without_cross_provider_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setenv("CHATANYWHERE_API_KEY", "wrong-provider-key")

    def must_not_start_workflow(**_kwargs: object) -> None:
        raise AssertionError("paid workflow must not start without the default key")

    monkeypatch.setattr(evolution, "run_evolution_workflow", must_not_start_workflow)

    result = evolution.evolve_main(
        [
            "--parent",
            str(PARENT),
            "--evidence",
            str(EVIDENCE),
            "--output",
            str(tmp_path / "candidate"),
            "--mode",
            "live",
            "--project-root",
            str(ROOT),
        ]
    )

    assert result == 1
    assert "missing SILICONFLOW_API_KEY" in capsys.readouterr().err

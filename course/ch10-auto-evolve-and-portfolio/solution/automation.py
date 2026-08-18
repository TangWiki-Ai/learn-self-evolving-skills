"""Lesson 10 solution delegates to production automation and reporting modules."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ses.automation.fixed import run_fixed_auto_evolve
from ses.automation.portfolio import export_portfolio
from ses.contracts import AutoEvolveState, PortfolioManifest
from ses.reporting.l3 import write_l3_html


class Lesson10Result:
    """Public outputs of the fixed/offline Lesson 10 vertical slice."""

    __slots__ = ("l3_path", "portfolio_manifest", "portfolio_root", "state")

    def __init__(
        self,
        *,
        state: AutoEvolveState,
        l3_path: Path,
        portfolio_root: Path,
        portfolio_manifest: PortfolioManifest,
    ) -> None:
        self.state = state
        self.l3_path = l3_path
        self.portfolio_root = portfolio_root
        self.portfolio_manifest = portfolio_manifest


def run_bounded(*, project_root: Path, output_root: Path) -> AutoEvolveState:
    """Run or exactly resume the production two-round fixed workflow."""

    return run_fixed_auto_evolve(
        project_root=project_root,
        output_root=output_root,
    )


def render_l3(*, experiment_root: Path, destination: Path) -> Path:
    """Render the verified aggregate records through the production L3 view."""

    return write_l3_html(experiment_root, destination)


def export_public_portfolio(
    *,
    experiment_root: Path,
    destination: Path,
    created_at: datetime,
) -> PortfolioManifest:
    """Export only allowlisted public evidence and the accepted Skill."""

    return export_portfolio(
        experiment_root,
        destination,
        created_at=created_at,
    )


def run_and_export(
    *,
    project_root: Path,
    experiment_root: Path,
    l3_path: Path,
    portfolio_root: Path,
    created_at: datetime,
) -> Lesson10Result:
    """Run two fixed rounds, one final, L3, and the public portfolio."""

    state = run_bounded(project_root=project_root, output_root=experiment_root)
    report = render_l3(experiment_root=experiment_root, destination=l3_path)
    manifest = export_public_portfolio(
        experiment_root=experiment_root,
        destination=portfolio_root,
        created_at=created_at,
    )
    return Lesson10Result(
        state=state,
        l3_path=report,
        portfolio_root=portfolio_root,
        portfolio_manifest=manifest,
    )

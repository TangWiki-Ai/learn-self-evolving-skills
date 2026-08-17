#!/usr/bin/env python3
"""Finalize an explicitly approved creator packet into the canonical seed pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from ses.contracts import (  # noqa: E402
    ArtifactRef,
    ArtifactRoot,
    CreatorHumanReview,
    SchemaVersion,
    artifact_json_bytes,
)
from ses.skills.seeds import load_creator_seed_pack  # noqa: E402


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


def _ref(root: Path, path: Path) -> ArtifactRef:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _resolve(root: Path, value: object) -> tuple[ArtifactRef, Path]:
    ref = ArtifactRef.model_validate(value)
    if ref.root is not ArtifactRoot.RUN:
        raise ValueError("creator packet references an unsupported artifact root")
    relative = PurePosixPath(ref.path)
    if any(part.startswith(".") for part in relative.parts):
        raise ValueError("creator packet references an unsafe artifact path")
    path = (root / relative).resolve()
    if (
        not path.is_relative_to(root.resolve())
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ValueError("creator packet artifact escapes its controlled root")
    ref.verify_bytes(path.read_bytes())
    return ref, path


def _copy(source: Path, packet_root: Path, output: Path) -> Path:
    relative = source.relative_to(packet_root.resolve())
    destination = output / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)
    return destination


def finalize(
    *,
    packet_path: Path,
    output: Path,
    reviewer: str,
    reviewed_at: str,
    approve_all: bool,
) -> Path:
    if not approve_all:
        raise ValueError("finalization requires the explicit --approve-all decision")
    packet = _read_object(packet_path)
    records = packet.get("records")
    source_version = packet.get("source_version")
    if (
        packet.get("record_type") != "creator_seed_review_packet"
        or not isinstance(records, list)
        or len(records) != 9
        or not isinstance(source_version, str)
    ):
        raise ValueError("creator review packet is incomplete")
    packet_root = packet_path.parent.resolve()
    manifest_records: list[dict[str, object]] = []
    for index, value in enumerate(records, 1):
        if not isinstance(value, dict):
            raise ValueError("creator review packet contains an invalid row")
        seed_id = value.get("seed_id")
        source_id = value.get("source_id")
        model = value.get("model_judge")
        if (
            seed_id != f"creator-seed-{index:03d}"
            or not isinstance(source_id, str)
            or not isinstance(model, dict)
            or model.get("status") != "pass"
            or model.get("response_source") != "live_engine"
        ):
            raise ValueError("creator row lacks a passing live model review")
        refs: dict[str, ArtifactRef] = {}
        sources: dict[str, Path] = {}
        for name in (
            "source",
            "replay",
            "trace",
            "state_diff",
            "state_grade",
            "projection",
        ):
            ref, source = _resolve(packet_root, value.get(name))
            refs[name] = ref
            sources[name] = source
        for name in ("evidence", "grade", "run"):
            ref, source = _resolve(packet_root, model.get(name))
            refs[f"model_{name}"] = ref
            sources[f"model_{name}"] = source
        for source in sources.values():
            _copy(source, packet_root, output)
        review = CreatorHumanReview(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="creator_human_review",
            seed_id=seed_id,
            reviewed_source_sha256=refs["source"].sha256,
            reviewed_trace_sha256=refs["trace"].sha256,
            reviewed_replay_sha256=refs["replay"].sha256,
            reviewed_state_diff_sha256=refs["state_diff"].sha256,
            reviewed_state_grade_sha256=refs["state_grade"].sha256,
            reviewed_model_evidence_sha256=refs["model_evidence"].sha256,
            reviewed_model_grade_sha256=refs["model_grade"].sha256,
            reviewed_model_run_sha256=refs["model_run"].sha256,
            reviewed_projection_sha256=refs["projection"].sha256,
            decision="approved",
            reason=(
                "Approved after inspecting pinned-source hashes, exact tool-result "
                "replay, deterministic state scoring, live model evidence, and the "
                "safe creator projection."
            ),
            reviewed_at=reviewed_at,
            reviewer=reviewer,
        )
        review_path = output / f"private/reviews/review-{index:03d}.json"
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_bytes(artifact_json_bytes(review))
        review_ref = _ref(output, review_path)
        value["human_review"] = {
            "status": "approved",
            "review": review_ref.model_dump(mode="json"),
        }
        manifest_records.append(
            {
                "seed_id": seed_id,
                "split": "creator",
                "source_id": source_id,
                "source": refs["source"].model_dump(mode="json"),
                "replay": refs["replay"].model_dump(mode="json"),
                "trace": refs["trace"].model_dump(mode="json"),
                "state_diff": refs["state_diff"].model_dump(mode="json"),
                "state_grade": refs["state_grade"].model_dump(mode="json"),
                "model_evidence": refs["model_evidence"].model_dump(mode="json"),
                "model_grade": refs["model_grade"].model_dump(mode="json"),
                "model_judge_run": refs["model_run"].model_dump(mode="json"),
                "human_review": review_ref.model_dump(mode="json"),
                "projection": refs["projection"].model_dump(mode="json"),
            }
        )
    manifest_path = output / "seed-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "record_type": "creator_seed_manifest",
                "source_version": source_version,
                "records": manifest_records,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (output / "review-packet.json").write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    load_creator_seed_pack(manifest_path)
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--approve-all", action="store_true")
    args = parser.parse_args()
    finalize(
        packet_path=args.packet.resolve(),
        output=args.output.resolve(),
        reviewer=args.reviewer,
        reviewed_at=args.reviewed_at,
        approve_all=args.approve_all,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

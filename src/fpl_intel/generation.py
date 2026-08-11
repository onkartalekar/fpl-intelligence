"""Transactional publication of complete dashboard generations."""

import json
from pathlib import Path
import shutil
import uuid

from .fpl_data import save_json


def _safe_generation_dir(root):
    root = Path(root).resolve()
    pointer = root / "data" / "current-generation.json"
    if not pointer.exists():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    generation_id = str(payload.get("generation_id") or "")
    if not generation_id or Path(generation_id).name != generation_id:
        return None
    generations_root = (root / "data" / "generations").resolve()
    candidate = (generations_root / generation_id).resolve()
    if candidate.parent != generations_root or not (candidate / "manifest.json").is_file():
        return None
    return candidate


def resolve_artifact(root, filename):
    """Resolve an artifact from the authoritative generation, then legacy paths."""
    if not filename or Path(filename).name != filename:
        raise ValueError("Artifact filename must be a basename")
    root = Path(root).resolve()
    generation = _safe_generation_dir(root)
    if generation is not None:
        candidate = generation / filename
        if candidate.is_file():
            return candidate
    return root / "data" / filename


def publish_generation(root, generated_at, json_artifacts):
    """Stage a complete generation and switch one authoritative pointer last.

    Legacy root-level files are still published for compatibility, but all
    application consumers resolve the current-generation pointer. If any
    compatibility write fails, the pointer remains on the previous complete
    generation.

    Issue #120: this used to also publish a `dashboard.html` snapshot, but nothing
    reads it anymore -- the server renders the dashboard fresh from
    `dashboard-state.json` on every request instead of serving a static file that
    could go stale relative to newly-deployed code. Dropped rather than kept as a
    dead artifact, to avoid a future maintainer mistaking it for load-bearing.
    """
    root = Path(root).resolve()
    data_root = root / "data"
    generations_root = data_root / "generations"
    generations_root.mkdir(parents=True, exist_ok=True)
    safe_stamp = "".join(character if character.isalnum() else "-" for character in str(generated_at)).strip("-")
    generation_id = f"{safe_stamp or 'generation'}-{uuid.uuid4().hex[:12]}"
    staged = generations_root / generation_id
    staged.mkdir()
    try:
        for filename, payload in json_artifacts.items():
            if not filename or Path(filename).name != filename:
                raise ValueError("Artifact filename must be a basename")
            save_json(staged / filename, payload)
        save_json(
            staged / "manifest.json",
            {
                "generation_id": generation_id,
                "generated_at": generated_at,
                "json_artifacts": sorted(json_artifacts),
            },
        )

        for filename, payload in json_artifacts.items():
            save_json(data_root / filename, payload)
        save_json(
            data_root / "current-generation.json",
            {"generation_id": generation_id, "generated_at": generated_at},
        )
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return generation_id

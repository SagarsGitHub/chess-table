"""Durable ownership guard shared by GUI replay and ordinary CLI play.

The physical checkpoint remains authoritative for motion progress. This small
adjacent marker preserves replay ownership when the GUI's preview is gone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from .game_errors import GameStateError


def replay_marker_path(checkpoint: Path) -> Path:
    """Return the sidecar path for this exact checkpoint filename."""
    checkpoint = Path(checkpoint)
    return checkpoint.with_name(checkpoint.stem + ".replay.json")


def _validate_marker(marker: object) -> dict:
    if (not isinstance(marker, dict) or type(marker.get("schema_version")) is not int or
            marker["schema_version"] != 1 or
            not isinstance(marker.get("host_game_id"), str) or not marker["host_game_id"]):
        raise GameStateError(
            "replay ownership marker is invalid; preserve it and inspect before restarting"
        )
    return marker


def read_replay_marker(checkpoint: Path) -> dict | None:
    """Read valid ownership, fail closed on corrupt data, or return absent."""
    try:
        text = replay_marker_path(checkpoint).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise GameStateError(
            "replay ownership marker is invalid; preserve it and inspect before restarting"
        ) from exc
    try:
        marker = json.loads(text)
    except ValueError as exc:
        raise GameStateError(
            "replay ownership marker is invalid; preserve it and inspect before restarting"
        ) from exc
    return _validate_marker(marker)


def save_replay_marker(checkpoint: Path, marker: dict) -> None:
    """Fsync the complete marker before atomically replacing prior ownership."""
    _validate_marker(marker)
    path = replay_marker_path(checkpoint)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + str(uuid4()) + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

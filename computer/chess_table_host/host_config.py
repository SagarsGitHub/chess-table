"""Validate host settings shared by the CLI and browser GUI."""

from __future__ import annotations

import json
from pathlib import Path

from .game_errors import GameStateError


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def _load_config(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GameStateError(f"cannot read game config {path}: {exc}") from exc
    required = {
        "serial_port", "stockfish_path", "moving_diameter_mm",
        "standing_diameter_mm", "clearance_margin_mm",
    }
    if not isinstance(config, dict) or not required.issubset(config):
        raise GameStateError(f"game config must contain {', '.join(sorted(required))}")
    if any(not isinstance(config[key], str) or not config[key].strip()
           for key in ("serial_port", "stockfish_path")):
        raise GameStateError("serial_port and stockfish_path must be nonempty strings")
    for key in ("moving_diameter_mm", "standing_diameter_mm", "clearance_margin_mm"):
        if not isinstance(config[key], (int, float)) or isinstance(config[key], bool):
            raise GameStateError(f"{key} must be a number")
    if (config["moving_diameter_mm"] <= 0 or config["standing_diameter_mm"] <= 0 or
            config["clearance_margin_mm"] < 0):
        raise GameStateError("diameters must be positive and margin cannot be negative")
    if type(config.get("allow_tight_routes", False)) is not bool:
        raise GameStateError("allow_tight_routes must be a boolean")
    config["allow_tight_routes"] = config.get("allow_tight_routes", False)
    return config

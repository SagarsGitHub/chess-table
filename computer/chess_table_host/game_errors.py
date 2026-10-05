"""Errors shared by checkpoint and physical inventory modules."""


class GameStateError(ValueError):
    """Saved game state is invalid or inconsistent."""

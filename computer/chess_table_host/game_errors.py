"""Errors shared by checkpoint validation, planning and physical execution."""


class GameStateError(ValueError):
    """Saved game state is invalid or inconsistent."""


class ReconciliationRequired(GameStateError):
    """A physical action has an unknown result and must be inspected."""


class MoveDeferred(GameStateError):
    """The operator paused before a physical action began."""

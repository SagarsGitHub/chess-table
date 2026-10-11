"""Thread-safe checkpoint snapshots and read-only browser presentation.

Snapshot reads never consult the mutable runner during a physical transfer.
The cached view bridges brief filesystem visibility gaps and disables actions.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any

import chess

from .game import GameState, JsonGameStore


class SynchronizedGameStore(JsonGameStore):
    """Serialize GUI reads with checkpoint writes and keep a brief safe view.

    DrvFS may report the target path absent for a short time after os.replace.
    A cached *copy of the last successfully saved* state keeps read-only GUI
    polling available while the path reappears. The controller disables action
    buttons whenever it serves this cached view.
    """

    def __init__(self, path: str | Path):
        super().__init__(path)
        self._io_lock = threading.RLock()
        self._last_state: GameState | None = None
        self._last_save_at = 0.0

    def save(self, state: GameState) -> None:
        with self._io_lock:
            super().save(state)
            self._last_state = deepcopy(state)
            self._last_save_at = time.monotonic()

    def load(self) -> GameState:
        with self._io_lock:
            state = super().load()
            self._last_state = deepcopy(state)
            return state

    def read_snapshot(self) -> tuple[GameState, bool]:
        with self._io_lock:
            try:
                state = super().load()
            except FileNotFoundError:
                if (self._last_state is None or
                        time.monotonic() - self._last_save_at > 2.0):
                    raise
                return deepcopy(self._last_state), True
            self._last_state = deepcopy(state)
            return state, False


def _san_history(state: GameState) -> list[str]:
    """Replay committed UCI moves to derive notation for the browser."""
    board = chess.Board(state.initial_fen)
    history = []
    for uci in state.moves:
        move = board.parse_uci(uci)
        history.append(board.san(move))
        board.push(move)
    return history


def _active_action(state: GameState) -> dict[str, Any] | None:
    """Describe the next saved transfer without reading the mutable runner."""
    active = state.reset or state.pending
    if active is None:
        return None
    value = active.to_json()
    index = active.next_action
    return {
        "kind": "reset" if state.reset else "move",
        "uci": state.pending.uci if state.pending else None,
        "phase": active.phase,
        "next_action": index,
        "action_number": index + 1,
        "total_actions": len(active.actions),
        "action": value["actions"][index],
        "active_route": list(active.active_route) if active.active_route else None,
        "castle_clearance_override": bool(
            state.pending and state.pending.castle_clearance_override
        ),
    }

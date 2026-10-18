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
from .game_errors import GameStateError
from .reset_receipt import RESET_TERMINAL, RemoteResetReceipt


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
            # An API reset intent may be registered while the sole worker is
            # finishing a chess carry. Merge only this metadata into its next
            # checkpoint; never replace the worker's physical snapshot.
            try:
                stored = super().load()
            except FileNotFoundError:
                # DrvFS can briefly hide an atomically replaced file. The
                # last successful snapshot still contains every registered
                # receipt, even when this worker's in-memory copy predates it.
                stored = self._last_state
            if stored is not None:
                for key, receipt in stored.remote_reset_receipts.items():
                    current = state.remote_reset_receipts.get(key)
                    if current is None:
                        if receipt.state in RESET_TERMINAL or stored.host_game_id == state.host_game_id:
                            state.remote_reset_receipts[key] = deepcopy(receipt)
                    elif not current.same_intent(receipt):
                        raise GameStateError("reset request_id differs from the saved intent")
                    elif receipt.state in RESET_TERMINAL or (
                        receipt.state == "in_flight" and current.state == "prepared"
                    ):
                        state.remote_reset_receipts[key] = deepcopy(receipt)
            if state.reset is not None:
                for receipt in state.remote_reset_receipts.values():
                    if receipt.state == "prepared":
                        receipt.state = "in_flight"
            super().save(state)
            self._last_state = deepcopy(state)
            self._last_save_at = time.monotonic()

    def register_reset_receipt(self, receipt: RemoteResetReceipt, validate) -> None:
        """Fence and register against one atomic snapshot before motion starts."""
        with self._io_lock:
            state = super().load()
            validate(state)
            state.remote_reset_receipts[receipt.request_id] = receipt
            self.save(state)

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

"""Inspection-based reconciliation and commits for the shared GameRunner.

These methods use the runner's state and store directly. A destination outcome
requires operator inspection; controller completion cannot sense delivery.
"""

from __future__ import annotations

import chess

from .checkpoint import GameState, PendingMove, PendingReset
from .game_errors import GameStateError
from .physical import _apply_action, _board_pieces, _replay_physical


class GameRecoveryMixin:
    """Internal methods of GameRunner; owns no additional state or worker."""

    def _resolve_inspected_action(
        self, pending: PendingMove | PendingReset, outcome: str,
    ) -> int:
        """Advance only a verified destination, retaining the original event index."""
        if outcome not in {"source", "destination"}:
            raise ValueError("outcome must be source or destination")
        action_index = pending.next_action
        if outcome == "destination":
            _apply_action(
                self.state.physical_squares, self.state.physical_parking,
                pending.actions[action_index],
            )
            pending.next_action += 1
        pending.phase = "ready"
        if isinstance(pending, PendingMove) and pending.remote_request_id is not None:
            self.state.remote_receipts[pending.remote_request_id].state = "prepared"
        pending.active_route = None
        return action_index

    def resolve_uncertain_reset(self, outcome: str) -> None:
        """Record an inspected source or destination result for the active reset carry."""
        pending = self.state.reset
        if pending is None or pending.phase == "ready":
            raise GameStateError("no uncertain reset transfer to resolve")
        action_index = self._resolve_inspected_action(pending, outcome)
        self.state.events.append({
            "event": "reset_action_reconciled", "action": action_index,
            "outcome": outcome,
        })
        if pending.next_action == len(pending.actions):
            self._finish_reset()
        else:
            self.store.save(self.state)

    def abort_reset_after_restore(self) -> None:
        """Discard a reset after restoring the pre-reset position and centers."""
        if self.state.reset is None:
            raise GameStateError("no pending board reset")
        self.state.physical_squares = self.state.expected_physical_squares()
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.reset = None
        self.state.events.append({"event": "reset_aborted_after_manual_restore"})
        self.store.save(self.state)

    def _finish_reset(self) -> None:
        if (self.state.physical_squares != _board_pieces(chess.Board()) or
                any(self.state.physical_parking)):
            raise GameStateError("reset cannot finish before every piece is home")
        ply_count = len(self.state.moves)
        self.state = GameState.new()
        self.state.events.append({"event": "board_reset_completed", "previous_plies": ply_count})
        self.store.save(self.state)

    def resolve_uncertain_action(self, outcome: str) -> None:
        """Record an operator-verified source or destination result.

        ``source`` means the action has not happened (or the operator restored
        it to that state). ``destination`` means it has fully happened. The
        caller must inspect the entire affected path and pieces first.
        """
        pending = self.state.pending
        if pending is None or pending.phase == "ready":
            raise GameStateError("no uncertain action to resolve")
        action_index = self._resolve_inspected_action(pending, outcome)
        self.state.events.append({
            "event": "action_reconciled", "uci": pending.uci,
            "action": action_index,
            "outcome": outcome,
        })
        self._commit_if_complete()
        self.store.save(self.state)

    def abort_pending_after_restore(self) -> None:
        """Discard a move after restoring all pieces to their pre-move positions.

        Every temporarily offset piece must also be returned to its center.
        """
        if self.state.pending is None:
            raise GameStateError("no pending move")
        uci = self.state.pending.uci
        if self.state.pending.remote_request_id is not None:
            self.state.remote_receipts[self.state.pending.remote_request_id].state = "aborted"
        self.state.physical_squares = self.state.expected_physical_squares()
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.pending = None
        self.state.events.append({"event": "move_aborted_after_manual_restore", "uci": uci})
        self.store.save(self.state)

    def cancel_unstarted_remote_move(self) -> bool:
        """Tombstone a remote move only when no physical action has begun."""
        pending = self.state.pending
        if (pending is None or pending.remote_request_id is None or
                pending.next_action != 0 or pending.phase != "ready"):
            return False
        self.state.remote_receipts[pending.remote_request_id].state = "aborted"
        self.state.events.append({
            "event": "remote_move_cancelled_before_motion",
            "uci": pending.uci,
            "request_id": pending.remote_request_id,
        })
        self.state.pending = None
        self.store.save(self.state)
        return True

    def _commit_if_complete(self) -> None:
        pending = self.state.pending
        if pending is None or pending.next_action < len(pending.actions):
            return
        resulting_board, expected_physical = _replay_physical(
            self.state.initial_fen, [*self.state.moves, pending.uci]
        )
        if (resulting_board.fen() != pending.fen_after or
                self.state.physical_squares != expected_physical or
                tuple(self.state.physical_parking) != pending.parking_after.slots):
            raise GameStateError("confirmed actions do not match planned final position")
        self.state.board_fen = pending.fen_after
        self.state.committed_parking = pending.parking_after
        self.state.moves.append(pending.uci)
        if pending.remote_request_id is not None:
            self.state.remote_receipts[pending.remote_request_id].state = "committed"
        self.state.events.append({"event": "move_committed", "uci": pending.uci})
        self.state.pending = None

"""Saved action prefixes retain every temporary in-square piece offset."""

from __future__ import annotations

import unittest

import chess

from chess_table_host.checkpoint import GameState, PendingMove, PendingReset
from chess_table_host.game_errors import GameStateError
from chess_table_host.planner import (
    BoardSquare, ParkingInventory, PieceOffset, RookOffset, Transfer,
)


def _move_state(actions: tuple, uci: str = "g1f3") -> GameState:
    state = GameState.new()
    board = state.board()
    board.push_uci(uci)
    state.pending = PendingMove(
        uci, board.fen(), ParkingInventory.empty(), actions,
    )
    return state


def _reset_state(actions: tuple) -> GameState:
    initial = chess.Board()
    after_move = initial.copy()
    after_move.push_uci("e2e4")
    physical = {
        chess.square_name(square): piece.symbol()
        for square, piece in after_move.piece_map().items()
    }
    return GameState(
        after_move.fen(), ParkingInventory.empty(), physical, [None] * 32,
        moves=["e2e4"], reset=PendingReset(actions), initial_fen=initial.fen(),
    )


class OffsetCheckpointTests(unittest.TestCase):
    def test_move_round_trips_two_confirmed_offsets(self) -> None:
        first = PieceOffset("P", BoardSquare("f2"), "NW", "OUT", "clearance_offset_out")
        second = PieceOffset("P", BoardSquare("h2"), "NE", "OUT", "clearance_offset_out")
        actions = (
            first, second,
            Transfer("N", BoardSquare("g1"), BoardSquare("f3"), "move"),
            PieceOffset("P", second.square, second.corner, "IN", "clearance_offset_in"),
            PieceOffset("P", first.square, first.corner, "IN", "clearance_offset_in"),
        )
        state = _move_state(actions)
        state.pending.next_action = 2
        saved = state.to_json()
        self.assertEqual(saved["pending"]["actions"][0]["type"], "piece_offset")
        self.assertEqual(saved["pending"]["actions"][1]["type"], "piece_offset")
        restored = GameState.from_json(saved)
        self.assertEqual(restored.pending.actions, actions)
        self.assertEqual(restored.pending.next_action, 2)
        self.assertEqual(restored.physical_squares["f2"], "P")
        self.assertEqual(restored.physical_squares["h2"], "P")

    def test_in_flight_offset_retains_three_argument_wire_route(self) -> None:
        offset = PieceOffset("P", BoardSquare("f2"), "NW", "OUT", "clearance_offset_out")
        state = _move_state((
            offset,
            Transfer("N", BoardSquare("g1"), BoardSquare("f3"), "move"),
            PieceOffset("P", offset.square, offset.corner, "IN", "clearance_offset_in"),
        ))
        state.pending.phase = "in_flight"
        state.pending.active_route = (str(chess.F2), "NW", "OUT")
        self.assertEqual(GameState.from_json(state.to_json()).pending.active_route,
                         state.pending.active_route)
        state.pending.active_route = (str(chess.F2), "NE", "OUT")
        with self.assertRaisesRegex(GameStateError, "active offset route"):
            state.validate()

    def test_offset_restoration_must_match_and_complete(self) -> None:
        departure = PieceOffset("P", BoardSquare("f2"), "NW", "OUT", "clearance_offset_out")
        move = Transfer("N", BoardSquare("g1"), BoardSquare("f3"), "move")
        with self.assertRaisesRegex(GameStateError, "restoration does not match"):
            _move_state((
                departure, move,
                PieceOffset("P", departure.square, "NE", "IN", "clearance_offset_in"),
            )).validate()
        with self.assertRaisesRegex(GameStateError, "leaves a piece off center"):
            _move_state((departure, move)).validate()

    def test_transfer_cannot_use_occupied_offset_square(self) -> None:
        departure = PieceOffset("P", BoardSquare("f2"), "NW", "OUT", "clearance_offset_out")
        state = _move_state((
            departure,
            Transfer("P", BoardSquare("f2"), BoardSquare("f3"), "move"),
            PieceOffset("P", departure.square, departure.corner, "IN", "clearance_offset_in"),
        ))
        with self.assertRaisesRegex(GameStateError, "cannot transfer through an offset"):
            state.validate()

    def test_reset_round_trips_confirmed_offset_prefix(self) -> None:
        departure = PieceOffset("P", BoardSquare("d2"), "NW", "OUT", "clearance_offset_out")
        actions = (
            departure,
            Transfer("P", BoardSquare("e4"), BoardSquare("e2"), "reset_board"),
            PieceOffset("P", departure.square, departure.corner, "IN", "clearance_offset_in"),
        )
        state = _reset_state(actions)
        state.reset.next_action = 1
        restored = GameState.from_json(state.to_json())
        self.assertEqual(restored.reset.actions, actions)
        self.assertEqual(restored.reset.next_action, 1)
        self.assertEqual(restored.physical_squares["d2"], "P")
        state.reset.next_action = 0
        state.reset.phase = "in_flight"
        state.reset.active_route = (str(chess.D2), "NW", "OUT")
        self.assertEqual(GameState.from_json(state.to_json()).reset.active_route,
                         state.reset.active_route)

    def test_reset_rejects_offset_transfer_and_unrestored_offset(self) -> None:
        departure = PieceOffset("P", BoardSquare("d2"), "NW", "OUT", "clearance_offset_out")
        with self.assertRaisesRegex(GameStateError, "cannot transfer through an offset"):
            _reset_state((
                departure,
                Transfer("P", BoardSquare("d2"), BoardSquare("d3"), "reset_board"),
                Transfer("P", BoardSquare("e4"), BoardSquare("e2"), "reset_board"),
            )).validate()
        with self.assertRaisesRegex(GameStateError, "leaves a piece off center"):
            _reset_state((
                departure,
                Transfer("P", BoardSquare("e4"), BoardSquare("e2"), "reset_board"),
            )).validate()

    def test_legacy_castling_rook_offset_plan_remains_readable(self) -> None:
        fen = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"
        board = chess.Board(fen)
        physical = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        next_board = board.copy()
        next_board.push_uci("e1g1")
        actions = (
            RookOffset("R", BoardSquare("h1"), "NE", "OUT", "castle_rook_out"),
            Transfer("K", BoardSquare("e1"), BoardSquare("g1"), "castle_king"),
            RookOffset("R", BoardSquare("h1"), "NE", "IN", "castle_rook_in"),
            Transfer("R", BoardSquare("h1"), BoardSquare("f1"), "castle_rook"),
        )
        state = GameState(
            board.fen(), ParkingInventory.empty(), physical, [None] * 32,
            pending=PendingMove("e1g1", next_board.fen(), ParkingInventory.empty(), actions),
        )
        self.assertEqual(GameState.from_json(state.to_json()).pending.actions, actions)

    def test_in_flight_rook_carry_uses_confirmed_corner(self) -> None:
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        physical = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        physical["g1"] = physical.pop("e1")
        after = board.copy()
        after.push_uci("e1g1")
        actions = (
            RookOffset("R", BoardSquare("h1"), "NE", "OUT", "castle_rook_out"),
            Transfer("K", BoardSquare("e1"), BoardSquare("g1"), "castle_king"),
            Transfer("R", BoardSquare("h1"), BoardSquare("f1"), "castle_rook_from_offset"),
        )
        pending = PendingMove("e1g1", after.fen(), ParkingInventory.empty(), actions)
        pending.next_action = 2
        pending.phase = "in_flight"
        pending.active_route = (str(chess.H1), "NE", str(chess.F1), "F1G2E4C2B1")
        state = GameState(
            board.fen(), ParkingInventory.empty(), physical, [None] * 32,
            pending=pending,
        )
        state.validate()
        pending.active_route = (str(chess.H1), "SW", str(chess.F1), "F1G2E4C2B1")
        with self.assertRaisesRegex(GameStateError, "confirmed rook corner"):
            state.validate()


if __name__ == "__main__":
    unittest.main()

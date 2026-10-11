"""Whole-move execution and recovery for temporary neighbor offsets."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chess

from chess_table_host.game import GameRunner, GameState, JsonGameStore, MoveDeferred
from chess_table_host.offset_stage import route_has_tight_squeeze
from chess_table_host.planner import BoardSquare, ParkingInventory, PieceOffset, Transfer
from chess_table_host.routing import CarryRoute, HalfSquarePoint, NoRouteError, RouteGeometry


GEOMETRY = RouteGeometry(
    square_pitch_mm=31.25, moving_diameter_mm=18,
    occupied_diameter_mm=18, margin_mm=0,
    x_travel_mm=249.7350, y_travel_mm=251.9850,
    board_origin_x_mm=-0.1325, board_origin_y_mm=0.9925,
    end_inset_mm=2.0, pulses_per_mm=133.3333,
)
FEN = "rnbqkbnr/pppppppp/8/8/8/2N5/PPPPPPPP/R1BQKBNR w KQkq - 0 1"


class RecordingLink:
    def __init__(self) -> None:
        self.commands: list[tuple[object, ...]] = []

    def set_map(self, occupancy_hex: str) -> None:
        self.commands.append(("MAP", occupancy_hex))

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        self.commands.append(("CARRY", source_id, destination_id, point_chars))
        return SimpleNamespace(command_id=len(self.commands))

    def offset(self, square_id: int, corner: str, direction: str):
        self.commands.append(("OFFSET", square_id, corner, direction))
        return SimpleNamespace(command_id=len(self.commands))


class OffsetStageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JsonGameStore(Path(temporary.name) / "game.json")
        board = chess.Board(FEN)
        squares = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        state = GameState(board.fen(), ParkingInventory.empty(), squares, [None] * 32)
        self.runner = GameRunner(state, self.store, allow_tight_routes=True)
        self.store.save(state)

    def prepare_offset_move(self):
        # Exercise the offset fallback when no temporary square-to-square
        # single-blocker stage is available to this move.
        with patch("chess_table_host.game.plan_tight_knight_move", return_value=None):
            return self.runner.prepare_move("c3b1", GEOMETRY)

    def test_move_offsets_are_strict_and_restored(self) -> None:
        plan = self.prepare_offset_move()
        self.assertEqual(len(plan.actions), 5)
        self.assertEqual(
            [action.direction for action in plan.actions if isinstance(action, PieceOffset)],
            ["OUT", "OUT", "IN", "IN"],
        )
        self.assertEqual(
            [action for action in plan.actions if isinstance(action, Transfer)],
            [Transfer("N", BoardSquare("c3"), BoardSquare("b1"),
                      "clearance_offset_move")],
        )
        routes = []
        link = RecordingLink()
        self.runner.execute_pending(
            link, GEOMETRY, lambda _action: True,
            lambda _action, route, _wire: routes.append(route) or True,
        )
        self.assertEqual(len(routes), 5)
        self.assertTrue(all(not route.clearance_ignored for route in routes))
        self.assertEqual(
            [command[0] for command in link.commands if command[0] != "MAP"],
            ["OFFSET", "OFFSET", "CARRY", "OFFSET", "OFFSET"],
        )
        self.assertEqual(self.runner.state.moves, ["c3b1"])
        self.assertEqual(self.runner.state.physical_squares["b1"], "N")
        self.assertEqual(self.runner.state.physical_squares["b2"], "P")
        self.assertEqual(self.runner.state.physical_squares["c2"], "P")
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.store.load().to_json(), self.runner.state.to_json())

    def test_resume_restorations_after_middle_carry(self) -> None:
        self.prepare_offset_move()
        with self.assertRaises(MoveDeferred):
            self.runner.execute_pending(
                RecordingLink(), GEOMETRY, lambda _action: True,
                approve_route=lambda action, _route, _wire:
                    not (isinstance(action, PieceOffset) and action.direction == "IN"),
            )
        saved = self.store.load()
        self.assertEqual(saved.pending.next_action, 3)
        self.assertEqual(saved.physical_squares["b1"], "N")
        self.assertEqual(saved.moves, [])

        resumed = GameRunner(saved, self.store, allow_tight_routes=True)
        link = RecordingLink()
        resumed.execute_pending(link, GEOMETRY, lambda _action: True)
        self.assertEqual(
            [command[0] for command in link.commands if command[0] != "MAP"],
            ["OFFSET", "OFFSET"],
        )
        self.assertIsNone(resumed.state.pending)
        self.assertEqual(resumed.state.moves, ["c3b1"])

    def test_tight_switch_does_not_authorize_modeled_squeeze(self) -> None:
        action = Transfer("N", BoardSquare("c3"), BoardSquare("b1"), "move")
        with self.assertRaisesRegex(NoRouteError, "tight carry"):
            self.runner._route_transfer(
                action, self.runner.state.physical_squares,
                self.runner.state.physical_parking, GEOMETRY,
            )

    def test_horizontal_gap_between_two_pieces_is_a_tight_squeeze(self) -> None:
        route = CarryRoute(
            Transfer("R", BoardSquare("d4"), BoardSquare("g4"), "move"),
            (HalfSquarePoint(7, 7), HalfSquarePoint(8, 8),
             HalfSquarePoint(12, 8), HalfSquarePoint(13, 7)),
            geometry_applied=True, clearance_ignored=True,
        )
        self.assertTrue(route_has_tight_squeeze(
            route, {"d4": "R", "e4": "P", "e5": "P"}, [None] * 32,
            GEOMETRY,
        ))


if __name__ == "__main__":
    unittest.main()

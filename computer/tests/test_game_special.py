"""Physical-action checkpoints for special chess moves."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import chess

from chess_table_host.game import GameRunner, GameState, JsonGameStore, ReconciliationRequired
from chess_table_host.planner import BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, Transfer
from chess_table_host.routing import RouteGeometry


# Actual controller GEOM; piece diameters are test inputs pending measurement.
GEOMETRY = RouteGeometry(
    square_pitch_mm=31.25,
    moving_diameter_mm=18,
    occupied_diameter_mm=18,
    margin_mm=0,
    x_travel_mm=249.7350,
    y_travel_mm=251.9850,
    board_origin_x_mm=-0.1325,
    board_origin_y_mm=0.9925,
    end_inset_mm=2.0000,
    pulses_per_mm=133.3333,
)


class FakeLink:
    def __init__(self) -> None:
        self.maps: list[str] = []
        self.carries: list[tuple[str, str, str]] = []

    def set_map(self, occupancy_hex: str) -> None:
        self.maps.append(occupancy_hex)

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        self.carries.append((source_id, destination_id, point_chars))
        return SimpleNamespace(command_id=len(self.carries))


class SpecialMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JsonGameStore(Path(temporary.name) / "game.json")

    def runner_at(self, fen: str) -> GameRunner:
        board = chess.Board(fen)
        self.assertTrue(board.is_valid())
        squares = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        state = GameState(board.fen(), ParkingInventory.empty(), squares, [None] * 32)
        runner = GameRunner(state, self.store)
        self.store.save(state)
        return runner

    def test_en_passant_parks_pawn_from_actual_square_before_attacker(self) -> None:
        runner = self.runner_at("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        before = runner.state.board_fen
        plan = runner.prepare_move("e5d6", GEOMETRY)
        self.assertEqual(
            plan.actions,
            (
                Transfer("p", BoardSquare("d5"), ParkingSlot(0), "capture"),
                Transfer("P", BoardSquare("e5"), BoardSquare("d6"), "move"),
            ),
        )
        link = FakeLink()
        confirmations: list[str] = []

        def confirm(action) -> bool:
            saved = self.store.load()
            self.assertEqual(saved.board_fen, before)
            self.assertEqual(saved.pending.phase, "awaiting_confirmation")
            if len(confirmations) == 1:
                self.assertEqual(saved.pending.next_action, 1)
                self.assertNotIn("d5", saved.physical_squares)
                self.assertEqual(saved.physical_squares["e5"], "P")
                self.assertEqual(saved.physical_parking[0], "p")
            confirmations.append(action.reason)
            return True

        runner.execute_pending(link, GEOMETRY, confirm)
        self.assertEqual(confirmations, ["capture", "move"])
        self.assertEqual([route[:2] for route in link.carries], [("35", "64"), ("36", "43")])
        self.assertEqual(len(link.maps), 2)
        second_map = int.from_bytes(bytes.fromhex(link.maps[1]), "little")
        self.assertFalse(second_map & (1 << chess.D5))
        self.assertTrue(second_map & (1 << chess.E5))
        self.assertTrue(second_map & (1 << 64))
        self.assertEqual(runner.state.physical_squares["d6"], "P")
        self.assertEqual(runner.state.physical_parking[0], "p")
        self.assertIsNone(runner.state.pending)
        self.assertEqual(runner.state.moves, ["e5d6"])
        self.assertEqual(self.store.load().to_json(), runner.state.to_json())

    def test_castling_confirms_king_before_rook_transfer(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        before = runner.state.board_fen
        plan = runner.prepare_move("e1g1", GEOMETRY)
        self.assertEqual(
            plan.actions,
            (
                Transfer("K", BoardSquare("e1"), BoardSquare("g1"), "castle_king"),
                Transfer("R", BoardSquare("h1"), BoardSquare("f1"), "castle_rook"),
            ),
        )
        link = FakeLink()
        confirmations: list[str] = []

        def confirm(action) -> bool:
            saved = self.store.load()
            self.assertEqual(saved.board_fen, before)
            self.assertEqual(saved.pending.phase, "awaiting_confirmation")
            if len(confirmations) == 1:
                self.assertNotIn("e1", saved.physical_squares)
                self.assertEqual(saved.physical_squares["g1"], "K")
                self.assertEqual(saved.physical_squares["h1"], "R")
                self.assertNotIn("f1", saved.physical_squares)
            confirmations.append(action.reason)
            return True

        runner.execute_pending(link, GEOMETRY, confirm)
        self.assertEqual(confirmations, ["castle_king", "castle_rook"])
        self.assertEqual([route[:2] for route in link.carries], [("4", "6"), ("7", "5")])
        self.assertNotEqual(link.maps[0], link.maps[1])
        self.assertEqual(runner.state.physical_squares["g1"], "K")
        self.assertEqual(runner.state.physical_squares["f1"], "R")
        self.assertEqual(runner.state.moves, ["e1g1"])
        self.assertIsNone(runner.state.pending)

    def test_promoted_pawn_waits_for_carry_confirmation(self) -> None:
        runner = self.runner_at("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        before = runner.state.board_fen
        plan = runner.prepare_move("a7a8q", GEOMETRY)
        self.assertEqual(
            plan.actions,
            (
                Transfer("P", BoardSquare("a7"), BoardSquare("a8"), "promotion_stand_in"),
            ),
        )
        link = FakeLink()
        with self.assertRaises(ReconciliationRequired):
            runner.execute_pending(link, GEOMETRY, lambda action: False)
        saved = self.store.load()
        self.assertEqual(len(link.carries), 1)
        self.assertEqual(saved.board_fen, before)
        self.assertEqual(saved.moves, [])
        self.assertEqual(saved.pending.next_action, 0)
        self.assertEqual(saved.pending.phase, "awaiting_confirmation")
        self.assertIsNotNone(saved.pending.active_route)
        self.assertEqual(saved.physical_squares["a7"], "P")
        self.assertNotIn("a8", saved.physical_squares)

        # The operator verifies the pawn reached a8; no replacement is needed.
        runner.resolve_uncertain_action("destination")
        self.assertIsNone(runner.state.pending)
        self.assertEqual(runner.state.physical_squares["a8"], "P")
        self.assertEqual(chess.Board(runner.state.board_fen).piece_at(chess.A8), chess.Piece(chess.QUEEN, chess.WHITE))
        self.assertEqual(runner.state.moves, ["a7a8q"])
        self.assertEqual(len(link.carries), 1)
        self.assertEqual(self.store.load().to_json(), runner.state.to_json())


if __name__ == "__main__":
    unittest.main()

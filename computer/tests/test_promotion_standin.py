"""A promoted pawn keeps its physical token for the rest of the game.

Chess rules use the promoted type, while motion and parking continue to use
the pawn that was already on the table. No spare pieces are available.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import chess

from chess_table_host.game import (
    GameRunner, GameState, GameStateError, JsonGameStore, ReconciliationRequired,
)
from chess_table_host.planner import BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, Transfer
from chess_table_host.routing import RouteGeometry


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


class PromotionStandinTests(unittest.TestCase):
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

    def play(self, runner: GameRunner, uci: str) -> tuple[object, FakeLink]:
        plan = runner.prepare_move(uci, GEOMETRY)
        link = FakeLink()
        runner.execute_pending(link, GEOMETRY, lambda _action: True)
        return plan, link

    def test_every_white_promotion_keeps_the_pawn_and_needs_one_carry(self) -> None:
        for promotion in "qrbn":
            with self.subTest(promotion=promotion):
                runner = self.runner_at("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
                plan, link = self.play(runner, f"a7a8{promotion}")

                self.assertEqual(
                    plan.actions,
                    (Transfer("P", BoardSquare("a7"), BoardSquare("a8"), "promotion_stand_in"),),
                )
                self.assertFalse(any(isinstance(action, ManualReplacement) for action in plan.actions))
                self.assertEqual(len(link.carries), 1)
                self.assertEqual(runner.state.physical_squares["a8"], "P")
                self.assertEqual(runner.state.board().piece_at(chess.A8).symbol(), promotion.upper())
                self.assertEqual(runner.state.committed_parking, ParkingInventory.empty())
                self.assertEqual(self.store.load().to_json(), runner.state.to_json())

    def test_promoted_queen_moves_later_as_the_same_physical_pawn(self) -> None:
        runner = self.runner_at("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        self.play(runner, "a7a8q")
        runner = GameRunner(self.store.load(), self.store)
        self.play(runner, "e8e7")

        plan, link = self.play(runner, "a8a7")

        self.assertEqual(plan.actions, (Transfer("P", BoardSquare("a8"), BoardSquare("a7"), "move"),))
        self.assertEqual(len(link.carries), 1)
        self.assertEqual(runner.state.physical_squares["a7"], "P")
        self.assertEqual(runner.state.board().piece_at(chess.A7).symbol(), "Q")
        self.assertNotIn("a8", runner.state.physical_squares)

    def test_capture_of_promoted_queen_parks_the_pawn_token(self) -> None:
        runner = self.runner_at("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        self.play(runner, "a7a8q")

        plan, link = self.play(runner, "b8a8")

        self.assertEqual(plan.actions[0].piece, "P")
        self.assertEqual(plan.actions[0].source, BoardSquare("a8"))
        self.assertIsInstance(plan.actions[0].destination, ParkingSlot)
        self.assertEqual(plan.actions[0].reason, "capture")
        self.assertEqual(runner.state.physical_parking[plan.actions[0].destination.slot_id], "P")
        self.assertEqual(runner.state.physical_squares["a8"], "r")
        self.assertEqual(runner.state.board().piece_at(chess.A8).symbol(), "r")
        self.assertEqual(len(link.carries), 2)

    def test_capture_promotion_parks_victim_then_moves_pawn_to_destination(self) -> None:
        runner = self.runner_at("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1")

        plan, link = self.play(runner, "a7b8q")

        self.assertEqual(len(plan.actions), 2)
        self.assertEqual(plan.actions[0].piece, "r")
        self.assertEqual(plan.actions[0].source, BoardSquare("b8"))
        self.assertIsInstance(plan.actions[0].destination, ParkingSlot)
        self.assertEqual(plan.actions[0].reason, "capture")
        self.assertEqual(
            plan.actions[1], Transfer("P", BoardSquare("a7"), BoardSquare("b8"), "promotion_stand_in")
        )
        self.assertEqual(len(link.carries), 2)
        self.assertEqual(runner.state.physical_squares["b8"], "P")
        self.assertEqual(runner.state.board().piece_at(chess.B8).symbol(), "Q")
        self.assertEqual(runner.state.physical_parking[plan.actions[0].destination.slot_id], "r")

    def test_black_underpromotion_keeps_black_pawn(self) -> None:
        runner = self.runner_at("4k3/8/8/8/8/8/p7/4K3 b - - 0 1")

        plan, link = self.play(runner, "a2a1n")

        self.assertEqual(
            plan.actions,
            (Transfer("p", BoardSquare("a2"), BoardSquare("a1"), "promotion_stand_in"),),
        )
        self.assertEqual(len(link.carries), 1)
        self.assertEqual(runner.state.physical_squares["a1"], "p")
        self.assertEqual(runner.state.board().piece_at(chess.A1).symbol(), "n")
        self.assertEqual(runner.state.committed_parking, ParkingInventory.empty())

    def test_checkpoint_reload_and_uncertain_promotion_recovery(self) -> None:
        runner = self.runner_at("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        before = runner.state.board_fen
        runner.prepare_move("a7a8q", GEOMETRY)
        with self.assertRaises(ReconciliationRequired):
            runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: False)

        saved = self.store.load()
        self.assertEqual(saved.board_fen, before)
        self.assertEqual(saved.pending.phase, "awaiting_confirmation")
        self.assertEqual(saved.physical_squares["a7"], "P")
        resumed = GameRunner(saved, self.store)
        resumed.resolve_uncertain_action("destination")

        self.assertIsNone(resumed.state.pending)
        self.assertEqual(resumed.state.physical_squares["a8"], "P")
        self.assertEqual(resumed.state.board().piece_at(chess.A8).symbol(), "Q")
        self.assertEqual(self.store.load().to_json(), resumed.state.to_json())

    def test_checkpoint_declares_promotion_rule_and_migrates_unpromoted_v2(self) -> None:
        saved = GameState.new().to_json()
        self.assertEqual(saved["promotion_rule"], "pawn_stand_in")
        legacy = dict(saved, version=2)
        legacy.pop("promotion_rule")
        restored = GameState.from_json(legacy)
        self.assertEqual(restored.to_json()["promotion_rule"], "pawn_stand_in")
        self.assertEqual(restored.to_json()["version"], 4)

    def test_old_promoted_checkpoint_requires_reconciliation(self) -> None:
        runner = self.runner_at("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        self.play(runner, "a7a8q")
        legacy = dict(runner.state.to_json(), version=2)
        legacy.pop("promotion_rule")
        with self.assertRaisesRegex(GameStateError, "manual reconciliation"):
            GameState.from_json(legacy)

    def test_checkpoint_rejects_other_promotion_rule(self) -> None:
        saved = GameState.new().to_json()
        saved["promotion_rule"] = "manual_replacement"
        with self.assertRaisesRegex(GameStateError, "promotion rule"):
            GameState.from_json(saved)


if __name__ == "__main__":
    unittest.main()

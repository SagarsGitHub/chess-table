"""Checkpointed physical board reset using the existing MAP/CARRY protocol."""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chess

from chess_table_host.game import (
    GameRunner, GameState, GameStateError, JsonGameStore, ReconciliationRequired,
)
from chess_table_host.planner import BoardSquare, ParkingInventory, ParkingSlot, Transfer
from chess_table_host.play import SimulatedLink, _geometry_from_file, main
from chess_table_host.routing import RouteGeometry


GEOMETRY = RouteGeometry(
    square_pitch_mm=31.25, moving_diameter_mm=18, occupied_diameter_mm=18,
    margin_mm=0, x_travel_mm=249.7350, y_travel_mm=251.9850,
    board_origin_x_mm=-0.1325, board_origin_y_mm=0.9925,
    end_inset_mm=2.0000, pulses_per_mm=133.3333,
)


class FakeLink:
    def __init__(self, *, fail_on: int | None = None, during=None) -> None:
        self.maps: list[str] = []
        self.carries: list[tuple[str, str, str]] = []
        self.fail_on = fail_on
        self.during = during

    def set_map(self, occupancy_hex: str) -> None:
        self.maps.append(occupancy_hex)

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        self.carries.append((source_id, destination_id, point_chars))
        if self.during:
            self.during()
        if self.fail_on == len(self.carries):
            raise OSError("serial disconnect during reset CARRY")
        return SimpleNamespace(command_id=len(self.carries))


class ResetTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JsonGameStore(Path(temporary.name) / "game.json")
        self.runner = GameRunner(GameState.new(), self.store, allow_tight_routes=True)
        self.store.save(self.runner.state)

    def play(self, *moves: str) -> None:
        for uci in moves:
            self.runner.prepare_move(uci, GEOMETRY)
            self.runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)

    def assert_start(self) -> None:
        saved = self.store.load()
        self.assertEqual(saved.board_fen, chess.Board().fen())
        self.assertEqual(saved.moves, [])
        self.assertEqual(saved.physical_squares, GameState.new().physical_squares)
        self.assertEqual(saved.physical_parking, [None] * 32)
        self.assertIsNone(saved.reset)

    def test_already_standard_position_uses_no_carry(self) -> None:
        self.assertEqual(self.runner.prepare_reset(GEOMETRY), ())
        self.assert_start()

    def test_misplaced_home_rank_rook_returns_directly_without_staging(self) -> None:
        self.play("g1f3", "g8f6", "h1g1", "h8g8")
        actions = self.runner.preflight_reset(GEOMETRY)
        self.assertEqual(actions[0].source, BoardSquare("g1"))
        self.assertEqual(actions[0].destination, BoardSquare("h1"))
        self.assertEqual(actions[1].source, BoardSquare("g8"))
        self.assertEqual(actions[1].destination, BoardSquare("h8"))
        self.assertEqual(len(actions), 4)

    def test_board_knight_returns_before_parked_pawn_when_exits_stay_open(self) -> None:
        self.play("e2e4", "e7e5", "f1c4", "b8c6", "c4f7", "e8f7")
        actions = self.runner.preflight_reset(GEOMETRY)
        knight = next(index for index, action in enumerate(actions)
                      if action.source == BoardSquare("c6") and action.destination == BoardSquare("b8"))
        first_parked_pawn = next(index for index, action in enumerate(actions)
                                 if isinstance(action.source, ParkingSlot) and action.piece.lower() == "p")
        self.assertLess(knight, first_parked_pawn)

    def test_parked_pawn_precedes_bishop_if_bishop_would_trap_its_exit(self) -> None:
        self.play("e2e4", "e7e5", "f1c4", "b8c6", "c4f7", "e8f7")
        actions = self.runner.preflight_reset(GEOMETRY)
        parked = [action for action in actions if isinstance(action.source, ParkingSlot)]
        self.assertEqual([action.piece for action in parked], ["p", "B"])

    def test_parked_bishop_precedes_pawn_when_both_exits_are_open(self) -> None:
        self.play("e2e4", "e7e5", "f1c4", "b8c6", "c4f7", "e8f7")
        slots = self.runner.state.physical_parking
        for index in range(len(slots)):
            slots[index] = None
        slots[20] = "p"
        slots[23] = "B"
        actions = self.runner.preflight_reset(GEOMETRY)
        parked = [action for action in actions if isinstance(action.source, ParkingSlot)]
        self.assertEqual([action.piece for action in parked], ["B", "p"])

    def test_full_game_reset_breaks_parked_home_cycle(self) -> None:
        # A 126-ply game left 20 pieces parked. Three of their remaining
        # return homes formed a cycle: q->d8 would trap b, b->f8 would trap r,
        # and r->a8 would trap q. The complete reset must stage one piece and
        # reach the standard setup without sending an unroutable transfer.
        fixture = Path(__file__).with_name("fixtures") / "parking_cycle_game.json"
        state = JsonGameStore(fixture).load()
        self.store.save(state)
        self.runner = GameRunner(self.store.load(), self.store, allow_tight_routes=True)
        actions = self.runner.prepare_reset(GEOMETRY)
        self.assertTrue(any(isinstance(action.source, ParkingSlot) and
                            action.destination.name[1] in "3456"
                            for action in actions))
        link = FakeLink()
        self.runner.execute_reset(link, GEOMETRY, lambda _action: True,
                                  confirmation_source="simulation")
        self.assertEqual(len(link.carries), len(actions))
        self.assert_start()

    def test_stage_pawn_blocker_so_knight_goes_home_in_one_carry(self) -> None:
        # Physical arrangement of a home-square cycle. The remaining pieces
        # keep the standard inventory while the open lanes permit reset routes.
        squares = self.runner.state.physical_squares
        squares["g1"], squares["h2"] = squares["h2"], squares["g1"]
        squares["g4"] = squares.pop("g2")
        squares["c4"] = squares.pop("f1")
        actions = self.runner.preflight_reset(GEOMETRY)
        knight_returns = [action for action in actions
                          if action.piece == "N" and action.destination == BoardSquare("g1")]
        self.assertEqual(len(knight_returns), 1)
        self.assertEqual(knight_returns[0].source, BoardSquare("h2"))
        self.assertEqual(actions[0].piece, "P")
        self.assertEqual(actions[0].source, BoardSquare("g1"))

    def test_capture_reset_returns_parked_piece_and_checkpoints_each_transfer(self) -> None:
        self.play("e2e4", "d7d5", "e4d5")
        self.assertEqual(sum(piece is not None for piece in self.runner.state.physical_parking), 1)
        actions = self.runner.prepare_reset(GEOMETRY)
        self.assertTrue(any(isinstance(action.source, ParkingSlot) for action in actions))
        self.assertEqual(sum(1 for action in actions if isinstance(action, Transfer)), len(actions))
        self.assertEqual(self.store.load().reset.next_action, 0)

        def during() -> None:
            saved = self.store.load()
            self.assertEqual(saved.reset.phase, "in_flight")
            self.assertEqual(saved.moves, ["e2e4", "d7d5", "e4d5"])

        def confirm(_action: Transfer) -> bool:
            saved = self.store.load()
            self.assertEqual(saved.reset.phase, "awaiting_confirmation")
            return True

        link = FakeLink(during=during)
        self.runner.execute_reset(link, GEOMETRY, confirm, confirmation_source="simulation")
        self.assertEqual(len(link.carries), len(actions))
        self.assert_start()

    def test_uncertain_reset_requires_inspection_and_never_replays_it_automatically(self) -> None:
        self.play("e2e4", "d7d5", "e4d5")
        actions = self.runner.prepare_reset(GEOMETRY)
        with self.assertRaises(OSError):
            self.runner.execute_reset(FakeLink(fail_on=1), GEOMETRY, lambda _action: True)
        saved = self.store.load()
        self.assertEqual(saved.reset.phase, "in_flight")
        self.assertEqual(saved.reset.next_action, 0)
        resumed = GameRunner(saved, self.store, allow_tight_routes=True)
        with self.assertRaises(ReconciliationRequired):
            resumed.execute_reset(FakeLink(), GEOMETRY, lambda _action: True)
        resumed.resolve_uncertain_reset("destination")
        self.assertEqual(self.store.load().reset.next_action, 1)
        link = FakeLink()
        resumed.execute_reset(link, GEOMETRY, lambda _action: True)
        self.assertEqual(len(link.carries), len(actions) - 1)
        self.assert_start()

    def test_reconciling_last_reset_carry_finishes_once_without_restarting(self) -> None:
        self.play("e2e4")
        actions = self.runner.prepare_reset(GEOMETRY)
        with self.assertRaises(OSError):
            self.runner.execute_reset(
                FakeLink(fail_on=len(actions)), GEOMETRY, lambda _action: True,
            )
        saved = self.store.load()
        self.assertEqual(saved.reset.next_action, len(actions) - 1)
        self.assertEqual(saved.reset.phase, "in_flight")

        answers = iter(("destination", "y", "y"))
        with (
            patch("chess_table_host.play._connect_physical",
                  side_effect=AssertionError("reset is already complete")),
            patch("builtins.input", side_effect=lambda _prompt: next(answers)),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(main([
                "--resume", "--state", str(self.store.path), "--reset-board",
            ]), 0)
        self.assertIn("completed during reconciliation", output.getvalue())
        self.assert_start()
        completed = [event for event in self.store.load().events
                     if event["event"] == "board_reset_completed"]
        self.assertEqual(completed, [{"event": "board_reset_completed", "previous_plies": 1}])

    def test_uncertain_reset_source_retries_only_after_reconciliation(self) -> None:
        self.play("e2e4")
        actions = self.runner.prepare_reset(GEOMETRY)
        with self.assertRaises(OSError):
            self.runner.execute_reset(FakeLink(fail_on=1), GEOMETRY, lambda _action: True)
        resumed = GameRunner(self.store.load(), self.store, allow_tight_routes=True)
        resumed.resolve_uncertain_reset("source")
        link = FakeLink()
        resumed.execute_reset(link, GEOMETRY, lambda _action: True)
        self.assertEqual(len(link.carries), len(actions))
        self.assert_start()

    def test_abort_reset_restores_original_checkpoint_after_manual_restore(self) -> None:
        self.play("e2e4", "d7d5", "e4d5")
        original = deepcopy(self.runner.state.to_json())
        self.runner.prepare_reset(GEOMETRY)
        with self.assertRaises(OSError):
            self.runner.execute_reset(FakeLink(fail_on=2), GEOMETRY, lambda _action: True)
        saved = self.store.load()
        self.assertEqual(saved.reset.next_action, 1)
        resumed = GameRunner(saved, self.store, allow_tight_routes=True)
        # The API is called only after the operator has physically restored
        # every square and parking slot to the pre-reset position.
        resumed.abort_reset_after_restore()
        restored = self.store.load()
        self.assertIsNone(restored.reset)
        self.assertEqual(restored.board_fen, original["board_fen"])
        self.assertEqual(restored.moves, original["moves"])
        self.assertEqual(restored.physical_squares, original["physical_squares"])
        self.assertEqual(restored.physical_parking, original["physical_parking"])

    def test_version_three_checkpoint_loads_without_a_reset(self) -> None:
        legacy = GameState.new().to_json()
        legacy["version"] = 3
        legacy.pop("reset")
        restored = GameState.from_json(legacy)
        self.assertIsNone(restored.reset)
        self.assertEqual(restored.to_json()["version"], 4)

    def test_pending_move_cannot_start_reset(self) -> None:
        self.runner.prepare_move("e2e4", GEOMETRY)
        with self.assertRaisesRegex(ReconciliationRequired, "pending move"):
            self.runner.prepare_reset(GEOMETRY)
        self.assertIsNone(self.store.load().reset)

    def test_blocked_corner_parking_opens_a1_then_restores_rook(self) -> None:
        self.play("e2e4", "d7d5", "e4d5")
        old_slot = next(i for i, piece in enumerate(self.runner.state.physical_parking) if piece)
        pawn = self.runner.state.physical_parking[old_slot]
        self.runner.state.physical_parking[old_slot] = None
        self.runner.state.physical_parking[0] = pawn
        self.runner.state.committed_parking = ParkingInventory(tuple(self.runner.state.physical_parking))
        self.store.save(self.runner.state)
        actions = self.runner.prepare_reset(GEOMETRY)
        self.assertTrue(any(action.source == ParkingSlot(0) for action in actions))
        self.assertTrue(any(action.source.name == "a1" for action in actions
                            if hasattr(action.source, "name")))
        self.runner.execute_reset(FakeLink(), GEOMETRY, lambda _action: True)
        self.assert_start()

    def test_promoted_pawn_stand_in_returns_as_pawn(self) -> None:
        self.play(
            "a2a4", "b7b5", "a4b5", "a7a6", "b5a6",
            "b8c6", "a6a7", "c6b8", "a7b8q",
        )
        self.assertEqual(self.runner.state.board().piece_at(chess.B8).symbol(), "Q")
        self.assertEqual(self.runner.state.physical_squares["b8"], "P")
        actions = self.runner.prepare_reset(GEOMETRY)
        self.assertTrue(any(action.source == ParkingSlot(0) or action.piece == "P"
                            for action in actions))
        self.runner.execute_reset(FakeLink(), GEOMETRY, lambda _action: True)
        self.assert_start()

    def test_missing_piece_cannot_start_reset(self) -> None:
        self.play("e2e4")
        # A corrupt checkpoint cannot be made by the public constructor, so
        # check the inventory guard against an in-memory physical discrepancy.
        self.runner.state.physical_squares.pop("e4")
        with self.assertRaisesRegex(GameStateError, "32 standard"):
            self.runner.preflight_reset(GEOMETRY)

    def test_physical_cli_reset_uses_no_routine_confirmation_prompts(self) -> None:
        self.play("e2e4", "d7d5", "e4d5")
        played_bytes = self.store.path.read_bytes()
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"
        controller = _geometry_from_file(fixture)
        with (
            patch("chess_table_host.play._connect_physical",
                  return_value=(SimulatedLink(), controller)),
            patch("builtins.input", side_effect=AssertionError("unexpected reset prompt")),
            redirect_stdout(StringIO()) as output,
        ):
            result = main(["--resume", "--state", str(self.store.path), "--reset-board"])
        self.assertEqual(result, 0)
        self.assertIn("Board reset complete", output.getvalue())
        archives = list(self.store.path.parent.glob("game.before-reset-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), played_bytes)
        self.assert_start()

    def test_already_arranged_played_game_is_archived_before_fresh_checkpoint(self) -> None:
        self.play("g1f3", "g8f6", "f3g1", "f6g8")
        self.assertEqual(self.runner.preflight_reset(GEOMETRY), ())
        played_bytes = self.store.path.read_bytes()
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"
        controller = _geometry_from_file(fixture)
        with (
            patch("chess_table_host.play._connect_physical",
                  return_value=(SimulatedLink(), controller)),
            patch("builtins.input", side_effect=AssertionError("unexpected reset prompt")),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["--resume", "--state", str(self.store.path),
                                   "--reset-board"]), 0)
        archives = list(self.store.path.parent.glob("game.before-reset-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), played_bytes)
        self.assert_start()

    def test_resuming_reset_does_not_archive_a_second_time(self) -> None:
        self.play("e2e4")
        played_bytes = self.store.path.read_bytes()
        fixture = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"
        controller = _geometry_from_file(fixture)

        class DisconnectingLink(SimulatedLink):
            def carry(self, source_id: str, destination_id: str, point_chars: str):
                raise OSError("serial disconnect during reset")

        with (
            patch("chess_table_host.play._connect_physical",
                  return_value=(DisconnectingLink(), controller)),
            patch("builtins.input", side_effect=AssertionError("unexpected reset prompt")),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["--resume", "--state", str(self.store.path),
                                   "--reset-board"]), 1)
        archives = list(self.store.path.parent.glob("game.before-reset-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_bytes(), played_bytes)
        resumed = GameRunner(self.store.load(), self.store, allow_tight_routes=True)
        resumed.resolve_uncertain_reset("source")
        with (
            patch("chess_table_host.play._connect_physical",
                  return_value=(SimulatedLink(), controller)),
            patch("builtins.input", side_effect=AssertionError("unexpected reset prompt")),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["--resume", "--state", str(self.store.path),
                                   "--reset-board"]), 0)
        self.assertEqual(len(list(self.store.path.parent.glob("game.before-reset-*.json"))), 1)
        self.assert_start()


if __name__ == "__main__":
    unittest.main()

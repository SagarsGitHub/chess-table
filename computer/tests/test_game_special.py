"""Physical-action checkpoints for special chess moves."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chess

from chess_table_host.checkpoint import PendingMove
from chess_table_host.game import GameRunner, GameState, JsonGameStore, MoveDeferred, ReconciliationRequired
from chess_table_host.game_errors import GameStateError
from chess_table_host.planner import (
    BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, RookOffset,
    Transfer,
)
from chess_table_host.play import _reconcile_at_start
from chess_table_host.routing import NoRouteError, RouteError, RouteGeometry


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
        self.offset_carries: list[tuple[str, str, str, str]] = []
        self.offsets: list[tuple[int, str, str]] = []

    def set_map(self, occupancy_hex: str) -> None:
        self.maps.append(occupancy_hex)

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        self.carries.append((source_id, destination_id, point_chars))
        return SimpleNamespace(command_id=len(self.carries))

    def offset(self, square_id: int, corner: str, direction: str):
        self.offsets.append((square_id, corner, direction))
        return SimpleNamespace(command_id=len(self.carries) + len(self.offsets))

    def carry_offset(self, source_id: str, corner: str,
                     destination_id: str, point_chars: str):
        self.offset_carries.append((source_id, corner, destination_id, point_chars))
        return SimpleNamespace(command_id=(
            len(self.carries) + len(self.offsets) + len(self.offset_carries)
        ))


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

    def test_opening_knights_stage_and_restore_even_with_tight_routes_enabled(self) -> None:
        for uci, pawn, staging in (("g1f3", "g2", "g3"), ("b1c3", "b2", "b3")):
            with self.subTest(uci=uci):
                runner = self.runner_at(chess.STARTING_FEN)
                runner.allow_tight_routes = True
                plan = runner.prepare_move(uci, GEOMETRY)
                self.assertEqual(plan.actions, (
                    Transfer("P", BoardSquare(pawn), BoardSquare(staging), "knight_stage"),
                    Transfer("N", BoardSquare(uci[:2]), BoardSquare(uci[2:]),
                             "knight_clearance_move"),
                    Transfer("P", BoardSquare(staging), BoardSquare(pawn), "knight_restore"),
                ))
                link = FakeLink()
                runner.execute_pending(link, GEOMETRY, lambda action: True)
                self.assertEqual(len(link.carries), 3)
                self.assertEqual(runner.state.physical_squares[pawn], "P")
                self.assertEqual(runner.state.physical_squares[uci[2:]], "N")
                self.assertNotIn(staging, runner.state.physical_squares)
                self.assertIsNone(runner.state.pending)

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

    def test_castling_checkpoints_rook_offset_before_king_transfer(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        before = runner.state.board_fen
        plan = runner.prepare_move("e1g1", GEOMETRY)
        self.assertEqual(
            plan.actions,
            (
                RookOffset("R", BoardSquare("h1"), "NE", "OUT", "castle_rook_out"),
                Transfer("K", BoardSquare("e1"), BoardSquare("g1"), "castle_king"),
                Transfer("R", BoardSquare("h1"), BoardSquare("f1"), "castle_rook_from_offset"),
            ),
        )
        link = FakeLink()
        confirmations: list[str] = []

        def confirm(action) -> bool:
            saved = self.store.load()
            self.assertEqual(saved.board_fen, before)
            self.assertEqual(saved.pending.phase, "awaiting_confirmation")
            if len(confirmations) == 1:
                self.assertEqual(saved.physical_squares["h1"], "R")
                self.assertEqual(saved.physical_squares["e1"], "K")
                self.assertNotIn("g1", saved.physical_squares)
            if len(confirmations) == 2:
                self.assertNotIn("e1", saved.physical_squares)
                self.assertEqual(saved.physical_squares["g1"], "K")
                self.assertEqual(saved.physical_squares["h1"], "R")
                self.assertNotIn("f1", saved.physical_squares)
            confirmations.append(action.reason)
            return True

        def approve(action, route, wire) -> bool:
            if isinstance(action, Transfer) and action.reason == "castle_rook_from_offset":
                self.assertIsNotNone(route.source_offset_mm)
                self.assertEqual(len(wire), 4)
                self.assertEqual(wire[1], "NE")
            return True

        runner.execute_pending(link, GEOMETRY, confirm, approve)
        self.assertEqual(confirmations, [
            "castle_rook_out", "castle_king", "castle_rook_from_offset",
        ])
        self.assertEqual(link.offsets, [(7, "NE", "OUT")])
        self.assertEqual([route[:2] for route in link.carries], [("4", "6")])
        self.assertEqual([route[:3] for route in link.offset_carries], [("7", "NE", "5")])
        self.assertEqual(len(link.maps), 3)
        self.assertEqual(link.maps[0], link.maps[1])
        self.assertNotEqual(link.maps[1], link.maps[2])
        self.assertEqual(runner.state.physical_squares["g1"], "K")
        self.assertEqual(runner.state.physical_squares["f1"], "R")
        self.assertTrue(all(piece is None for piece in runner.state.physical_parking))
        self.assertEqual(runner.state.moves, ["e1g1"])
        self.assertIsNone(runner.state.pending)

    def test_castling_resumes_after_rook_was_offset(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.prepare_move("e1g1", GEOMETRY)
        with self.assertRaises(MoveDeferred):
            runner.execute_pending(
                FakeLink(), GEOMETRY, lambda _action: True,
                approve_route=lambda action, _route, _wire: action.reason == "castle_rook_out",
            )
        saved = self.store.load()
        self.assertEqual(saved.pending.next_action, 1)
        self.assertEqual(saved.pending.phase, "ready")
        self.assertEqual(saved.physical_squares["h1"], "R")
        self.assertTrue(all(piece is None for piece in saved.physical_parking))

        resumed = GameRunner(saved, self.store)
        link = FakeLink()
        resumed.execute_pending(link, GEOMETRY, lambda _action: True)
        self.assertEqual([route[:2] for route in link.carries], [("4", "6")])
        self.assertEqual([route[:3] for route in link.offset_carries], [("7", "NE", "5")])
        self.assertEqual(link.offsets, [])
        self.assertEqual(resumed.state.moves, ["e1g1"])
        self.assertIsNone(resumed.state.pending)
        self.assertEqual(resumed.state.physical_squares["f1"], "R")

    def test_uncertain_rook_offset_requires_inspection_before_resuming(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.prepare_move("e1g1", GEOMETRY)

        class FailedOffset(FakeLink):
            def offset(self, square_id: int, corner: str, direction: str):
                super().offset(square_id, corner, direction)
                raise OSError("simulated disconnect during OFFSET")

        link = FailedOffset()
        with self.assertRaises(OSError):
            runner.execute_pending(link, GEOMETRY, lambda _action: True)
        saved = self.store.load()
        self.assertEqual(saved.pending.next_action, 0)
        self.assertEqual(saved.pending.phase, "in_flight")
        self.assertEqual(saved.pending.active_route, ("7", "NE", "OUT"))
        self.assertEqual(saved.physical_squares["h1"], "R")
        self.assertEqual(link.carries, [])

        resumed = GameRunner(saved, self.store)
        with self.assertRaises(ReconciliationRequired):
            resumed.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)
        # The operator has inspected the rook at the planned in-square corner.
        resumed.resolve_uncertain_action("destination")
        self.assertEqual(self.store.load().pending.next_action, 1)
        continuation = FakeLink()
        resumed.execute_pending(continuation, GEOMETRY, lambda _action: True)
        self.assertEqual(continuation.offsets, [])
        self.assertEqual(len(continuation.carries), 1)
        self.assertEqual(len(continuation.offset_carries), 1)
        self.assertEqual(resumed.state.moves, ["e1g1"])

    def test_uncertain_offset_carry_keeps_four_argument_route_and_resumes(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.prepare_move("e1g1", GEOMETRY)

        class FailedCarry(FakeLink):
            def carry_offset(self, source_id, corner, destination_id, point_chars):
                super().carry_offset(source_id, corner, destination_id, point_chars)
                raise OSError("simulated disconnect during CARRY_OFFSET")

        with self.assertRaises(OSError):
            runner.execute_pending(FailedCarry(), GEOMETRY, lambda _action: True)
        saved = self.store.load()
        self.assertEqual(saved.pending.next_action, 2)
        self.assertEqual(saved.pending.phase, "in_flight")
        self.assertEqual(saved.pending.active_route[:3], ("7", "NE", "5"))
        self.assertEqual(len(saved.pending.active_route), 4)
        self.assertEqual(saved.physical_squares["h1"], "R")
        self.assertEqual(saved.physical_squares["g1"], "K")

        resumed = GameRunner(saved, self.store)
        with self.assertRaises(ReconciliationRequired):
            resumed.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)
        # Inspection establishes that the rook is still at its offset corner.
        resumed.resolve_uncertain_action("source")
        continuation = FakeLink()
        resumed.execute_pending(continuation, GEOMETRY, lambda _action: True)
        self.assertEqual(len(continuation.offset_carries), 1)
        self.assertEqual(resumed.state.moves, ["e1g1"])

    def test_cli_recovery_names_offset_corner_as_rook_source(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.prepare_move("e1g1", GEOMETRY)

        class FailedCarry(FakeLink):
            def carry_offset(self, source_id, corner, destination_id, point_chars):
                raise OSError("simulated disconnect during offset carry")

        with self.assertRaises(OSError):
            runner.execute_pending(FailedCarry(), GEOMETRY, lambda _action: True)
        output = io.StringIO()
        with patch("builtins.input", side_effect=("source", "y", "y")):
            with redirect_stdout(output):
                self.assertTrue(_reconcile_at_start(runner))
        self.assertIn("SOURCE: restore the rook to the NE corner within h1", output.getvalue())
        self.assertIn("DESTINATION: verify the rook at f1", output.getvalue())
        self.assertIn("Inspect the castled king", output.getvalue())
        self.assertEqual(runner.state.pending.next_action, 2)
        self.assertEqual(runner.state.pending.phase, "ready")

    def test_crowded_castles_stage_pawn_and_restore_it_with_strict_routes(self) -> None:
        position = "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R"
        for turn, uci, pawn, staging, rook, corner, rook_home in (
            ("w", "e1g1", "g2", "g3", "h1", "NE", "f1"),
            ("w", "e1c1", "c2", "c3", "a1", "NW", "d1"),
            ("b", "e8g8", "g7", "g6", "h8", "SE", "f8"),
            ("b", "e8c8", "c7", "c6", "a8", "SW", "d8"),
        ):
            with self.subTest(uci=uci):
                runner = self.runner_at(f"{position} {turn} KQkq - 0 1")
                runner.allow_tight_routes = True
                before = runner.state.board_fen
                plan = runner.prepare_move(uci, GEOMETRY)
                self.assertFalse(plan.castle_clearance_override)
                self.assertEqual([action.reason for action in plan.actions], [
                    "castle_clearance_stage", "castle_rook_out", "castle_king",
                    "castle_rook_from_offset", "castle_clearance_restore",
                ])
                self.assertEqual(plan.actions[0].source, BoardSquare(pawn))
                self.assertEqual(plan.actions[0].destination, BoardSquare(staging))
                self.assertEqual(plan.actions[1].square, BoardSquare(rook))
                self.assertEqual(plan.actions[1].corner, corner)
                self.assertEqual(plan.actions[3].destination, BoardSquare(rook_home))
                self.assertEqual(plan.actions[4].destination, BoardSquare(pawn))
                routed = []

                def approve(_action, route, _wire):
                    routed.append(route)
                    self.assertFalse(route.clearance_ignored)
                    self.assertLessEqual(len(route.points), 12)
                    return True

                link = FakeLink()
                runner.execute_pending(link, GEOMETRY, lambda _action: True, approve)
                self.assertEqual(len(link.carries), 3)
                self.assertEqual(len(link.offset_carries), 1)
                self.assertEqual(len(link.offsets), 1)
                self.assertEqual(len(routed), 5)
                self.assertEqual(runner.state.moves, [uci])
                self.assertEqual(runner.state.physical_squares[pawn], "P" if turn == "w" else "p")
                self.assertNotIn(staging, runner.state.physical_squares)
                self.assertTrue(all(piece is None for piece in runner.state.physical_parking))
                self.assertNotEqual(runner.state.board_fen, before)
                self.assertEqual(self.store.load().to_json(), runner.state.to_json())

    def test_black_castle_stages_two_pawns_when_parked_bishop_blocks_release(self) -> None:
        # Reproduces the position after white's 29th ply in the physical GUI
        # game. P14 blocks the rook's northwest release extension if only g7
        # is staged; moving f7 too opens a southwest approach to f8.
        runner = self.runner_at(
            "r3k2r/pbppqppp/1pn1p3/4P3/2PPB3/5N2/3NQPPP/R4RK1 b kq - 2 15"
        )
        slots = list(runner.state.physical_parking)
        for slot, piece in ((2, "B"), (3, "P"), (4, "n"), (14, "b"), (18, "P")):
            slots[slot] = piece
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        runner.allow_tight_routes = True

        plan = runner.prepare_move("e8g8", GEOMETRY)
        self.assertFalse(plan.castle_clearance_override)
        self.assertEqual(
            [action.reason for action in plan.actions],
            ["castle_clearance_stage", "castle_clearance_stage", "castle_rook_out",
             "castle_king", "castle_rook_from_offset", "castle_clearance_restore",
             "castle_clearance_restore"],
        )
        self.assertEqual(
            [(action.source.name, action.destination.name)
             for action in (plan.actions[0], plan.actions[1], plan.actions[5], plan.actions[6])],
            [("g7", "g6"), ("f7", "f6"), ("f6", "f7"), ("g6", "g7")],
        )
        self.assertEqual(plan.actions[2].corner, "SE")
        self.assertEqual(plan.actions[4].source, BoardSquare("h8"))
        self.assertEqual(plan.actions[4].destination, BoardSquare("f8"))
        self.assertEqual(runner.state.physical_squares["e8"], "k")
        self.assertNotIn("g8", runner.state.physical_squares)
        self.assertEqual(self.store.load().pending.next_action, 0)

        routed = []

        def approve(_action, route, _wire):
            routed.append(route)
            self.assertFalse(route.clearance_ignored)
            self.assertLessEqual(len(route.points), 12)
            return True

        link = FakeLink()
        runner.execute_pending(link, GEOMETRY, lambda _action: True, approve)
        self.assertEqual(len(routed), 7)
        self.assertEqual(len(link.carries), 5)
        self.assertEqual(len(link.offset_carries), 1)
        self.assertEqual(len(link.offsets), 1)
        self.assertEqual(runner.state.moves[-1], "e8g8")
        self.assertEqual(runner.state.physical_squares["g8"], "k")
        self.assertEqual(runner.state.physical_squares["f8"], "r")
        self.assertEqual(runner.state.physical_squares["g7"], "p")
        self.assertEqual(runner.state.physical_squares["f7"], "p")
        self.assertNotIn("g6", runner.state.physical_squares)
        self.assertNotIn("f6", runner.state.physical_squares)
        self.assertEqual(runner.state.physical_parking, slots)
        self.assertIsNone(runner.state.pending)

    def test_black_castle_stages_bishop_and_pawn_when_g7_blocks_rook(self) -> None:
        # This physical GUI position has a bishop on g7 and a pawn on g6.
        # Both g7 and f7 must be cleared for the rook's h8-to-f8 carry
        # after the king has reached g8.
        runner = self.runner_at(
            "rnbqk2r/ppppppbp/6pn/8/3PPB2/5N2/PPP2PPP/RN1QKB1R b KQkq - 4 4"
        )
        runner.allow_tight_routes = True

        plan = runner.prepare_move("e8g8", GEOMETRY)
        self.assertFalse(plan.castle_clearance_override)
        self.assertEqual(
            [action.reason for action in plan.actions],
            ["castle_clearance_stage", "castle_clearance_stage", "castle_rook_out",
             "castle_king", "castle_rook_from_offset", "castle_clearance_restore",
             "castle_clearance_restore"],
        )
        self.assertEqual(
            [(action.piece, action.source.name, action.destination.name)
             for action in (plan.actions[0], plan.actions[1], plan.actions[5], plan.actions[6])],
            [("b", "g7", "f6"), ("p", "f7", "e6"),
             ("p", "e6", "f7"), ("b", "f6", "g7")],
        )
        self.assertEqual(plan.actions[2].corner, "SE")
        self.assertEqual(self.store.load().pending.next_action, 0)

        routed = []

        def approve(_action, route, _wire):
            routed.append(route)
            self.assertFalse(route.clearance_ignored)
            self.assertLessEqual(len(route.points), 12)
            return True

        runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: True, approve)
        self.assertEqual(len(routed), 7)
        self.assertEqual(runner.state.moves, ["e8g8"])
        self.assertEqual(runner.state.physical_squares["g8"], "k")
        self.assertEqual(runner.state.physical_squares["f8"], "r")
        self.assertEqual(runner.state.physical_squares["g7"], "b")
        self.assertEqual(runner.state.physical_squares["f7"], "p")
        self.assertNotIn("f6", runner.state.physical_squares)
        self.assertNotIn("e6", runner.state.physical_squares)
        self.assertIsNone(runner.state.pending)

    def test_castling_does_not_need_an_empty_outer_parking_corner(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.allow_tight_routes = True
        slots = list(runner.state.physical_parking)
        slots[8] = "p"
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        plan = runner.prepare_move("e1g1", GEOMETRY)
        self.assertFalse(plan.castle_clearance_override)
        self.assertEqual(plan.actions[0].corner, "NE")
        self.assertEqual(plan.parking_after.slots[8], "p")
        link = FakeLink()
        runner.execute_pending(link, GEOMETRY, lambda _action: True)
        self.assertEqual(runner.state.physical_parking[8], "p")
        self.assertEqual(runner.state.moves, ["e1g1"])

    def test_castling_tries_other_in_square_corner_when_one_is_blocked(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        slots = list(runner.state.physical_parking)
        slots[25] = "p"  # Adjacent to the preferred h1 NE offset.
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        plan = runner.prepare_move("e1g1", GEOMETRY)
        self.assertEqual(plan.actions[0].corner, "SE")
        self.assertEqual(plan.actions[2].reason, "castle_rook_from_offset")

    def test_last_resort_castle_keeps_rook_offset_across_checkpoint_reload(self) -> None:
        # An opposing knight on f7 and three pawns on rank six close the strict
        # approaches. No friendly rank-seven piece can be staged. A parked
        # bishop beside the rook delivery closes the remaining release angle.
        runner = self.runner_at("4k2r/5N2/5ppp/8/8/8/8/K7 b k - 0 1")
        slots = list(runner.state.physical_parking)
        slots[14] = "b"
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        with self.assertRaises(RouteError):
            runner.prepare_move("e8g8", GEOMETRY)
        self.assertIsNone(self.store.load().pending)

        runner.allow_tight_routes = True
        plan = runner.prepare_move("e8g8", GEOMETRY)
        self.assertTrue(plan.castle_clearance_override)
        self.assertEqual(
            [action.reason for action in plan.actions],
            ["castle_rook_out", "castle_king", "castle_rook_from_offset"],
        )
        self.assertEqual(plan.actions[0].corner, "SE")
        saved = self.store.load()
        self.assertTrue(saved.pending.castle_clearance_override)
        self.assertTrue(saved.events[-1]["castle_clearance_override"])

        link = FakeLink()

        def approve_first(action, route, _wire):
            self.assertFalse(route.clearance_ignored)
            return action.reason == "castle_rook_out"

        with self.assertRaises(MoveDeferred):
            runner.execute_pending(
                link, GEOMETRY, lambda _action: True,
                approve_route=approve_first,
            )
        self.assertEqual(self.store.load().pending.next_action, 1)
        self.assertEqual(len(link.offsets), 1)
        self.assertEqual(link.carries, [])

        strict_resume = GameRunner(self.store.load(), self.store)
        with self.assertRaisesRegex(ReconciliationRequired, "--allow-tight-routes"):
            strict_resume.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)
        self.assertEqual(self.store.load().pending.next_action, 1)

        resumed = GameRunner(self.store.load(), self.store, allow_tight_routes=True)
        routed = []

        def approve(_action, route, _wire):
            routed.append(route)
            self.assertLessEqual(len(route.points), 12)
            return True

        resumed.execute_pending(link, GEOMETRY, lambda _action: True, approve)
        self.assertEqual(len(routed), 2)
        # The king still has a strict route; only the rook needs the override.
        self.assertEqual([route.clearance_ignored for route in routed], [False, True])
        self.assertEqual(len(link.carries), 1)
        self.assertEqual(len(link.offset_carries), 1)
        self.assertEqual(resumed.state.moves, ["e8g8"])
        self.assertIsNone(resumed.state.pending)

    def test_last_resort_castle_cannot_bypass_rook_offset_clearance(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        runner.allow_tight_routes = True
        slots = list(runner.state.physical_parking)
        slots[8] = "p"
        slots[25] = "p"
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        with self.assertRaises(NoRouteError):
            runner.prepare_move("e1g1", GEOMETRY)
        self.assertIsNone(self.store.load().pending)

    def test_castle_override_checkpoint_flag_requires_castling(self) -> None:
        runner = self.runner_at("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        runner.prepare_move("e1e2", GEOMETRY)
        saved = runner.state.to_json()
        pending = saved["pending"]
        del pending["castle_clearance_override"]
        self.assertFalse(GameState.from_json(saved).pending.castle_clearance_override)
        pending["castle_clearance_override"] = True
        with self.assertRaisesRegex(GameStateError, "requires an offset-rook castling plan"):
            GameState.from_json(saved)

    def test_castling_fails_before_motion_when_both_outer_offsets_are_blocked(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        slots = list(runner.state.physical_parking)
        slots[8] = "p"
        slots[25] = "p"
        runner.state.physical_parking = slots
        runner.state.committed_parking = ParkingInventory(tuple(slots))
        self.store.save(runner.state)
        with self.assertRaises(NoRouteError):
            runner.prepare_move("e1g1", GEOMETRY)
        self.assertIsNone(self.store.load().pending)

    def test_legacy_king_first_castling_checkpoint_requires_reconciliation(self) -> None:
        runner = self.runner_at("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        after = runner.state.board()
        after.push_uci("e1g1")
        runner.state.pending = PendingMove(
            "e1g1", after.fen(), ParkingInventory.empty(),
            (
                Transfer("K", BoardSquare("e1"), BoardSquare("g1"), "castle_king"),
                Transfer("R", BoardSquare("h1"), BoardSquare("f1"), "castle_rook"),
            ),
        )
        self.store.save(runner.state)
        link = FakeLink()
        with self.assertRaisesRegex(ReconciliationRequired, "king-first castling"):
            GameRunner(self.store.load(), self.store).execute_pending(
                link, GEOMETRY, lambda _action: True,
            )
        self.assertEqual(link.carries, [])
        self.assertEqual(self.store.load().pending.next_action, 0)

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

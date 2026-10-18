"""Stage 4 checkpoints around actual measured table geometry."""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chess

from chess_table_host.game import (
    GameRunner,
    GameState,
    JsonGameStore,
    MoveDeferred,
    ReconciliationRequired,
)
from chess_table_host.planner import BoardSquare, ParkingInventory, ParkingSlot, Transfer
from chess_table_host.routing import NoRouteError, RouteGeometry


# The dimensions are the board's reported @4 DONE GEOM values. Diameters are
# provisional test inputs until the physical pieces and moving head are measured.
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
    def __init__(self, *, fail_carry: bool = False, on_carry=None) -> None:
        self.maps: list[str] = []
        self.carries: list[tuple[str, str, str]] = []
        self.fail_carry = fail_carry
        self.on_carry = on_carry

    def set_map(self, occupancy_hex: str) -> None:
        self.maps.append(occupancy_hex)

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        self.carries.append((source_id, destination_id, point_chars))
        if self.on_carry is not None:
            self.on_carry()
        if self.fail_carry:
            raise OSError("simulated serial disconnect during CARRY")
        return SimpleNamespace(command_id=len(self.carries))


class GameRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JsonGameStore(Path(temporary.name) / "game.json")
        self.runner = GameRunner(GameState.new(), self.store)
        self.store.save(self.runner.state)

    def play(self, uci: str) -> None:
        self.runner.prepare_move(uci, GEOMETRY)
        self.runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)

    def runner_at(self, fen: str) -> GameRunner:
        board = chess.Board(fen)
        self.assertTrue(board.is_valid())
        squares = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        runner = GameRunner(
            GameState(board.fen(), ParkingInventory.empty(), squares, [None] * 32),
            self.store,
        )
        self.store.save(runner.state)
        return runner

    def test_midgame_knight_stages_black_blocker_with_strict_routes(self) -> None:
        runner = self.runner_at(
            "k7/8/6pp/3pppp1/3PN3/3B4/3PP3/4K3 w - - 0 1"
        )
        plan = runner.prepare_move("e4f6", GEOMETRY)
        self.assertEqual(plan.actions, (
            Transfer("p", BoardSquare("e5"), BoardSquare("e6"), "knight_stage"),
            Transfer("N", BoardSquare("e4"), BoardSquare("f6"), "knight_clearance_move"),
            Transfer("p", BoardSquare("e6"), BoardSquare("e5"), "knight_restore"),
        ))
        routes = []
        runner.execute_pending(
            FakeLink(), GEOMETRY, lambda _action: True,
            lambda _action, route, _wire: routes.append(route) or True,
        )
        self.assertEqual(len(routes), 3)
        self.assertTrue(all(not route.clearance_ignored for route in routes))
        self.assertEqual(runner.state.physical_squares["f6"], "N")
        self.assertEqual(runner.state.physical_squares["e5"], "p")
        self.assertEqual(runner.state.moves, ["e4f6"])

    def test_short_rook_move_avoids_unnecessary_neighbor_staging(self) -> None:
        geometry = replace(GEOMETRY, moving_diameter_mm=23, occupied_diameter_mm=23)
        runner = self.runner_at("k7/8/8/8/3PRP2/8/8/4K3 w - - 0 1")
        plan = runner.prepare_move("e4e6", geometry)
        self.assertEqual(plan.actions, (
            Transfer("R", BoardSquare("e4"), BoardSquare("e6"), "move"),
        ))
        link = FakeLink()
        runner.execute_pending(link, geometry, lambda _action: True)
        self.assertEqual(link.carries, [("28", "44", "979B")])
        self.assertEqual(runner.state.physical_squares["e6"], "R")
        self.assertEqual(runner.state.physical_squares["d4"], "P")
        self.assertEqual(runner.state.moves, ["e4e6"])

    def test_longer_midboard_rook_move_stages_pawn_when_clearance_is_tighter(self) -> None:
        geometry = replace(GEOMETRY, moving_diameter_mm=23, occupied_diameter_mm=23)
        runner = self.runner_at("k7/8/8/8/3PRP2/8/8/4K3 w - - 0 1")
        plan = runner.prepare_move("e4e7", geometry)
        self.assertEqual(plan.actions, (
            Transfer("P", BoardSquare("d4"), BoardSquare("c4"), "clearance_stage"),
            Transfer("R", BoardSquare("e4"), BoardSquare("e7"), "clearance_move"),
            Transfer("P", BoardSquare("c4"), BoardSquare("d4"), "clearance_restore"),
        ))
        runner.execute_pending(FakeLink(), geometry, lambda _action: True)
        self.assertEqual(runner.state.physical_squares["e7"], "R")
        self.assertEqual(runner.state.physical_squares["d4"], "P")
        self.assertEqual(runner.state.moves, ["e4e7"])

    def prepare_capture(self) -> tuple[str, ParkingSlot]:
        self.play("e2e4")
        self.play("d7d5")
        before = self.runner.state.board_fen
        plan = self.runner.prepare_move("e4d5", GEOMETRY)
        self.assertEqual(len(plan.actions), 2)
        victim, attacker = plan.actions
        self.assertEqual(victim, Transfer("p", BoardSquare("d5"), victim.destination, "capture"))
        self.assertEqual(attacker, Transfer("P", BoardSquare("e4"), BoardSquare("d5"), "move"))
        self.assertIsInstance(victim.destination, ParkingSlot)
        return before, victim.destination

    def test_capture_parks_victim_before_attacker_and_persists_each_checkpoint(self) -> None:
        before, slot = self.prepare_capture()
        # P00 is blocked in this position with the provisional 18 mm footprint;
        # the runner must use a reachable slot rather than the first vacancy.
        self.assertNotEqual(slot.slot_id, 0)
        in_flight: list[tuple[int, str]] = []
        awaiting: list[tuple[int, str]] = []

        def on_carry() -> None:
            saved = self.store.load()
            self.assertIsNotNone(saved.pending)
            self.assertEqual(saved.pending.phase, "in_flight")
            self.assertEqual(saved.board_fen, before)
            in_flight.append((saved.pending.next_action, saved.physical_squares.get("d5", "")))

        def confirm(action) -> bool:
            saved = self.store.load()
            self.assertIsNotNone(saved.pending)
            self.assertEqual(saved.pending.phase, "awaiting_confirmation")
            self.assertEqual(saved.board_fen, before)
            awaiting.append((saved.pending.next_action, action.reason))
            return True

        link = FakeLink(on_carry=on_carry)
        self.runner.execute_pending(link, GEOMETRY, confirm)
        self.assertEqual(in_flight, [(0, "p"), (1, "")])
        self.assertEqual(awaiting, [(0, "capture"), (1, "move")])
        self.assertEqual(len(link.carries), 2)
        self.assertNotEqual(link.maps[0], link.maps[1])
        self.assertEqual(self.runner.state.physical_parking[slot.slot_id], "p")
        self.assertEqual(self.runner.state.physical_squares["d5"], "P")
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.runner.state.moves, ["e2e4", "d7d5", "e4d5"])
        self.assertEqual(self.store.load().to_json(), self.runner.state.to_json())

    def test_capture_checks_other_parking_slots_after_first_route_fails(self) -> None:
        self.play("e2e4")
        self.play("d7d5")
        from chess_table_host import game_preflight as game

        original_stage = game.plan_cleared_transfer
        original_offset = game.plan_offset_cleared_transfer

        def block_first_slot_stage(squares, slots, transfer, geometry):
            if (transfer.reason == "capture" and
                    transfer.destination == ParkingSlot(0)):
                return None
            return original_stage(squares, slots, transfer, geometry)

        def block_first_slot_offset(transfer, squares, slots, geometry):
            if (transfer.reason == "capture" and
                    transfer.destination == ParkingSlot(0)):
                return None
            return original_offset(transfer, squares, slots, geometry)

        # Force an initially chosen slot that has no strict direct or staged
        # route. Preflight must try another vacancy for the complete capture.
        with (
            patch.object(game.ReachableParkingSlotSelector, "__call__",
                         return_value=ParkingSlot(0)),
            patch.object(game, "plan_cleared_transfer",
                         side_effect=block_first_slot_stage),
            patch.object(game, "plan_offset_cleared_transfer",
                         side_effect=block_first_slot_offset),
        ):
            plan = self.runner.prepare_move("e4d5", GEOMETRY)
        transfers = [action for action in plan.actions if isinstance(action, Transfer)]
        self.assertNotEqual(transfers[0].destination, ParkingSlot(0))
        self.assertEqual(transfers[-1].destination, BoardSquare("d5"))
        self.assertIsNotNone(self.runner.state.pending)

    def _fault_on_capture_victim(self) -> tuple[str, ParkingSlot]:
        before, slot = self.prepare_capture()
        with self.assertRaises(OSError):
            self.runner.execute_pending(FakeLink(fail_carry=True), GEOMETRY, lambda _action: True)
        saved = self.store.load()
        self.assertEqual(saved.board_fen, before)
        self.assertEqual(saved.pending.phase, "in_flight")
        self.assertEqual(saved.pending.next_action, 0)
        self.assertEqual(saved.physical_squares["d5"], "p")
        self.assertIsNone(saved.physical_parking[slot.slot_id])
        return before, slot

    def test_fault_during_capture_can_reconcile_piece_at_source_and_retry(self) -> None:
        before, slot = self._fault_on_capture_victim()
        self.runner.resolve_uncertain_action("source")
        saved = self.store.load()
        self.assertEqual(saved.pending.phase, "ready")
        self.assertEqual(saved.pending.next_action, 0)
        self.assertEqual(saved.board_fen, before)
        self.assertEqual(saved.physical_squares["d5"], "p")
        self.assertEqual(saved.events[-1]["action"], 0)
        self.assertEqual(saved.events[-1]["outcome"], "source")
        self.runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: True)
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.runner.state.physical_parking[slot.slot_id], "p")
        self.assertEqual(self.runner.state.physical_squares["d5"], "P")

    def test_fault_during_capture_can_reconcile_piece_at_destination(self) -> None:
        before, slot = self._fault_on_capture_victim()
        self.runner.resolve_uncertain_action("destination")
        saved = self.store.load()
        self.assertEqual(saved.pending.phase, "ready")
        self.assertEqual(saved.pending.next_action, 1)
        self.assertEqual(saved.board_fen, before)
        self.assertNotIn("d5", saved.physical_squares)
        self.assertEqual(saved.physical_parking[slot.slot_id], "p")
        self.assertEqual(saved.events[-1]["action"], 0)
        self.assertEqual(saved.events[-1]["outcome"], "destination")
        link = FakeLink()
        self.runner.execute_pending(link, GEOMETRY, lambda _action: True)
        self.assertEqual(len(link.carries), 1)
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.runner.state.physical_squares["d5"], "P")

    def test_second_leg_preflight_failure_does_not_modify_state_or_file(self) -> None:
        self.play("e2e4")
        self.play("d7d5")
        before_state = self.runner.state.to_json()
        before_file = self.store.path.read_bytes()
        from chess_table_host import game_preflight as game

        actual_route = game.plan_transfer_route
        failed_attacker_legs = 0

        def fail_second_leg(action, **kwargs):
            nonlocal failed_attacker_legs
            if action.reason in {"move", "clearance_move"}:
                failed_attacker_legs += 1
                raise NoRouteError("second leg blocked")
            return actual_route(action, **kwargs)

        with patch.object(game, "plan_transfer_route", side_effect=fail_second_leg):
            with self.assertRaises(NoRouteError):
                self.runner.prepare_move("e4d5", GEOMETRY)
        self.assertGreater(failed_attacker_legs, 1)
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.runner.state.to_json(), before_state)
        self.assertEqual(self.store.path.read_bytes(), before_file)

    def test_carry_completion_does_not_commit_without_operator_confirmation(self) -> None:
        before = self.runner.state.board_fen
        self.runner.prepare_move("e2e4", GEOMETRY)
        with self.assertRaises(ReconciliationRequired):
            self.runner.execute_pending(FakeLink(), GEOMETRY, lambda _action: False)
        saved = self.store.load()
        self.assertEqual(saved.board_fen, before)
        self.assertEqual(saved.moves, [])
        self.assertEqual(saved.pending.phase, "awaiting_confirmation")
        self.assertEqual(saved.physical_squares["e2"], "P")
        self.assertNotIn("e4", saved.physical_squares)
        self.runner.resolve_uncertain_action("destination")
        self.assertIsNone(self.runner.state.pending)
        self.assertEqual(self.runner.state.moves, ["e2e4"])
        self.assertEqual(self.runner.state.physical_squares["e4"], "P")

    def test_crowded_knight_stages_pawn_with_strict_routes_and_restores_it(self) -> None:
        before = self.runner.state.to_json()
        preview = self.runner.preflight_move("g1f3", GEOMETRY)
        self.assertEqual(self.runner.state.to_json(), before)

        runner = GameRunner(self.runner.state, self.store, allow_tight_routes=True)
        plan = runner.prepare_move("g1f3", GEOMETRY)
        self.assertEqual(plan.actions, preview.actions)
        self.assertEqual(plan.actions, (
            Transfer("P", BoardSquare("g2"), BoardSquare("g3"), "knight_stage"),
            Transfer("N", BoardSquare("g1"), BoardSquare("f3"), "knight_clearance_move"),
            Transfer("P", BoardSquare("g3"), BoardSquare("g2"), "knight_restore"),
        ))
        routed = []

        def approve(_action, route, wire) -> bool:
            routed.append(route)
            self.assertTrue(route.geometry_applied)
            self.assertFalse(route.clearance_ignored)
            self.assertLessEqual(len(route.points), 12)
            self.assertLessEqual(len(wire[2]), 24)
            return True

        link = FakeLink()
        runner.execute_pending(
            link, GEOMETRY, lambda _action: True, approve,
            confirmation_source="controller_done",
        )
        self.assertEqual(len(routed), 3)
        self.assertEqual(len(link.carries), 3)
        self.assertEqual(runner.state.moves, ["g1f3"])
        self.assertEqual(runner.state.physical_squares["f3"], "N")
        self.assertEqual(runner.state.physical_squares["g2"], "P")
        self.assertNotIn("g3", runner.state.physical_squares)
        self.assertNotIn("g1", runner.state.physical_squares)
        self.assertIsNone(runner.state.pending)
        started = [event for event in runner.state.events if event["event"] == "carry_started"]
        confirmed = [event for event in runner.state.events if event["event"] == "action_confirmed"]
        self.assertEqual([event["clearance_override"] for event in started], [False] * 3)
        self.assertEqual([event["confirmation_source"] for event in confirmed], ["controller_done"] * 3)
        self.assertEqual(self.store.load().to_json(), runner.state.to_json())

    def test_crowded_knight_resumes_after_staging_pawn(self) -> None:
        self.runner.prepare_move("b1c3", GEOMETRY)
        with self.assertRaises(MoveDeferred):
            self.runner.execute_pending(
                FakeLink(), GEOMETRY, lambda _action: True,
                approve_route=lambda action, _route, _wire: action.reason == "knight_stage",
            )
        saved = self.store.load()
        self.assertEqual(saved.pending.next_action, 1)
        self.assertEqual(saved.pending.phase, "ready")
        self.assertEqual(saved.physical_squares["b3"], "P")
        self.assertNotIn("b2", saved.physical_squares)
        self.assertEqual(saved.physical_squares["b1"], "N")
        self.assertEqual(saved.moves, [])

        resumed = GameRunner(saved, self.store, allow_tight_routes=True)
        routes = []

        def approve(action, route, _wire) -> bool:
            routes.append((action.reason, route.clearance_ignored))
            return True

        link = FakeLink()
        resumed.execute_pending(link, GEOMETRY, lambda _action: True, approve)
        self.assertEqual(routes, [
            ("knight_clearance_move", False),
            ("knight_restore", False),
        ])
        self.assertEqual(len(link.carries), 2)
        self.assertEqual(resumed.state.moves, ["b1c3"])
        self.assertEqual(resumed.state.physical_squares["c3"], "N")
        self.assertEqual(resumed.state.physical_squares["b2"], "P")
        self.assertNotIn("b3", resumed.state.physical_squares)
        self.assertIsNone(resumed.state.pending)
        self.assertEqual(self.store.load().to_json(), resumed.state.to_json())


if __name__ == "__main__":
    unittest.main()

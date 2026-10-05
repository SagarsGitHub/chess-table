"""Stage 4 checkpoints around actual measured table geometry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chess_table_host.game import (
    GameRunner,
    GameState,
    JsonGameStore,
    ReconciliationRequired,
)
from chess_table_host.planner import BoardSquare, ParkingSlot, Transfer
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
        from chess_table_host import game

        actual_route = game.plan_transfer_route
        calls = 0

        def fail_second_leg(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise NoRouteError("second leg blocked")
            return actual_route(*args, **kwargs)

        with patch.object(game, "plan_transfer_route", side_effect=fail_second_leg):
            with self.assertRaises(NoRouteError):
                self.runner.prepare_move("e4d5", GEOMETRY)
        self.assertEqual(calls, 2)
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


if __name__ == "__main__":
    unittest.main()

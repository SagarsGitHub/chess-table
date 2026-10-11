"""Temporary blocker relocation for crowded physical carries."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import chess

from chess_table_host.knight_stage import plan_cleared_transfer, plan_tight_knight_move
from chess_table_host.physical import _apply_action, _board_pieces, _occupied_slots, _occupied_squares
from chess_table_host.planner import BoardSquare, ParkingSlot, Transfer
from chess_table_host.routing import NoRouteError, RouteGeometry, RouteError, plan_transfer_route


GEOMETRY = RouteGeometry(
    31.25, 18, 18, 0,
    x_travel_mm=249.735, y_travel_mm=251.985,
    board_origin_x_mm=-0.1325, board_origin_y_mm=0.9925,
    end_inset_mm=2, pulses_per_mm=133.3333,
)


class KnightStageTests(unittest.TestCase):
    def test_four_crowded_opening_knights_stage_pawn_and_restore(self) -> None:
        squares_before = _board_pieces(chess.Board())
        slots_before = [None] * 32
        for source, destination, blocker, staging in (
            ("b1", "c3", "b2", "b3"),
            ("g1", "f3", "g2", "g3"),
            ("b8", "c6", "b7", "b6"),
            ("g8", "f6", "g7", "g6"),
        ):
            with self.subTest(source=source):
                knight = Transfer(
                    squares_before[source], BoardSquare(source),
                    BoardSquare(destination), "move",
                )
                with self.assertRaises(RouteError):
                    plan_transfer_route(
                        knight,
                        occupied_squares=_occupied_squares(squares_before),
                        occupied_parking_slots=(), geometry=GEOMETRY,
                        max_waypoints=12,
                    )
                actions = plan_tight_knight_move(
                    squares_before, slots_before, knight, GEOMETRY,
                )
                self.assertIsNotNone(actions)
                self.assertEqual(
                    actions,
                    (
                        Transfer(squares_before[blocker], BoardSquare(blocker),
                                 BoardSquare(staging), "knight_stage"),
                        Transfer(squares_before[source], BoardSquare(source),
                                 BoardSquare(destination), "knight_clearance_move"),
                        Transfer(squares_before[blocker], BoardSquare(staging),
                                 BoardSquare(blocker), "knight_restore"),
                    ),
                )
                squares, slots = dict(squares_before), list(slots_before)
                for action in actions:
                    route = plan_transfer_route(
                        action,
                        occupied_squares=_occupied_squares(squares),
                        occupied_parking_slots=_occupied_slots(slots),
                        geometry=GEOMETRY, max_waypoints=12,
                    )
                    self.assertTrue(route.geometry_applied)
                    self.assertFalse(route.clearance_ignored)
                    self.assertLessEqual(len(route.points), 12)
                    _apply_action(squares, slots, action)
                expected = dict(squares_before)
                expected[destination] = expected.pop(source)
                self.assertEqual(squares, expected)
                self.assertEqual(slots, slots_before)
                self.assertEqual(squares_before, _board_pieces(chess.Board()))

    def test_no_extra_carries_when_knight_already_has_strict_route(self) -> None:
        knight = Transfer("N", BoardSquare("b1"), BoardSquare("c3"), "move")
        self.assertIsNone(plan_tight_knight_move({"b1": "N"}, [None] * 32, knight, GEOMETRY))

    def test_parking_is_considered_after_all_board_staging_fails(self) -> None:
        # Isolate the search policy: the route planner rejects every board
        # staging destination, but accepts an empty parking slot and the two
        # subsequent legs. Real routes are covered by the symmetry test.
        squares = {"b1": "N", "b2": "P", "c2": "P"}
        knight = Transfer("N", BoardSquare("b1"), BoardSquare("c3"), "move")
        tried_board = []

        def selective_route(action, **_options):
            if action.reason == "move":
                raise NoRouteError("direct route blocked")
            if action.reason == "knight_stage" and isinstance(action.destination, BoardSquare):
                tried_board.append(action.destination)
                raise NoRouteError("board staging blocked")
            return object()

        with patch("chess_table_host.knight_stage.plan_transfer_route", side_effect=selective_route):
            actions = plan_tight_knight_move(squares, [None] * 32, knight, GEOMETRY)
        self.assertTrue(tried_board)
        self.assertIsNotNone(actions)
        self.assertEqual(actions[0].destination, ParkingSlot(0))
        self.assertEqual(actions[1].reason, "knight_clearance_move")
        self.assertEqual(actions[2].reason, "knight_restore")

    def test_no_plan_without_a_vacant_staging_location(self) -> None:
        # The knight destination is the only vacant board square, and every
        # parking slot is occupied. Staging there would block the knight.
        squares = {name: "P" for name in chess.SQUARE_NAMES if name != "c3"}
        squares["b1"] = "N"
        knight = Transfer("N", BoardSquare("b1"), BoardSquare("c3"), "move")
        self.assertIsNone(plan_tight_knight_move(squares, ["p"] * 32, knight, GEOMETRY))

    def test_unsupported_or_mismatched_transfer_returns_none(self) -> None:
        squares = {"b1": "N", "b2": "P"}
        for transfer in (
            Transfer("B", BoardSquare("b1"), BoardSquare("c3"), "move"),
            Transfer("N", ParkingSlot(0), BoardSquare("c3"), "move"),
            Transfer("N", BoardSquare("b1"), ParkingSlot(0), "move"),
            Transfer("n", BoardSquare("b1"), BoardSquare("c3"), "move"),
        ):
            with self.subTest(transfer=transfer):
                self.assertIsNone(plan_tight_knight_move(squares, [None] * 32, transfer, GEOMETRY))


class GeneralClearanceStageTests(unittest.TestCase):
    def assert_strict_equivalent(
        self, squares_before: dict[str, str], slots_before: list[str | None],
        original: Transfer, actions: tuple[Transfer, Transfer, Transfer],
        geometry: RouteGeometry,
    ) -> None:
        expected_squares, expected_slots = dict(squares_before), list(slots_before)
        _apply_action(expected_squares, expected_slots, original)
        squares, slots = dict(squares_before), list(slots_before)
        for action in actions:
            route = plan_transfer_route(
                action,
                occupied_squares=_occupied_squares(squares),
                occupied_parking_slots=_occupied_slots(slots),
                geometry=geometry, max_waypoints=12,
            )
            self.assertTrue(route.geometry_applied)
            self.assertFalse(route.clearance_ignored)
            self.assertLessEqual(len(route.points), 12)
            _apply_action(squares, slots, action)
        self.assertEqual(squares, expected_squares)
        self.assertEqual(slots, expected_slots)

    def test_midboard_rook_and_queen_carries_can_stage_a_blocker(self) -> None:
        # With 23 mm measured pieces, each neighboring piece closes an
        # approach from e4. The same one-blocker search works for two kinds
        # of legal sliding move, well away from the opening knight squares.
        wide_geometry = replace(GEOMETRY, moving_diameter_mm=23, occupied_diameter_mm=23)
        slots = [None] * 32
        for piece in ("R", "Q"):
            with self.subTest(piece=piece):
                squares = {"e4": piece, "d4": "P", "f4": "P"}
                original = Transfer(piece, BoardSquare("e4"), BoardSquare("e6"), "move")
                with self.assertRaises(RouteError):
                    plan_transfer_route(
                        original, occupied_squares=_occupied_squares(squares),
                        occupied_parking_slots=(), geometry=wide_geometry,
                        max_waypoints=12,
                    )
                actions = plan_cleared_transfer(squares, slots, original, wide_geometry)
                self.assertEqual(actions, (
                    Transfer("P", BoardSquare("d4"), BoardSquare("c4"), "clearance_stage"),
                    Transfer(piece, BoardSquare("e4"), BoardSquare("e6"), "clearance_move"),
                    Transfer("P", BoardSquare("c4"), BoardSquare("d4"), "clearance_restore"),
                ))
                self.assert_strict_equivalent(squares, slots, original, actions, wide_geometry)

    def test_capture_victim_can_be_staged_into_corner_parking(self) -> None:
        # The obstacle is near the parking destination, not the victim source.
        squares = {"c3": "p", "a1": "R"}
        slots = [None] * 32
        original = Transfer("p", BoardSquare("c3"), ParkingSlot(0), "capture")
        with self.assertRaises(RouteError):
            plan_transfer_route(
                original, occupied_squares=_occupied_squares(squares),
                occupied_parking_slots=(), geometry=GEOMETRY,
                max_waypoints=12,
            )
        actions = plan_cleared_transfer(squares, slots, original, GEOMETRY)
        self.assertEqual(actions, (
            Transfer("R", BoardSquare("a1"), BoardSquare("a2"), "clearance_stage"),
            Transfer("p", BoardSquare("c3"), ParkingSlot(0), "clearance_move"),
            Transfer("R", BoardSquare("a2"), BoardSquare("a1"), "clearance_restore"),
        ))
        self.assert_strict_equivalent(squares, slots, original, actions, GEOMETRY)

    def test_midgame_knight_stages_a_local_blocker(self) -> None:
        # The search is based on current occupancy, with no opening-square
        # or pawn-file rule. A later knight carry can clear e5 and restore it.
        board = chess.Board("k7/8/6pp/3pppp1/3PN3/3B4/3PP3/4K3 w - - 0 1")
        self.assertTrue(board.is_valid())
        self.assertTrue(board.is_legal(chess.Move.from_uci("e4f6")))
        squares = _board_pieces(board)
        slots = [None] * 32
        original = Transfer("N", BoardSquare("e4"), BoardSquare("f6"), "move")
        with self.assertRaises(RouteError):
            plan_transfer_route(
                original, occupied_squares=_occupied_squares(squares),
                occupied_parking_slots=(), geometry=GEOMETRY,
                max_waypoints=12,
            )
        actions = plan_cleared_transfer(squares, slots, original, GEOMETRY)
        self.assertEqual(actions, (
            Transfer("p", BoardSquare("e5"), BoardSquare("e6"), "clearance_stage"),
            Transfer("N", BoardSquare("e4"), BoardSquare("f6"), "clearance_move"),
            Transfer("p", BoardSquare("e6"), BoardSquare("e5"), "clearance_restore"),
        ))
        self.assert_strict_equivalent(squares, slots, original, actions, GEOMETRY)

    def test_no_single_blocker_solution_returns_none(self) -> None:
        # There is nowhere for a blocker to go: the only empty board square
        # is the destination, and all perimeter parking is occupied.
        squares = {name: "P" for name in chess.SQUARE_NAMES if name != "f6"}
        squares["e4"] = "R"
        original = Transfer("R", BoardSquare("e4"), BoardSquare("f6"), "move")
        self.assertIsNone(plan_cleared_transfer(squares, ["p"] * 32, original, GEOMETRY))


if __name__ == "__main__":
    unittest.main()

"""Measured geometry for general within-square piece offsets."""

import unittest

from chess_table_host.corner_offset import offset_target_mm, plan_offset, plan_rook_offset
from chess_table_host.physical import _apply_action
from chess_table_host.planner import BoardSquare, PieceOffset, RookOffset, Transfer
from chess_table_host.routing import (
    NoRouteError, RouteError, RouteGeometry, _point_on_segment,
    location_point, plan_transfer_route,
)


def geometry(diameter: float = 18.0) -> RouteGeometry:
    return RouteGeometry(
        31.25, diameter, diameter,
        x_travel_mm=260, y_travel_mm=260,
        end_inset_mm=2, pulses_per_mm=1600 / 12,
    )


def nudge(piece: str, square: str, corner: str, direction: str = "OUT") -> PieceOffset:
    reason = "clearance_offset_out" if direction == "OUT" else "clearance_offset_in"
    return PieceOffset(piece, BoardSquare(square), corner, direction, reason)


class PieceOffsetGeometryTests(unittest.TestCase):
    def test_piece_offset_preserves_logical_occupancy_and_restores(self) -> None:
        squares = {"d4": "N"}
        slots = [None] * 32
        out = nudge("N", "d4", "NE")
        back = nudge("N", "d4", "NE", "IN")
        offset = plan_offset(out, squares, slots, geometry())
        self.assertNotEqual(offset.start_mm, offset.end_mm)
        _apply_action(squares, slots, out)
        self.assertEqual(squares, {"d4": "N"})
        displaced = {out.square: offset_target_mm(out, geometry())}
        restored = plan_offset(back, squares, slots, geometry(),
                               offset_positions_mm=displaced)
        self.assertEqual(restored.start_mm, offset.end_mm)
        self.assertEqual(restored.end_mm, offset.start_mm)
        _apply_action(squares, slots, back)
        self.assertEqual(squares, {"d4": "N"})

    def test_second_offset_checks_actual_displaced_piece(self) -> None:
        squares = {"d4": "N", "e4": "p"}
        slots = [None] * 32
        first = nudge("p", "e4", "NW")
        second = nudge("N", "d4", "NE")
        measured = geometry(12)
        plan_offset(first, squares, slots, measured)
        plan_offset(second, squares, slots, measured)
        with self.assertRaisesRegex(NoRouteError, "clearance"):
            plan_offset(second, squares, slots, measured,
                        offset_positions_mm={first.square: offset_target_mm(first, measured)})

    def test_second_offset_also_respects_firmware_nominal_map(self) -> None:
        measured = geometry(24)
        squares = {"d4": "N", "e4": "p"}
        slots = [None] * 32
        first = nudge("p", "e4", "SE")
        second = nudge("N", "d4", "NE")
        plan_offset(first, squares, slots, measured)
        with self.assertRaisesRegex(NoRouteError, "clearance"):
            plan_offset(second, squares, slots, measured,
                        offset_positions_mm={first.square: offset_target_mm(first, measured)})

    def test_displaced_piece_changes_strict_route_choice(self) -> None:
        measured = geometry(12)
        source, destination, blocker = map(BoardSquare, ("d4", "f4", "d3"))
        transfer = Transfer("P", source, destination, "move")
        centered = plan_transfer_route(
            transfer, occupied_squares=(source, blocker),
            occupied_parking_slots=(), geometry=measured, max_waypoints=12,
        )
        displaced = plan_transfer_route(
            transfer, occupied_squares=(source, blocker),
            occupied_parking_slots=(), geometry=measured, max_waypoints=12,
            offset_positions_mm={blocker: offset_target_mm(nudge("p", "d3", "NE"), measured)},
        )
        self.assertEqual(centered.wire_points[1][1], 6)
        self.assertEqual(displaced.wire_points[1][1], 8)
        self.assertFalse(displaced.clearance_ignored)

    def test_displaced_piece_does_not_unblock_nominal_firmware_topology(self) -> None:
        measured = geometry()
        source, destination, blocker = map(BoardSquare, ("c1", "h6", "d2"))
        direct = plan_transfer_route(
            Transfer("B", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=measured, max_waypoints=12,
            offset_positions_mm={blocker: offset_target_mm(nudge("p", "d2", "SE"), measured)},
        )
        nominal_blocker = location_point(blocker)
        self.assertFalse(any(
            _point_on_segment(nominal_blocker, start, end)
            for start, end in zip(direct.points, direct.points[1:])
        ))

    def test_offset_state_must_match_action(self) -> None:
        measured = geometry()
        squares = {"d4": "N"}
        slots = [None] * 32
        with self.assertRaisesRegex(NoRouteError, "not displaced"):
            plan_offset(nudge("N", "d4", "NE", "IN"), squares, slots, measured)
        displaced = {BoardSquare("d4"): offset_target_mm(nudge("N", "d4", "NE"), measured)}
        with self.assertRaisesRegex(NoRouteError, "already displaced"):
            plan_offset(nudge("N", "d4", "NE"), squares, slots, measured,
                        offset_positions_mm=displaced)
        with self.assertRaisesRegex(NoRouteError, "does not match"):
            plan_offset(nudge("N", "d4", "NW", "IN"), squares, slots, measured,
                        offset_positions_mm=displaced)

    def test_displaced_carry_source_needs_matching_source_position(self) -> None:
        measured = geometry()
        source, destination = BoardSquare("h1"), BoardSquare("f1")
        departure = nudge("R", "h1", "NE")
        source_mm = offset_target_mm(departure, measured)
        transfer = Transfer("R", source, destination, "move")
        route = plan_transfer_route(
            transfer, occupied_squares=(source,), occupied_parking_slots=(),
            geometry=measured, max_waypoints=12,
            source_offset_mm=source_mm, offset_positions_mm={source: source_mm},
        )
        self.assertEqual(route.source_offset_mm, source_mm)
        with self.assertRaisesRegex(RouteError, "matching source_offset_mm"):
            plan_transfer_route(
                transfer, occupied_squares=(source,), occupied_parking_slots=(),
                geometry=measured, max_waypoints=12,
                offset_positions_mm={source: source_mm},
            )

    def test_castling_rook_api_remains_supported(self) -> None:
        action = RookOffset("R", BoardSquare("h1"), "NE", "OUT", "castle_rook_out")
        offset = plan_rook_offset(action, {"h1": "R"}, [None] * 32, geometry())
        self.assertEqual(offset.action, action)


if __name__ == "__main__":
    unittest.main()

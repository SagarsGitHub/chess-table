"""The host checks the magnet-on release extension before sending motion."""

import math
import unittest

from chess_table_host.corner_offset import offset_target_mm, plan_offset
from chess_table_host.planner import BoardSquare, ParkingSlot, PieceOffset, RookOffset, Transfer
from chess_table_host.routing import (
    NoRouteError, RouteGeometry, _distance_squared_to_segment,
    carry_release_target_mm, compensated_release_target_mm, location_point,
    plan_transfer_route,
)


GEOMETRY = RouteGeometry(
    31.25, 20.0, 20.0,
    x_travel_mm=260, y_travel_mm=260,
    end_inset_mm=2, pulses_per_mm=1600 / 12,
)


class ReleaseCompensationGeometryTests(unittest.TestCase):
    def test_extension_follows_final_loaded_direction(self) -> None:
        target = compensated_release_target_mm((100.0, 100.0), (110.0, 100.0), GEOMETRY)
        self.assertAlmostEqual(target[0], 116.25, delta=1 / GEOMETRY.pulses_per_mm)
        self.assertAlmostEqual(target[1], 100.0, delta=1 / GEOMETRY.pulses_per_mm)

        diagonal = compensated_release_target_mm((100.0, 100.0), (110.0, 110.0), GEOMETRY)
        self.assertAlmostEqual(
            math.dist(diagonal, (110.0, 110.0)), 0.20 * GEOMETRY.square_pitch_mm,
            delta=2 / GEOMETRY.pulses_per_mm,
        )
        self.assertGreater(diagonal[0], 110.0)
        self.assertGreater(diagonal[1], 110.0)

    def test_extension_rejects_measured_limit(self) -> None:
        with self.assertRaisesRegex(NoRouteError, "travel bounds"):
            compensated_release_target_mm((245.0, 100.0), (254.0, 100.0), GEOMETRY)

    def test_outer_rook_offset_uses_shorter_release_when_needed(self) -> None:
        measured = RouteGeometry(
            31.25, 18.0, 18.0,
            x_travel_mm=250, y_travel_mm=260,
            end_inset_mm=2, pulses_per_mm=1600 / 12,
        )
        out = RookOffset("R", BoardSquare("h1"), "NE", "OUT", "castle_rook_out")
        route = plan_offset(out, {"h1": "R"}, [None] * 32, measured)
        self.assertLessEqual(route.release_mm[0], measured.x_travel_mm - measured.end_inset_mm)
        self.assertAlmostEqual(math.dist(route.release_mm, route.end_mm),
                               0.18 * measured.square_pitch_mm,
                               delta=2 / measured.pulses_per_mm)

    def test_board_carry_has_release_and_parking_does_not(self) -> None:
        source, destination = BoardSquare("d4"), BoardSquare("e4")
        board_route = plan_transfer_route(
            Transfer("P", source, destination, "move"),
            occupied_squares=(source,), occupied_parking_slots=(),
            geometry=GEOMETRY, max_waypoints=12,
        )
        release = carry_release_target_mm(board_route, GEOMETRY)
        self.assertIsNotNone(release)
        center = GEOMETRY.physical_point_mm(location_point(destination))
        self.assertAlmostEqual(math.dist(release, center), 6.25,
                               delta=2 / GEOMETRY.pulses_per_mm)

        park_route = plan_transfer_route(
            Transfer("P", source, ParkingSlot(4), "park"),
            occupied_squares=(source,), occupied_parking_slots=(),
            geometry=GEOMETRY, max_waypoints=12,
        )
        self.assertIsNone(carry_release_target_mm(park_route, GEOMETRY))

    def test_final_extension_avoids_known_displaced_neighbor(self) -> None:
        source, destination, blocker = map(BoardSquare, ("c4", "e4", "f4"))
        out = PieceOffset("p", blocker, "NW", "OUT", "clearance_offset_out")
        obstacle = offset_target_mm(out, GEOMETRY)
        route = plan_transfer_route(
            Transfer("P", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=GEOMETRY, max_waypoints=12,
            offset_positions_mm={blocker: obstacle},
        )
        release = carry_release_target_mm(route, GEOMETRY)
        center = GEOMETRY.physical_point_mm(location_point(destination))
        self.assertGreaterEqual(
            math.sqrt(_distance_squared_to_segment(obstacle, center, release)),
            GEOMETRY.required_center_distance_mm - 1e-6,
        )

    def test_offset_release_extends_past_nominal_corner_or_center(self) -> None:
        squares = {"d4": "N"}
        slots = [None] * 32
        out = PieceOffset("N", BoardSquare("d4"), "NE", "OUT", "clearance_offset_out")
        going_out = plan_offset(out, squares, slots, GEOMETRY)
        self.assertGreater(going_out.release_mm[0], going_out.end_mm[0])
        self.assertGreater(going_out.release_mm[1], going_out.end_mm[1])
        back = PieceOffset("N", out.square, out.corner, "IN", "clearance_offset_in")
        going_in = plan_offset(
            back, squares, slots, GEOMETRY,
            offset_positions_mm={out.square: offset_target_mm(out, GEOMETRY)},
        )
        self.assertLess(going_in.release_mm[0], going_in.end_mm[0])
        self.assertLess(going_in.release_mm[1], going_in.end_mm[1])


if __name__ == "__main__":
    unittest.main()

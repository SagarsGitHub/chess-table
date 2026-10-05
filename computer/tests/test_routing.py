import unittest
from functools import partial

import chess

from chess_table_host import (
    BoardSquare,
    HalfSquarePoint,
    MAX_ROUTE_POINTS,
    NoRouteError,
    ParkingSlot,
    PromotionPolicy,
    ReachableParkingSlotSelector,
    RouteError,
    RouteGeometry,
    RouteTooLongError,
    Transfer,
    find_reachable_parking_slot,
    parking_point,
    plan_move,
    plan_transfer_route,
    ParkingInventory,
)


class RoutingTests(unittest.TestCase):
    def test_all_board_square_pairs_have_nominal_routes(self) -> None:
        for source_name in chess.SQUARE_NAMES:
            source = BoardSquare(source_name)
            for destination_name in chess.SQUARE_NAMES:
                if source_name == destination_name:
                    continue
                destination = BoardSquare(destination_name)
                route = plan_transfer_route(
                    Transfer("P", source, destination, "move"),
                    occupied_squares=(source,),
                    occupied_parking_slots=(),
                    max_waypoints=12,
                )
                self.assertEqual(route.points[0], self._point(source_name))
                self.assertEqual(route.points[-1], self._point(destination_name))
                self.assertLessEqual(len(route.points), MAX_ROUTE_POINTS)
                self.assertFalse(route.geometry_applied)
                self._assert_firmware_segments(route.points, {self._point(source_name)})

    def test_all_32_slots_support_parking_and_retrieval(self) -> None:
        for square_name in chess.SQUARE_NAMES:
            square = BoardSquare(square_name)
            for slot_id in range(32):
                slot = ParkingSlot(slot_id)
                expected = parking_point(slot)
                parked = plan_transfer_route(
                    Transfer("P", square, slot, "capture"),
                    occupied_squares=(square,),
                    occupied_parking_slots=(),
                    max_waypoints=12,
                )
                retrieved = plan_transfer_route(
                    Transfer("P", slot, square, "promotion_piece"),
                    occupied_squares=(),
                    occupied_parking_slots=(slot,),
                    max_waypoints=12,
                )
                self.assertEqual(parked.points[-1], expected)
                self.assertEqual(retrieved.points[0], expected)
                self.assertEqual(retrieved.points[-1], self._point(square_name))
                self._assert_firmware_segments(parked.points, {self._point(square_name)})
                self._assert_firmware_segments(retrieved.points, {expected})

        self.assertEqual(parking_point(ParkingSlot(0)), HalfSquarePoint(0, 0))
        self.assertEqual(parking_point(ParkingSlot(8)), HalfSquarePoint(16, 0))
        self.assertEqual(parking_point(ParkingSlot(9)), HalfSquarePoint(0, 16))
        self.assertEqual(parking_point(ParkingSlot(17)), HalfSquarePoint(16, 16))
        self.assertEqual(parking_point(ParkingSlot(18)), HalfSquarePoint(0, 2))
        self.assertEqual(parking_point(ParkingSlot(31)), HalfSquarePoint(16, 14))

    def test_full_starting_position_has_nominal_and_small_footprint_routes(self) -> None:
        board = chess.Board()
        squares = tuple(BoardSquare(chess.square_name(square)) for square in board.piece_map())
        transfer = Transfer("P", BoardSquare("e2"), BoardSquare("e4"), "move")

        nominal = plan_transfer_route(
            transfer, occupied_squares=squares, occupied_parking_slots=()
        )
        checked = plan_transfer_route(
            transfer,
            occupied_squares=squares,
            occupied_parking_slots=(),
            geometry=self._geometry(15, 15, 0.3),
        )

        self.assertEqual(nominal.points[0], HalfSquarePoint(9, 3))
        self.assertEqual(checked.points[-1], HalfSquarePoint(9, 7))
        self.assertTrue(checked.geometry_applied)
        # This threshold is deliberately too large for the starting array.
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                transfer,
                occupied_squares=squares,
                occupied_parking_slots=(),
                geometry=self._geometry(32, 32),
            )

    def test_occupied_parking_slot_forces_detour(self) -> None:
        route = plan_transfer_route(
            Transfer("P", BoardSquare("a1"), ParkingSlot(2), "capture"),
            occupied_squares=(BoardSquare("a1"),),
            occupied_parking_slots=(ParkingSlot(1),),
        )
        self.assertNotIn(HalfSquarePoint(2, 0), route.points)
        self.assertTrue(any(point.y > 0 for point in route.points[1:-1]))

    def test_reachable_slot_selector_skips_trapped_corner(self) -> None:
        slots = [None] * 32
        slots[1] = "p"
        slots[18] = "p"
        source = BoardSquare("e4")
        slot = find_reachable_parking_slot(
            source, "P", tuple(slots), occupied_squares=(source, BoardSquare("a1"))
        )
        self.assertEqual(slot, ParkingSlot(2))
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                Transfer("P", source, ParkingSlot(0), "capture"),
                occupied_squares=(source, BoardSquare("a1")),
                occupied_parking_slots=(ParkingSlot(1), ParkingSlot(18)),
            )

    def test_vacant_edge_square_unlocks_corner_between_occupied_slots(self) -> None:
        route = plan_transfer_route(
            Transfer("P", ParkingSlot(0), BoardSquare("e4"), "retrieve"),
            occupied_squares=(),
            occupied_parking_slots=(ParkingSlot(0), ParkingSlot(1), ParkingSlot(18)),
        )
        self.assertEqual(route.points[:3], (
            HalfSquarePoint(0, 0), HalfSquarePoint(1, 1), HalfSquarePoint(2, 2),
        ))
        self.assertNotIn(HalfSquarePoint(2, 0), route.points)
        self.assertNotIn(HalfSquarePoint(0, 2), route.points)
        self._assert_firmware_segments(route.points, {HalfSquarePoint(0, 0), HalfSquarePoint(2, 0), HalfSquarePoint(0, 2)})

    def test_clamped_perimeter_clearance_uses_executed_pulse_targets(self) -> None:
        geometry = self._geometry(15, 15, x_travel_mm=246, y_travel_mm=246)
        occupied = tuple(BoardSquare(f"{file}1") for file in "abcdefg")
        transfer = Transfer("P", ParkingSlot(0), BoardSquare("h1"), "retrieve")
        source_mm = geometry.physical_point_mm(HalfSquarePoint(0, 0))
        a1_mm = geometry.physical_point_mm(HalfSquarePoint(1, 1))
        self.assertGreater(source_mm[0], 2)
        self.assertLess(a1_mm[1] - source_mm[1], 15)

        nominal = plan_transfer_route(
            transfer, occupied_squares=occupied,
            occupied_parking_slots=(ParkingSlot(0),),
        )
        self.assertTrue(any(point.y == 0 for point in nominal.points[1:-1]))
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                transfer, occupied_squares=occupied,
                occupied_parking_slots=(ParkingSlot(0),), geometry=geometry,
            )

    def test_reachable_selector_integrates_with_capture(self) -> None:
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("d7d5")
        occupied = tuple(BoardSquare(chess.square_name(square)) for square in board.piece_map())
        slots = [None] * 32
        slots[1] = "n"
        slots[18] = "n"
        selector = partial(find_reachable_parking_slot, occupied_squares=occupied)

        plan = plan_move(board, "e4d5", ParkingInventory(tuple(slots)), slot_selector=selector)

        self.assertEqual(plan.actions[0].destination, ParkingSlot(2))

    def test_one_plan_selector_advances_between_capture_and_pawn_parking(self) -> None:
        board = chess.Board("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        occupied = tuple(BoardSquare(chess.square_name(square)) for square in board.piece_map())
        slots = [None] * 32
        slots[4] = "Q"
        selector = ReachableParkingSlotSelector(occupied, max_waypoints=12)

        plan = plan_move(
            board, "a7b8q", ParkingInventory(tuple(slots)),
            promotion_policy=PromotionPolicy.PARKED_PIECE,
            slot_selector=selector,
        )

        self.assertEqual(plan.actions[0].source, BoardSquare("b8"))
        self.assertEqual(plan.actions[1].source, BoardSquare("a7"))
        self.assertNotIn(BoardSquare("b8"), selector.current_squares)
        self.assertNotIn(BoardSquare("a7"), selector.current_squares)
        self.assertNotEqual(plan.actions[0].destination, plan.actions[1].destination)

    def test_invalid_snapshot_and_geometry_are_rejected(self) -> None:
        source = BoardSquare("a1")
        destination = BoardSquare("b2")
        transfer = Transfer("P", source, destination, "move")
        with self.assertRaisesRegex(RouteError, "source must be occupied"):
            plan_transfer_route(transfer, occupied_squares=(), occupied_parking_slots=())
        with self.assertRaisesRegex(RouteError, "destination must be vacant"):
            plan_transfer_route(
                transfer, occupied_squares=(source, destination), occupied_parking_slots=()
            )
        with self.assertRaises(RouteError):
            plan_transfer_route(
                Transfer("P", source, source, "move"),
                occupied_squares=(source,), occupied_parking_slots=()
            )
        with self.assertRaises(RouteError):
            plan_transfer_route(
                Transfer("P", ParkingSlot(1), ParkingSlot(2), "move"),
                occupied_squares=(), occupied_parking_slots=(ParkingSlot(1),)
            )
        for geometry in (self._geometry(15, 15), self._geometry(15, 15, 0.2)):
            self.assertGreater(geometry.required_center_distance_mm, 0)
        with self.assertRaises(TypeError):
            RouteGeometry(31.25, 15, 15)
        with self.assertRaises(ValueError):
            self._geometry(15, 15, square_pitch_mm=0)
        with self.assertRaises(ValueError):
            self._geometry(float("nan"), 15)
        with self.assertRaises(ValueError):
            self._geometry(15, 15, -1)
        with self.assertRaises(ValueError):
            self._geometry(15, 15, x_travel_mm=200)
        with self.assertRaises(ValueError):
            HalfSquarePoint(17, 0)
        with self.assertRaises(ValueError):
            plan_transfer_route(transfer, occupied_squares=(source,),
                                occupied_parking_slots=(), max_waypoints=1)

    def test_waypoint_limit_reports_error_without_truncating(self) -> None:
        transfer = Transfer("P", BoardSquare("e4"), ParkingSlot(0), "capture")
        route = plan_transfer_route(
            transfer, occupied_squares=(BoardSquare("e4"),),
            occupied_parking_slots=()
        )
        self.assertGreater(len(route.points), 2)
        with self.assertRaises(RouteTooLongError) as caught:
            plan_transfer_route(
                transfer, occupied_squares=(BoardSquare("e4"),),
                occupied_parking_slots=(), max_waypoints=2
            )
        self.assertEqual(caught.exception.required_waypoints, len(route.points))
        self.assertEqual(caught.exception.max_waypoints, 2)

    def test_waypoint_budget_finds_longer_route_with_fewer_turns(self) -> None:
        occupied_squares = {BoardSquare(name) for name in (
            "c1", "d1", "e6", "f8", "g6", "g7", "h4",
        )}
        occupied_slots = {ParkingSlot(slot) for slot in (15, 18, 21, 27, 28)}
        transfer = Transfer("P", BoardSquare("d1"), ParkingSlot(30), "park")
        geometry = self._geometry(18, 18)

        shortest_steps = plan_transfer_route(
            transfer, occupied_squares=occupied_squares,
            occupied_parking_slots=occupied_slots, geometry=geometry,
        )
        within_budget = plan_transfer_route(
            transfer, occupied_squares=occupied_squares,
            occupied_parking_slots=occupied_slots, geometry=geometry,
            max_waypoints=7,
        )
        self.assertEqual(len(shortest_steps.points), 8)
        self.assertEqual(len(within_budget.points), 7)
        self._assert_firmware_segments(
            within_budget.points,
            {self._point(square.name) for square in occupied_squares} |
            {parking_point(slot) for slot in occupied_slots},
        )
        with self.assertRaises(RouteTooLongError) as caught:
            plan_transfer_route(
                transfer, occupied_squares=occupied_squares,
                occupied_parking_slots=occupied_slots, geometry=geometry,
                max_waypoints=6,
            )
        self.assertEqual(caught.exception.required_waypoints, 7)

    @staticmethod
    def _point(name: str) -> HalfSquarePoint:
        return HalfSquarePoint(2 * (ord(name[0]) - ord("a")) + 1,
                               2 * (int(name[1]) - 1) + 1)

    @staticmethod
    def _geometry(moving: float, occupied: float, margin: float = 0,
                  *, x_travel_mm: float = 260, y_travel_mm: float = 260,
                  square_pitch_mm: float = 31.25) -> RouteGeometry:
        return RouteGeometry(
            square_pitch_mm, moving, occupied, margin,
            x_travel_mm=x_travel_mm, y_travel_mm=y_travel_mm,
            end_inset_mm=2, pulses_per_mm=1600 / 12,
        )

    def _assert_firmware_segments(
        self, points: tuple[HalfSquarePoint, ...], occupied: set[HalfSquarePoint]
    ) -> None:
        for point in points[1:-1]:
            if point.x & 1:
                self.assertTrue(point.y & 1)
                self.assertNotIn(point, occupied)
        for start, end in zip(points, points[1:]):
            dx, dy = end.x - start.x, end.y - start.y
            start_square = bool(start.x & 1)
            end_square = bool(end.x & 1)
            self.assertEqual(start_square, bool(start.y & 1))
            self.assertEqual(end_square, bool(end.y & 1))
            if start_square != end_square:
                self.assertEqual((abs(dx), abs(dy)), (1, 1), (start, end))
            else:
                self.assertFalse(start_square)
                self.assertTrue(
                    (dx == 0 and dy != 0 and dy % 2 == 0) or
                    (dy == 0 and dx != 0 and dx % 2 == 0),
                    (start, end),
                )


if __name__ == "__main__":
    unittest.main()

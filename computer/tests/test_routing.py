import unittest
from functools import partial
from math import hypot

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
    encode_carry,
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
        self.assertFalse(checked.clearance_ignored)
        # This threshold is deliberately too large for the starting array.
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                transfer,
                occupied_squares=squares,
                occupied_parking_slots=(),
                geometry=self._geometry(32, 32),
            )

    def test_open_diagonals_take_straight_short_routes(self) -> None:
        for source_name, destination_name in (("c1", "h6"), ("e2", "g4")):
            source, destination = BoardSquare(source_name), BoardSquare(destination_name)
            route = plan_transfer_route(
                Transfer("B", source, destination, "move"),
                occupied_squares=(source,), occupied_parking_slots=(),
                geometry=self._geometry(18, 18), max_waypoints=12,
            )
            start, end = route.points[0], route.points[-1]
            self.assertEqual(len(route.points), 2 if source_name == "e2" else 4)
            self.assertTrue(all(
                (point.x - start.x) * (end.y - start.y) ==
                (point.y - start.y) * (end.x - start.x)
                for point in route.points
            ))
            self.assertAlmostEqual(
                self._route_distance(route.points),
                hypot(end.x - start.x, end.y - start.y),
            )
            self.assertLessEqual(len(route.points), 12)
            self.assertEqual(len(encode_carry(route)[2]), 2 * len(route.points))
            self._assert_firmware_segments(route.points, {start})

    def test_one_and_two_square_moves_go_direct_in_all_eight_directions(self) -> None:
        source = BoardSquare("d4")
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                       (1, 1), (1, -1), (-1, 1), (-1, -1)):
            for distance in (1, 2):
                destination = BoardSquare(
                    f"{chr(ord('d') + dx * distance)}{4 + dy * distance}"
                )
                with self.subTest(destination=destination):
                    route = plan_transfer_route(
                        Transfer("Q", source, destination, "move"),
                        occupied_squares=(source,), occupied_parking_slots=(),
                        geometry=self._geometry(18, 18), max_waypoints=2,
                    )
                    self.assertEqual(route.points, (self._point("d4"), self._point(destination.name)))
                    self.assertTrue(route.geometry_applied)
                    self._assert_firmware_segments(route.points, {route.points[0]})

    def test_starting_double_pawn_move_uses_direct_path(self) -> None:
        squares = tuple(BoardSquare(chess.square_name(square))
                        for square in chess.Board().piece_map())
        route = plan_transfer_route(
            Transfer("P", BoardSquare("e2"), BoardSquare("e4"), "move"),
            occupied_squares=squares, occupied_parking_slots=(),
            geometry=self._geometry(18, 18), max_waypoints=2,
        )
        self.assertEqual(route.wire_points, ((9, 3), (9, 7)))
        self.assertEqual(encode_carry(route), ("12", "28", "9397"))

    def test_direct_paths_can_be_disabled_and_exclude_long_and_knight_moves(self) -> None:
        source = BoardSquare("d4")
        for name, enabled in (("f4", False), ("g4", True), ("f5", True)):
            with self.subTest(destination=name, enabled=enabled):
                route = plan_transfer_route(
                    Transfer("Q", source, BoardSquare(name), "move"),
                    occupied_squares=(source,), occupied_parking_slots=(),
                    geometry=self._geometry(18, 18), allow_direct_moves=enabled,
                )
                self.assertGreater(len(route.points), 2)

    def test_blocked_two_square_direct_path_falls_back_to_grid(self) -> None:
        source, destination, blocker = map(BoardSquare, ("d4", "f4", "e4"))
        route = plan_transfer_route(
            Transfer("Q", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=self._geometry(18, 18), max_waypoints=12,
        )
        self.assertGreater(len(route.points), 2)
        self._assert_firmware_segments(route.points, {self._point("d4"), self._point("e4")})

    def test_direct_path_retains_firmware_clearance_with_small_piece_override(self) -> None:
        # At 24 mm pitch, a diagonal passes 24/sqrt(2) mm from e4. That
        # clears the configured 12 mm pieces, but not firmware's 18 mm.
        source, destination, blocker = map(BoardSquare, ("d4", "f6", "e4"))
        route = plan_transfer_route(
            Transfer("B", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=self._geometry(12, 12, square_pitch_mm=24),
            ignore_piece_clearance=True, max_waypoints=12,
        )
        self.assertGreater(len(route.points), 2)

    def test_direct_path_checks_displaced_neighbors_and_release_extension(self) -> None:
        source, destination, blocker = map(BoardSquare, ("d4", "f4", "e3"))
        geometry = self._geometry(24, 24)
        center = geometry.physical_point_mm(self._point("e3"))
        route = plan_transfer_route(
            Transfer("R", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=geometry, offset_positions_mm={blocker: (center[0], center[1] + 9.375)},
        )
        self.assertGreater(len(route.points), 2)
        # g4 clears the center path by a full pitch, but 26 mm footprints
        # cannot clear the extra 6.25 mm loaded stroke beyond f4.
        blocker = BoardSquare("g4")
        route = plan_transfer_route(
            Transfer("R", source, destination, "move"),
            occupied_squares=(source, blocker), occupied_parking_slots=(),
            geometry=self._geometry(26, 26),
        )
        self.assertGreater(len(route.points), 2)

    def test_occupied_center_blocks_direct_diagonal_even_with_clearance_override(self) -> None:
        source, destination, blocker = (BoardSquare(name) for name in ("c1", "h6", "d2"))
        transfer = Transfer("B", source, destination, "move")
        direct = hypot(10, 10)
        for ignore_clearance in (False, True):
            route = plan_transfer_route(
                transfer, occupied_squares=(source, blocker), occupied_parking_slots=(),
                geometry=self._geometry(18, 18),
                ignore_piece_clearance=ignore_clearance, max_waypoints=12,
            )
            self.assertGreater(self._route_distance(route.points), direct)
            self._assert_firmware_segments(route.points, {self._point("c1"), self._point("d2")})

    def test_explicit_clearance_override_routes_crowded_move(self) -> None:
        squares = tuple(BoardSquare(chess.square_name(square)) for square in chess.Board().piece_map())
        transfer = Transfer("P", BoardSquare("e2"), BoardSquare("e4"), "move")
        geometry = self._geometry(32, 32)
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                transfer, occupied_squares=squares, occupied_parking_slots=(),
                geometry=geometry,
            )

        route = plan_transfer_route(
            transfer, occupied_squares=squares, occupied_parking_slots=(),
            geometry=geometry, ignore_piece_clearance=True, max_waypoints=12,
        )
        self.assertEqual(route.points[0], self._point("e2"))
        self.assertEqual(route.points[-1], self._point("e4"))
        self.assertTrue(route.geometry_applied)
        self.assertTrue(route.clearance_ignored)
        self._assert_firmware_segments(route.points, {self._point(square.name) for square in squares})

    def test_long_crowded_horizontal_corridor_chooses_clear_detour(self) -> None:
        # The tempting y=6 lane has standing pieces on both sides for six
        # squares. Its y=8 neighbor still runs beside the row-four pieces.
        source, destination = BoardSquare("a4"), BoardSquare("h4")
        standing = tuple(BoardSquare(f"{file}{rank}") for rank in (3, 4) for file in "bcdefg")
        route = plan_transfer_route(
            Transfer("P", source, destination, "move"),
            occupied_squares=(source, *standing), occupied_parking_slots=(),
            geometry=self._geometry(18, 18), ignore_piece_clearance=True,
            max_waypoints=12,
        )

        self.assertEqual(route.points, (
            HalfSquarePoint(1, 7), HalfSquarePoint(2, 8),
            HalfSquarePoint(4, 10), HalfSquarePoint(12, 10),
            HalfSquarePoint(14, 8), HalfSquarePoint(15, 7),
        ))
        self.assertGreater(
            self._route_distance(route.points),
            self._route_distance((HalfSquarePoint(1, 7), HalfSquarePoint(2, 6),
                                  HalfSquarePoint(14, 6), HalfSquarePoint(15, 7))),
        )
        self._assert_firmware_segments(route.points, {self._point(square.name) for square in (source, *standing)})

    def test_logged_h3_to_c1_carry_avoids_two_sided_horizontal_run(self) -> None:
        # A physical game sent (12,2)->(6,2) through the d1/d2 and f1/f2
        # pairs. Replan that occupancy with its measured geometry and override.
        occupied = tuple(BoardSquare(name) for name in (
            "a1 b1 d1 e1 f1 g1 h1 a2 b2 c2 d2 f2 h3 d4 e4 f5 "
            "d6 g6 a7 b7 c7 d7 e7 h7 a8 b8 c8 d8 e8 f8 g8 h8"
        ).split())
        geometry = RouteGeometry(
            31.25, 18, 18, 0,
            x_travel_mm=249.735, y_travel_mm=251.985,
            end_inset_mm=2.0, pulses_per_mm=133.3333,
            board_origin_x_mm=-0.1325, board_origin_y_mm=0.9925,
        )
        route = plan_transfer_route(
            Transfer("N", BoardSquare("h3"), BoardSquare("c1"), "move"),
            occupied_squares=occupied, occupied_parking_slots=(),
            geometry=geometry, ignore_piece_clearance=True, max_waypoints=12,
        )

        self.assertEqual(route.wire_points, (
            (15, 5), (14, 6), (8, 6), (6, 4), (6, 2), (5, 1),
        ))
        self._assert_firmware_segments(route.points, {self._point(square.name) for square in occupied})

    def test_unavoidable_horizontal_corridor_remains_routable(self) -> None:
        # A packed board leaves only the bottom perimeter for this carry.
        # Crowding is a preference, never a new geometric prohibition.
        source, destination = BoardSquare("a1"), BoardSquare("h1")
        occupied = tuple(BoardSquare(name) for name in chess.SQUARE_NAMES if name != "h1")
        route = plan_transfer_route(
            Transfer("P", source, destination, "move"),
            occupied_squares=occupied, occupied_parking_slots=(),
            geometry=self._geometry(18, 18), ignore_piece_clearance=True,
            max_waypoints=12,
        )

        self.assertEqual(route.points, (
            HalfSquarePoint(1, 1), HalfSquarePoint(2, 0),
            HalfSquarePoint(14, 0), HalfSquarePoint(15, 1),
        ))
        self._assert_firmware_segments(route.points, {self._point(square.name) for square in occupied})

    def test_clearance_override_keeps_center_bounds_and_waypoint_checks(self) -> None:
        source = BoardSquare("e4")
        with self.assertRaises(NoRouteError):
            plan_transfer_route(
                Transfer("P", source, ParkingSlot(0), "park"),
                occupied_squares=(source, BoardSquare("a1")),
                occupied_parking_slots=(ParkingSlot(1), ParkingSlot(18)),
                geometry=self._geometry(32, 32), ignore_piece_clearance=True,
            )

        transfer = Transfer("P", source, ParkingSlot(0), "park")
        with self.assertRaises(RouteTooLongError):
            plan_transfer_route(
                transfer, occupied_squares=(source,), occupied_parking_slots=(),
                geometry=self._geometry(32, 32), ignore_piece_clearance=True,
                max_waypoints=2,
            )

        shifted_geometry = RouteGeometry(
            31.25, 18, 18, x_travel_mm=260, y_travel_mm=260,
            end_inset_mm=2, pulses_per_mm=1600 / 12,
            board_origin_x_mm=100,
        )
        with self.assertRaisesRegex(NoRouteError, "outside measured bounds"):
            plan_transfer_route(
                Transfer("P", BoardSquare("h1"), BoardSquare("g1"), "move"),
                occupied_squares=(BoardSquare("h1"),), occupied_parking_slots=(),
                geometry=shifted_geometry, ignore_piece_clearance=True,
            )

    def test_parking_selector_propagates_clearance_override(self) -> None:
        squares = tuple(BoardSquare(chess.square_name(square)) for square in chess.Board().piece_map())
        geometry = self._geometry(32, 32)
        with self.assertRaises(NoRouteError):
            find_reachable_parking_slot(
                BoardSquare("e2"), "P", (None,) * 32,
                occupied_squares=squares, geometry=geometry, max_waypoints=12,
            )

        selector = ReachableParkingSlotSelector(
            squares, geometry=geometry, max_waypoints=12,
            ignore_piece_clearance=True,
        )
        slot = selector(BoardSquare("e2"), "P", (None,) * 32)
        self.assertIsInstance(slot, ParkingSlot)
        self.assertNotIn(BoardSquare("e2"), selector.current_squares)

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

    def test_diagonal_grid_link_unlocks_corner_between_occupied_slots(self) -> None:
        route = plan_transfer_route(
            Transfer("P", ParkingSlot(0), BoardSquare("e4"), "retrieve"),
            occupied_squares=(),
            occupied_parking_slots=(ParkingSlot(0), ParkingSlot(1), ParkingSlot(18)),
        )
        self.assertEqual(route.points[:2], (
            HalfSquarePoint(0, 0), HalfSquarePoint(2, 2),
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
        self.assertEqual(nominal.points[0], HalfSquarePoint(0, 0))
        self.assertEqual(nominal.points[-1], self._point("h1"))
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
        with self.assertRaises(TypeError):
            plan_transfer_route(transfer, occupied_squares=(source,),
                                occupied_parking_slots=(), ignore_piece_clearance=1)
        with self.assertRaises(TypeError):
            plan_transfer_route(transfer, occupied_squares=(source,),
                                occupied_parking_slots=(), allow_direct_moves=1)

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
        self.assertEqual(caught.exception.required_waypoints, 3)
        self.assertEqual(caught.exception.max_waypoints, 2)

    def test_waypoint_budget_finds_longer_route_with_fewer_waypoints(self) -> None:
        source = BoardSquare("e4")
        transfer = Transfer("P", source, ParkingSlot(0), "park")
        geometry = self._geometry(18, 18)

        shortest_distance = plan_transfer_route(
            transfer, occupied_squares=(source,),
            occupied_parking_slots=(), geometry=geometry,
        )
        within_budget = plan_transfer_route(
            transfer, occupied_squares=(source,),
            occupied_parking_slots=(), geometry=geometry,
            max_waypoints=3,
        )
        self.assertEqual(len(shortest_distance.points), 4)
        self.assertEqual(len(within_budget.points), 3)
        self.assertGreater(
            self._route_distance(within_budget.points),
            self._route_distance(shortest_distance.points),
        )
        self._assert_firmware_segments(
            within_budget.points, {self._point("e4")},
        )
        with self.assertRaises(RouteTooLongError) as caught:
            plan_transfer_route(
                transfer, occupied_squares=(source,),
                occupied_parking_slots=(), geometry=geometry,
                max_waypoints=2,
            )
        self.assertEqual(caught.exception.required_waypoints, 3)

    @staticmethod
    def _point(name: str) -> HalfSquarePoint:
        return HalfSquarePoint(2 * (ord(name[0]) - ord("a")) + 1,
                               2 * (int(name[1]) - 1) + 1)

    @staticmethod
    def _route_distance(points: tuple[HalfSquarePoint, ...]) -> float:
        return sum(hypot(b.x - a.x, b.y - a.y) for a, b in zip(points, points[1:]))

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
                if start_square:
                    self.assertEqual(len(points), 2)
                    self.assertLessEqual(max(abs(dx), abs(dy)), 4)
                self.assertTrue(
                    (dx == 0 and dy != 0 and dy % 2 == 0) or
                    (dy == 0 and dx != 0 and dx % 2 == 0) or
                    (abs(dx) == abs(dy) and dx != 0 and dx % 2 == 0),
                    (start, end),
                )
            for center in occupied - {points[0]}:
                cx, cy = center.x - start.x, center.y - start.y
                self.assertFalse(
                    cx * dy == cy * dx and 0 <= cx * dx + cy * dy <= dx * dx + dy * dy,
                    (start, end, center),
                )


if __name__ == "__main__":
    unittest.main()

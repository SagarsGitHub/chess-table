"""Plan a carried piece's half-square path around occupied locations.

This is a geometric *candidate* route, not a guarantee of physical clearance.
Physical clearance needs measured diameters and pitch; nominal routes without
these measurements are explicitly marked as unchecked. Every returned point
uses the firmware's integer
``HalfSquarePoint`` coordinate system: a1=(1, 1), h8=(15, 15), and parking
slots lie on the even-coordinate perimeter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import KW_ONLY, dataclass
from functools import lru_cache
from heapq import heappop, heappush
from math import ceil, floor, isfinite
from struct import pack, unpack
from typing import Iterable

from .planner import (
    BoardSquare,
    NoEmptyParkingSlotError,
    ParkingInventory,
    ParkingSlot,
    Transfer,
)


# There are 9 * 9 grid intersections, one board endpoint, and up to two vacant
# edge squares adjacent to the one parking endpoint. A simple path cannot
# visit one twice. Square-to-square routes have the smaller bound of 83.
MAX_ROUTE_POINTS = 84


class RouteError(ValueError):
    """The transfer or occupied-location snapshot cannot be routed."""


class NoRouteError(RouteError):
    """No route meets the supplied center-to-center clearance."""


class RouteTooLongError(RouteError):
    """The planned route exceeds the requested waypoint capacity."""

    def __init__(self, required_waypoints: int, max_waypoints: int) -> None:
        self.required_waypoints = required_waypoints
        self.max_waypoints = max_waypoints
        super().__init__(
            f"Planned route needs {required_waypoints} waypoints; capacity is {max_waypoints}"
        )


@dataclass(frozen=True, order=True)
class HalfSquarePoint:
    x: int
    y: int

    def __post_init__(self) -> None:
        if type(self.x) is not int or type(self.y) is not int or not (0 <= self.x <= 16 and 0 <= self.y <= 16):
            raise ValueError("Half-square coordinates must be integers from 0 to 16")


@dataclass(frozen=True)
class RouteGeometry:
    """Measured circular-footprint approximation for a carried piece.

    Supply the controller's measured travel spans, end inset, and pulse scale.
    Optional origins override the centered origin calculated by firmware on
    a fresh calibration; pass the saved origins if EEPROM uses other values.
    Each coordinate is mapped with firmware float32 arithmetic, the inward
    perimeter clamp, and final pulse rounding before clearance is checked.
    The circular-footprint model still cannot account for wobble, off-center
    magnets, or non-circular pieces.
    """

    square_pitch_mm: float
    moving_diameter_mm: float
    occupied_diameter_mm: float
    margin_mm: float = 0.0
    _: KW_ONLY
    x_travel_mm: float
    y_travel_mm: float
    end_inset_mm: float
    pulses_per_mm: float
    board_origin_x_mm: float | None = None
    board_origin_y_mm: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "square_pitch_mm", "moving_diameter_mm", "occupied_diameter_mm",
            "margin_mm", "x_travel_mm", "y_travel_mm", "end_inset_mm",
            "pulses_per_mm", "board_origin_x_mm", "board_origin_y_mm",
        ):
            value = getattr(self, name)
            if value is None and name.startswith("board_origin_"):
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if any(getattr(self, name) <= 0 for name in (
            "square_pitch_mm", "moving_diameter_mm", "occupied_diameter_mm",
            "x_travel_mm", "y_travel_mm", "end_inset_mm", "pulses_per_mm",
        )):
            raise ValueError("Pitch, diameters, spans, inset and pulse scale must be positive")
        if self.margin_mm < 0:
            raise ValueError("Clearance margin cannot be negative")
        if min(self.x_travel_mm, self.y_travel_mm) - 2 * self.end_inset_mm < 7 * self.square_pitch_mm:
            raise ValueError("Measured travel cannot fit all board square centers at this pitch")

    @property
    def required_center_distance_mm(self) -> float:
        return (self.moving_diameter_mm + self.occupied_diameter_mm) / 2 + self.margin_mm

    def physical_point_mm(self, point: HalfSquarePoint) -> tuple[float, float]:
        """Return the commanded pulse target as millimeters from MIN/MIN."""

        return (
            self._axis_point_mm(point.x, self.x_travel_mm, self.board_origin_x_mm),
            self._axis_point_mm(point.y, self.y_travel_mm, self.board_origin_y_mm),
        )

    def _axis_point_mm(self, half: int, span: float, saved_origin: float | None) -> float:
        pitch = _f32(self.square_pitch_mm)
        travel = _f32(span)
        inset = _f32(self.end_inset_mm)
        ppm = _f32(self.pulses_per_mm)
        borders = _f32(_f32(2.0) * inset)
        usable = _f32(travel - borders)
        origin = _f32(saved_origin) if saved_origin is not None else _f32(
            _f32(travel - _f32(_f32(8.0) * pitch)) / _f32(2.0)
        )
        value = _f32(origin + _f32(_f32(float(half)) * _f32(pitch / _f32(2.0))))
        if half == 0:
            value = max(value, _f32(ceil(_f32(inset * ppm)) / ppm))
        if half == 16:
            firmware_span = _f32(usable + borders)
            upper = floor(_f32(_f32(firmware_span - inset) * ppm))
            value = min(value, _f32(upper / ppm))
        pulses = floor(_f32(value * ppm) + 0.5)  # lroundf for nonnegative targets
        low = ceil(_f32(inset * ppm))
        high = floor(_f32(_f32(travel - inset) * ppm))
        if pulses < low or pulses > high:
            raise RouteError("Waypoint is outside the controller's measured pulse bounds")
        return pulses / ppm


def _f32(value: float) -> float:
    """Round a host intermediate to the AVR firmware's 32-bit float."""

    return unpack("<f", pack("<f", value))[0]


@dataclass(frozen=True)
class CarryRoute:
    """A bounded route inclusive of its pickup and delivery points."""

    transfer: Transfer
    points: tuple[HalfSquarePoint, ...]
    geometry_applied: bool = False

    def __post_init__(self) -> None:
        if not 2 <= len(self.points) <= MAX_ROUTE_POINTS:
            raise ValueError(f"A carry route needs 2..{MAX_ROUTE_POINTS} points")

    @property
    def wire_points(self) -> tuple[tuple[int, int], ...]:
        """Coordinate pairs suitable for serialization as two bytes each."""

        return tuple((point.x, point.y) for point in self.points)


def parking_point(slot: ParkingSlot) -> HalfSquarePoint:
    """Match ``firmware/include/KnightRoute.h::parkingPoint`` exactly."""

    slot_id = slot.slot_id
    if slot_id < 9:
        return HalfSquarePoint(2 * slot_id, 0)
    if slot_id < 18:
        return HalfSquarePoint(2 * (slot_id - 9), 16)
    if slot_id < 25:
        return HalfSquarePoint(0, 2 * (slot_id - 17))
    return HalfSquarePoint(16, 2 * (slot_id - 24))


def location_point(location: BoardSquare | ParkingSlot) -> HalfSquarePoint:
    if isinstance(location, BoardSquare):
        return HalfSquarePoint(2 * (ord(location.name[0]) - ord("a")) + 1,
                               2 * (int(location.name[1]) - 1) + 1)
    if isinstance(location, ParkingSlot):
        return parking_point(location)
    raise RouteError(f"Unsupported location: {location!r}")


def _distance_squared_to_segment(
    center: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    vx = b[0] - a[0]
    vy = b[1] - a[1]
    denominator = vx * vx + vy * vy
    if denominator == 0:
        return (center[0] - a[0]) ** 2 + (center[1] - a[1]) ** 2
    t = ((center[0] - a[0]) * vx + (center[1] - a[1]) * vy) / denominator
    t = max(0.0, min(1.0, t))
    dx = center[0] - (a[0] + t * vx)
    dy = center[1] - (a[1] + t * vy)
    return dx * dx + dy * dy


def _adjacent_intersections(square: HalfSquarePoint) -> tuple[HalfSquarePoint, ...]:
    return tuple(
        HalfSquarePoint(square.x + dx, square.y + dy)
        for dx in (-1, 1)
        for dy in (-1, 1)
    )


def _adjacent_edge_centers(park: HalfSquarePoint) -> tuple[HalfSquarePoint, ...]:
    return tuple(
        HalfSquarePoint(park.x + dx, park.y + dy)
        for dx in (-1, 1)
        for dy in (-1, 1)
        if 1 <= park.x + dx <= 15 and 1 <= park.y + dy <= 15
    )


def _compress_collinear(points: list[HalfSquarePoint]) -> tuple[HalfSquarePoint, ...]:
    """Keep diagonal link endpoints; compress only straight grid runs."""

    compressed: list[HalfSquarePoint] = []
    for point in points:
        while len(compressed) >= 2:
            a, b = compressed[-2:]
            abx, aby = b.x - a.x, b.y - a.y
            bcx, bcy = point.x - b.x, point.y - b.y
            if (any((p.x & 1) or (p.y & 1) for p in (a, b, point)) or
                abx * bcy != aby * bcx or abx * bcx + aby * bcy <= 0):
                break
            compressed.pop()
        compressed.append(point)
    return tuple(compressed)


def plan_transfer_route(
    transfer: Transfer,
    *,
    occupied_squares: Iterable[BoardSquare],
    occupied_parking_slots: Iterable[ParkingSlot],
    geometry: RouteGeometry | None = None,
    max_waypoints: int | None = None,
) -> CarryRoute:
    """Route a single transfer against the occupancy *before* that transfer.

    The source must be occupied and the destination vacant. The caller must
    update its occupancy snapshot after each acknowledged transfer, especially
    between the victim and attacker legs of a capture. Paths use short
    diagonal entries from square centers to adjacent grid intersections, then
    horizontal/vertical grid edges. A vacant edge square adjacent to the
    parking endpoint may be used as an additional approach, so an occupied
    neighbor slot need not trap a corner slot. Occupied perimeter slots are
    obstacles.
    Without ``geometry``, only direct center crossings are excluded. Such a
    route is a nominal path and has no piece-clearance check.
    """

    if not isinstance(transfer, Transfer):
        raise TypeError("transfer must be a Transfer")
    if geometry is not None and not isinstance(geometry, RouteGeometry):
        raise TypeError("geometry must be a RouteGeometry or None")
    if max_waypoints is not None and (type(max_waypoints) is not int or not 2 <= max_waypoints <= MAX_ROUTE_POINTS):
        raise ValueError(f"max_waypoints must be 2..{MAX_ROUTE_POINTS}")
    source, destination = transfer.source, transfer.destination
    if not isinstance(source, (BoardSquare, ParkingSlot)) or not isinstance(destination, (BoardSquare, ParkingSlot)):
        raise RouteError("A transfer needs board or parking locations")
    if isinstance(source, ParkingSlot) and isinstance(destination, ParkingSlot):
        raise RouteError("Parking-to-parking transfers are unsupported")
    if source == destination:
        raise RouteError("Source and destination must differ")

    squares = frozenset(occupied_squares)
    slots = frozenset(occupied_parking_slots)
    if any(not isinstance(square, BoardSquare) for square in squares):
        raise RouteError("occupied_squares must contain BoardSquare values")
    if any(not isinstance(slot, ParkingSlot) for slot in slots):
        raise RouteError("occupied_parking_slots must contain ParkingSlot values")
    occupied = squares if isinstance(source, BoardSquare) else slots
    if source not in occupied:
        raise RouteError("Transfer source must be occupied in the supplied snapshot")
    occupied = squares if isinstance(destination, BoardSquare) else slots
    if destination in occupied:
        raise RouteError("Transfer destination must be vacant in the supplied snapshot")

    source_point = location_point(source)
    destination_point = location_point(destination)
    park_point = source_point if isinstance(source, ParkingSlot) else (
        destination_point if isinstance(destination, ParkingSlot) else None
    )
    occupied_square_points = frozenset(location_point(square) for square in squares)
    edge_centers = frozenset(
        point for point in _adjacent_edge_centers(park_point)
        if point not in occupied_square_points
    ) if park_point is not None else frozenset()
    obstacles = tuple(location_point(location) for location in ((*squares, *slots)) if location != source)

    @lru_cache(maxsize=None)
    def metric(point: HalfSquarePoint) -> tuple[float, float]:
        return geometry.physical_point_mm(point) if geometry else (float(point.x), float(point.y))

    try:
        metric(source_point)
        metric(destination_point)
        obstacle_positions = tuple(metric(point) for point in obstacles)
    except RouteError as exc:
        raise NoRouteError("Source, destination or occupied piece lies outside measured bounds") from exc
    required_squared = geometry.required_center_distance_mm ** 2 if geometry else None

    def clear(a: HalfSquarePoint, b: HalfSquarePoint) -> bool:
        try:
            start, end = metric(a), metric(b)
        except RouteError:
            return False
        if required_squared is None:
            return all(
                _distance_squared_to_segment(center, start, end) > 1e-12
                for center in obstacle_positions
            )
        return all(
            _distance_squared_to_segment(center, start, end) >= required_squared - 1e-9
            for center in obstacle_positions
        )

    # Board centers attach to four diagonal grid points; parking is itself a
    # grid point. Grid moves are orthogonal and one square pitch long.
    def neighbors(here: HalfSquarePoint) -> tuple[HalfSquarePoint, ...]:
        if here.x % 2 and here.y % 2:
            next_points = _adjacent_intersections(here)
            if isinstance(destination, ParkingSlot) and here in edge_centers:
                next_points += (destination_point,)
        else:
            next_points = tuple(
                HalfSquarePoint(here.x + dx, here.y + dy)
                for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2))
                if 0 <= here.x + dx <= 16 and 0 <= here.y + dy <= 16
            )
            if isinstance(destination, BoardSquare) and here in _adjacent_intersections(destination_point):
                next_points += (destination_point,)
            next_points += tuple(
                center for center in edge_centers
                if abs(center.x - here.x) == 1 and abs(center.y - here.y) == 1
            )
        return next_points

    def fewest_waypoints() -> tuple[HalfSquarePoint, ...]:
        # The state remembers the incoming heading, which completely decides
        # whether another grid edge can share the current straight waypoint.
        # This is a bounded Dijkstra search over at most 84 points * 8 headings.
        Heading = tuple[int, int] | None
        State = tuple[HalfSquarePoint, Heading]
        first: State = (source_point, None)
        best: dict[State, tuple[int, int]] = {first: (1, 0)}
        previous_state: dict[State, State | None] = {first: None}
        heap: list[tuple[int, int, int, State]] = [(1, 0, 0, first)]
        serial = 0
        while heap:
            kept, steps, _, state = heappop(heap)
            if (kept, steps) != best[state]:
                continue
            here, heading = state
            if here == destination_point:
                reverse: list[HalfSquarePoint] = []
                cursor: State | None = state
                while cursor is not None:
                    reverse.append(cursor[0])
                    cursor = previous_state[cursor]
                reverse.reverse()
                return _compress_collinear(reverse)
            for next_point in neighbors(here):
                if not clear(here, next_point):
                    continue
                dx, dy = next_point.x - here.x, next_point.y - here.y
                next_heading = ((dx > 0) - (dx < 0), (dy > 0) - (dy < 0))
                same_grid_run = (
                    heading == next_heading and
                    not (here.x & 1 or here.y & 1 or next_point.x & 1 or next_point.y & 1) and
                    (dx == 0 or dy == 0)
                )
                cost = (kept + (0 if same_grid_run else 1), steps + 1)
                next_state: State = (next_point, next_heading)
                if next_state not in best or cost < best[next_state]:
                    best[next_state] = cost
                    previous_state[next_state] = state
                    serial += 1
                    heappush(heap, (cost[0], cost[1], serial, next_state))
        raise NoRouteError(
            f"No route from {source!r} to {destination!r} at the specified clearance"
        )

    queue = deque([source_point])
    previous: dict[HalfSquarePoint, HalfSquarePoint | None] = {source_point: None}
    while queue:
        here = queue.popleft()
        if here == destination_point:
            points: list[HalfSquarePoint] = []
            cursor: HalfSquarePoint | None = here
            while cursor is not None:
                points.append(cursor)
                cursor = previous[cursor]
            points.reverse()
            compressed = _compress_collinear(points)
            if max_waypoints is not None and len(compressed) > max_waypoints:
                compressed = fewest_waypoints()
                if len(compressed) > max_waypoints:
                    raise RouteTooLongError(len(compressed), max_waypoints)
            return CarryRoute(transfer, compressed, geometry_applied=geometry is not None)

        for next_point in neighbors(here):
            if next_point not in previous and clear(here, next_point):
                previous[next_point] = here
                queue.append(next_point)

    raise NoRouteError(
        f"No route from {source!r} to {destination!r} at the specified clearance"
    )


def find_reachable_parking_slot(
    source: BoardSquare,
    piece: str,
    slots: tuple[str | None, ...],
    *,
    occupied_squares: Iterable[BoardSquare],
    geometry: RouteGeometry | None = None,
    max_waypoints: int | None = None,
) -> ParkingSlot:
    """Choose the first vacant slot with a route from ``source``.

    This has the ``plan_move`` slot-selector call signature when the occupancy
    and geometry keywords are bound with ``functools.partial``. The supplied
    square occupancy must be the state at the instant this piece is parked.
    """

    inventory = ParkingInventory(slots)
    squares = frozenset(occupied_squares)
    parked = frozenset(ParkingSlot(i) for i, symbol in enumerate(inventory.slots) if symbol is not None)
    if len(parked) == len(inventory.slots):
        raise NoEmptyParkingSlotError("All 32 parking slots are occupied")
    too_long: RouteTooLongError | None = None
    for slot_id, symbol in enumerate(inventory.slots):
        if symbol is not None:
            continue
        slot = ParkingSlot(slot_id)
        transfer = Transfer(piece, source, slot, "park")
        try:
            plan_transfer_route(
                transfer,
                occupied_squares=squares,
                occupied_parking_slots=parked,
                geometry=geometry,
                max_waypoints=max_waypoints,
            )
        except NoRouteError:
            continue
        except RouteTooLongError as exc:
            too_long = exc
            continue
        return slot
    if too_long is not None:
        raise too_long
    raise NoRouteError("No empty parking slot is reachable at the specified clearance")


class ReachableParkingSlotSelector:
    """One-plan callback that advances occupancy after each parking choice.

    Construct a fresh instance for each ``plan_move`` call. It accounts for a
    captured victim's square becoming vacant before a promoted pawn is parked
    in the same logical move. The resulting `MovePlan` remains predictive:
    this selector does not confirm or execute any physical transfer.
    """

    def __init__(
        self,
        occupied_squares: Iterable[BoardSquare],
        *,
        geometry: RouteGeometry | None = None,
        max_waypoints: int | None = None,
    ) -> None:
        self.current_squares = set(occupied_squares)
        if any(not isinstance(square, BoardSquare) for square in self.current_squares):
            raise RouteError("occupied_squares must contain BoardSquare values")
        self.geometry = geometry
        self.max_waypoints = max_waypoints

    def __call__(self, source: BoardSquare, piece: str, slots: tuple[str | None, ...]) -> ParkingSlot:
        slot = find_reachable_parking_slot(
            source, piece, slots,
            occupied_squares=self.current_squares,
            geometry=self.geometry,
            max_waypoints=self.max_waypoints,
        )
        self.current_squares.remove(source)
        return slot

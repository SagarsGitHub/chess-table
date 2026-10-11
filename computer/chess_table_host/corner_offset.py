"""Measured preflight for temporary, within-square piece nudges.

OFFSET carries to a fixed point 0.30 square pitches toward a selected corner.
The square stays occupied in the controller bitmap. The host checks the
nudge segment and subsequent carried routes against each displaced piece.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil, floor, isfinite

from .planner import BoardSquare, ParkingSlot, PieceOffset, RookOffset
from .routing import (
    CarryRoute, NoRouteError, RouteError, RouteGeometry,
    _distance_squared_to_segment, _f32, compensated_release_target_mm,
    loaded_route_segments_mm, location_point, required_offset_clearance_mm,
)


_CORNER_SIGNS = {
    "NW": (-1, 1), "NE": (1, 1),
    "SW": (-1, -1), "SE": (1, -1),
}
_NUDGE_FRACTION = 0.30


@dataclass(frozen=True)
class OffsetPoint:
    x: float
    y: float


@dataclass(frozen=True)
class OffsetRoute:
    action: RookOffset | PieceOffset
    points: tuple[OffsetPoint, OffsetPoint]
    start_mm: tuple[float, float]
    end_mm: tuple[float, float]
    release_mm: tuple[float, float]
    geometry_applied: bool = True
    clearance_ignored: bool = False


def _offset_axis_mm(
    half: int, sign: int, span: float, saved_origin: float | None,
    geometry: RouteGeometry,
) -> float:
    """Mirror firmware routePoint(center) + 0.30f * pitch, then pulse rounding."""
    pitch = _f32(geometry.square_pitch_mm)
    travel = _f32(span)
    inset = _f32(geometry.end_inset_mm)
    ppm = _f32(geometry.pulses_per_mm)
    origin = _f32(saved_origin) if saved_origin is not None else _f32(
        _f32(travel - _f32(_f32(8.0) * pitch)) / _f32(2.0)
    )
    center = _f32(origin + _f32(_f32(float(half)) * _f32(pitch / _f32(2.0))))
    delta = _f32(_f32(_NUDGE_FRACTION) * pitch)
    target = _f32(center + (delta if sign > 0 else -delta))
    pulses = floor(_f32(target * ppm) + 0.5)
    low = ceil(_f32(inset * ppm))
    high = floor(_f32(_f32(travel - inset) * ppm))
    if pulses < low or pulses > high:
        raise NoRouteError("piece corner offset is outside measured travel bounds")
    return pulses / ppm


def offset_target_mm(
    action: RookOffset | PieceOffset, geometry: RouteGeometry,
) -> tuple[float, float]:
    if not isinstance(action, (RookOffset, PieceOffset)):
        raise TypeError("action must be a RookOffset or PieceOffset")
    point = location_point(action.square)
    dx, dy = _CORNER_SIGNS[action.corner]
    return (
        _offset_axis_mm(point.x, dx, geometry.x_travel_mm,
                        geometry.board_origin_x_mm, geometry),
        _offset_axis_mm(point.y, dy, geometry.y_travel_mm,
                        geometry.board_origin_y_mm, geometry),
    )


def plan_offset(
    action: RookOffset | PieceOffset,
    squares: dict[str, str], slots: list[str | None],
    geometry: RouteGeometry, *,
    offset_positions_mm: Mapping[BoardSquare, tuple[float, float]] | None = None,
) -> OffsetRoute:
    """Reject a nudge that passes too close to any centered or offset piece.

    ``offset_positions_mm`` describes all currently displaced board pieces.
    ``OUT`` starts at its square center and ``IN`` starts at the matching
    firmware-defined corner target. All standing obstacles use their actual
    commanded millimeter positions.
    """
    if not isinstance(action, (RookOffset, PieceOffset)):
        raise TypeError("action must be a RookOffset or PieceOffset")
    if not isinstance(geometry, RouteGeometry):
        raise TypeError("geometry must be a RouteGeometry")
    if offset_positions_mm is not None and not isinstance(offset_positions_mm, Mapping):
        raise TypeError("offset_positions_mm must be a mapping")
    if squares.get(action.square.name) != action.piece:
        raise NoRouteError("offset source is not occupied by its planned piece")
    offsets = dict(offset_positions_mm or {})
    for square, position in offsets.items():
        if not isinstance(square, BoardSquare) or square.name not in squares:
            raise NoRouteError("offset obstacle must be an occupied board square")
        if (not isinstance(position, tuple) or len(position) != 2 or
                any(not isinstance(value, (int, float)) or isinstance(value, bool) or
                    not isfinite(value) for value in position)):
            raise ValueError("offset obstacle needs finite millimeter coordinates")
        nominal = geometry.physical_point_mm(location_point(square))
        if any(abs(value - center) > geometry.square_pitch_mm / 2 or
               not geometry.end_inset_mm <= value <= span - geometry.end_inset_mm
               for value, center, span in zip(position, nominal,
                    (geometry.x_travel_mm, geometry.y_travel_mm))):
            raise NoRouteError("offset obstacle lies outside its square or measured travel")
    is_displaced = action.square in offsets
    if action.direction == "OUT" and is_displaced:
        raise NoRouteError("cannot nudge an already displaced piece outward")
    if action.direction == "IN" and not is_displaced:
        raise NoRouteError("cannot restore a piece that is not displaced")
    center_point = location_point(action.square)
    try:
        center = geometry.physical_point_mm(center_point)
        corner = offset_target_mm(action, geometry)
        board_obstacles = tuple(
            (BoardSquare(name), geometry.physical_point_mm(location_point(BoardSquare(name))))
            for name in squares if name != action.square.name
        )
        parked_obstacles = tuple(
            geometry.physical_point_mm(location_point(ParkingSlot(index)))
            for index, piece in enumerate(slots) if piece is not None
        )
        nominal_obstacles = tuple(position for _, position in board_obstacles) + parked_obstacles
        actual_obstacles = tuple(
            offsets.get(square, position) for square, position in board_obstacles
        ) + parked_obstacles
    except RouteError as exc:
        raise NoRouteError("offset or occupied piece lies outside measured bounds") from exc
    if action.direction == "IN" and any(
        abs(a - b) > 1e-6 for a, b in zip(offsets[action.square], corner)
    ):
        raise NoRouteError("restore corner does not match the displaced piece")
    required_squared = required_offset_clearance_mm(geometry) ** 2
    # Firmware still checks its nominal MAP centers for every OFFSET command.
    # Check both that picture and the host's actual displaced-piece positions.
    if any(_distance_squared_to_segment(obstacle, center, corner) < required_squared
           for obstacle in (*nominal_obstacles, *actual_obstacles)):
        raise NoRouteError("piece corner offset lacks measured piece clearance")
    start_mm, end_mm = ((center, corner) if action.direction == "OUT"
                        else (corner, center))
    release_mm = compensated_release_target_mm(start_mm, end_mm, geometry)
    if any(_distance_squared_to_segment(obstacle, end_mm, release_mm) < required_squared
           for obstacle in (*nominal_obstacles, *actual_obstacles)):
        raise NoRouteError("piece compensated release lacks measured piece clearance")
    dx, dy = _CORNER_SIGNS[action.corner]
    half_delta = 2 * _NUDGE_FRACTION
    corner_point = OffsetPoint(center_point.x + dx * half_delta,
                               center_point.y + dy * half_delta)
    nominal_center = OffsetPoint(float(center_point.x), float(center_point.y))
    if action.direction == "OUT":
        return OffsetRoute(action, (nominal_center, corner_point), center, corner,
                           release_mm)
    return OffsetRoute(action, (corner_point, nominal_center), corner, center,
                       release_mm)


def plan_rook_offset(
    action: RookOffset, squares: dict[str, str], slots: list[str | None],
    geometry: RouteGeometry,
) -> OffsetRoute:
    """Preserve the castling preflight API for old and new checkpoints."""
    if not isinstance(action, RookOffset):
        raise TypeError("action must be a RookOffset")
    offsets = (
        {action.square: offset_target_mm(action, geometry)}
        if action.direction == "IN" else None
    )
    return plan_offset(action, squares, slots, geometry,
                       offset_positions_mm=offsets)


def require_route_clear_of_offset(
    route: CarryRoute, offset: RookOffset | PieceOffset, geometry: RouteGeometry,
) -> None:
    """Keep a carried piece clear of one known offset corner."""
    require_route_clear_of_offsets(route, {offset.square: offset_target_mm(offset, geometry)}, geometry)


def require_route_clear_of_offsets(
    route: CarryRoute,
    offset_positions_mm: Mapping[BoardSquare, tuple[float, float]],
    geometry: RouteGeometry,
) -> None:
    """Check every carried segment against actual displaced-piece positions."""
    required_squared = required_offset_clearance_mm(geometry) ** 2
    segments = loaded_route_segments_mm(route, geometry)
    for square, obstacle in offset_positions_mm.items():
        if isinstance(route.transfer.source, BoardSquare) and square == route.transfer.source:
            continue
        if any(_distance_squared_to_segment(obstacle, start, end) < required_squared
               for start, end in segments):
            raise NoRouteError("carry route passes too close to an offset piece")

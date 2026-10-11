"""Bounded, reversible in-square nudges for a crowded carry.

The whole OUT, CARRY, IN bundle is checked against the inventory before any
motion command is sent. An offset changes a piece's physical center, not its
logical board square or the controller occupancy bitmap.
"""

from __future__ import annotations

from itertools import combinations, product
from math import sqrt

from .corner_offset import offset_target_mm, plan_offset, required_offset_clearance_mm
from .physical import _apply_action, _occupied_slots, _occupied_squares
from .planner import BoardSquare, ParkingSlot, PieceOffset, Transfer
from .protocol_limits import MAX_CARRY_WAYPOINTS
from .routing import (
    CarryRoute, RouteError, RouteGeometry,
    _distance_squared_to_segment, loaded_route_segments_mm,
    location_point, plan_transfer_route,
)


_CORNERS = ("NW", "NE", "SW", "SE")
_MAX_OFFSET_PIECES = 2
_MAX_CANDIDATES = 6


def _distance_to_route_mm(
    position: tuple[float, float], route: CarryRoute, geometry: RouteGeometry,
) -> float:
    return sqrt(min(
        _distance_squared_to_segment(position, start, end)
        for start, end in loaded_route_segments_mm(route, geometry)
    ))


def route_has_tight_squeeze(
    route: CarryRoute, squares: dict[str, str], slots: list[str | None],
    geometry: RouteGeometry,
) -> bool:
    """Whether a permissive carry violates measured standing-piece clearance."""
    source = route.transfer.source
    threshold_squared = required_offset_clearance_mm(geometry) ** 2
    standing = [
        geometry.physical_point_mm(location_point(BoardSquare(name)))
        for name in squares if BoardSquare(name) != source
    ]
    standing.extend(
        geometry.physical_point_mm(location_point(ParkingSlot(index)))
        for index, piece in enumerate(slots)
        if piece is not None and ParkingSlot(index) != source
    )
    for start, end in loaded_route_segments_mm(route, geometry):
        for center in standing:
            if _distance_squared_to_segment(center, start, end) < threshold_squared - 1e-9:
                return True
    return False


def plan_offset_cleared_transfer(
    transfer: Transfer, squares: dict[str, str], slots: list[str | None],
    geometry: RouteGeometry,
) -> tuple[PieceOffset | Transfer, ...] | None:
    """Find up to two neighbor offsets that make a strict carry possible.

    Candidates come from the nominal short route that ignores diameter but
    still obeys occupied centers and the Uno's waypoint cap. Every proposed
    OUT, strict CARRY, and reverse IN is individually preflighted. The search
    is intentionally bounded so a long crowded corridor remains unresolved.
    """
    if not isinstance(transfer, Transfer):
        raise TypeError("transfer must be a Transfer")
    try:
        relaxed = plan_transfer_route(
            transfer, occupied_squares=_occupied_squares(squares),
            occupied_parking_slots=_occupied_slots(slots), geometry=geometry,
            max_waypoints=MAX_CARRY_WAYPOINTS, ignore_piece_clearance=True,
        )
    except RouteError:
        return None

    # Only occupied board squares can receive OFFSET. If the relaxed route is
    # crowded by more than two pieces, the subset search below may still find
    # a strict detour after moving one or two of the strongest blockers.
    source = transfer.source
    clearance = required_offset_clearance_mm(geometry)
    ranked: list[tuple[float, BoardSquare]] = []
    for name in squares:
        square = BoardSquare(name)
        if square == source:
            continue
        center = geometry.physical_point_mm(location_point(square))
        distance = _distance_to_route_mm(center, relaxed, geometry)
        if distance < clearance:
            ranked.append((distance, square))
    ranked.sort(key=lambda item: (item[0], item[1].name))
    candidates = [square for _, square in ranked[:_MAX_CANDIDATES]]
    if not candidates:
        return None

    for count in range(1, _MAX_OFFSET_PIECES + 1):
        for blockers in combinations(candidates, count):
            # Prefer corners that move away from the tight candidate route.
            corners_for = []
            for square in blockers:
                choices = []
                for corner in _CORNERS:
                    out = PieceOffset(
                        squares[square.name], square, corner, "OUT",
                        "clearance_offset_out",
                    )
                    try:
                        target = offset_target_mm(out, geometry)
                    except RouteError:
                        continue
                    choices.append((
                        -_distance_to_route_mm(target, relaxed, geometry), corner,
                    ))
                corners_for.append(tuple(corner for _, corner in sorted(choices)))
            for chosen_corners in product(*corners_for):
                offsets: dict[BoardSquare, tuple[float, float]] = {}
                out_actions: list[PieceOffset] = []
                try:
                    for square, corner in zip(blockers, chosen_corners):
                        out = PieceOffset(
                            squares[square.name], square, corner, "OUT",
                            "clearance_offset_out",
                        )
                        plan_offset(
                            out, squares, slots, geometry,
                            offset_positions_mm=offsets,
                        )
                        offsets[square] = offset_target_mm(out, geometry)
                        out_actions.append(out)
                    carry = Transfer(
                        transfer.piece, transfer.source, transfer.destination,
                        "clearance_offset_move",
                    )
                    plan_transfer_route(
                        carry, occupied_squares=_occupied_squares(squares),
                        occupied_parking_slots=_occupied_slots(slots),
                        geometry=geometry, max_waypoints=MAX_CARRY_WAYPOINTS,
                        offset_positions_mm=offsets,
                    )
                    after_squares, after_slots = dict(squares), list(slots)
                    _apply_action(after_squares, after_slots, carry)
                    in_actions: list[PieceOffset] = []
                    for out in reversed(out_actions):
                        restore = PieceOffset(
                            out.piece, out.square, out.corner, "IN",
                            "clearance_offset_in",
                        )
                        plan_offset(
                            restore, after_squares, after_slots, geometry,
                            offset_positions_mm=offsets,
                        )
                        del offsets[out.square]
                        in_actions.append(restore)
                    if offsets:
                        raise AssertionError("offset bundle did not restore every piece")
                    return (*out_actions, carry, *in_actions)
                except RouteError:
                    continue
    return None

"""Find a reversible single-blocker detour for a crowded physical carry.

This changes only the physical action sequence. A caller still commits one
legal chess move, and each detour leg is routed against the inventory left by
its predecessor before any motor command is sent.
"""

from __future__ import annotations

import chess

from .physical import _apply_action, _occupied_slots, _occupied_squares
from .planner import PARKING_SLOT_COUNT, BoardSquare, ParkingSlot, Transfer
from .protocol_limits import MAX_CARRY_WAYPOINTS
from .routing import (
    RouteError, RouteGeometry, _distance_squared_to_segment,
    location_point, plan_transfer_route,
)


KNIGHT_STAGE_REASONS = frozenset({
    "knight_stage", "knight_clearance_move", "knight_restore",
})
CLEARANCE_STAGE_REASONS = frozenset({
    "clearance_stage", "clearance_move", "clearance_restore",
})


def _square_distance(a: str, b: str) -> tuple[int, int]:
    file_distance = abs(ord(a[0]) - ord(b[0]))
    rank_distance = abs(int(a[1]) - int(b[1]))
    return max(file_distance, rank_distance), file_distance + rank_distance


def _distance_to_segment(name: str, source: BoardSquare, destination: BoardSquare | ParkingSlot) -> float:
    """Squared distance to the direct carry corridor, in half-square units."""
    point = location_point(BoardSquare(name))
    start = location_point(source)
    end = location_point(destination)
    return _distance_squared_to_segment(
        (point.x, point.y), (start.x, start.y), (end.x, end.y),
    )


def _rank_blockers(
    squares: dict[str, str], source: BoardSquare,
    destination: BoardSquare | ParkingSlot,
) -> list[str]:
    """Search the whole board, favoring nearby pieces on the direct corridor."""
    end_point = location_point(destination)
    source_point = location_point(source)

    def score(name: str) -> tuple[float, int, bool, int, str]:
        point = location_point(BoardSquare(name))
        return (
            _distance_to_segment(name, source, destination),
            min(
                _square_distance(name, source.name)[1] * 2,
                abs(point.x - end_point.x) + abs(point.y - end_point.y),
            ),
            # When scores tie, a piece on the mover's file is often the
            # useful pawn in a crowded opening knight route.
            name[0] != source.name[0],
            abs(point.x - source_point.x) + abs(point.y - source_point.y),
            name,
        )

    return sorted((name for name in squares if name != source.name), key=score)


def _strict_route(
    action: Transfer, squares: dict[str, str], slots: list[str | None],
    geometry: RouteGeometry,
) -> bool:
    try:
        plan_transfer_route(
            action,
            occupied_squares=_occupied_squares(squares),
            occupied_parking_slots=_occupied_slots(slots),
            geometry=geometry,
            max_waypoints=MAX_CARRY_WAYPOINTS,
        )
    except RouteError:
        return False
    return True


def _plan_detour(
    squares_before: dict[str, str], slots_before: list[str | None],
    transfer: Transfer, geometry: RouteGeometry, reasons: tuple[str, str, str],
) -> tuple[Transfer, Transfer, Transfer] | None:
    if not isinstance(transfer.source, BoardSquare) or not isinstance(
        transfer.destination, (BoardSquare, ParkingSlot)
    ):
        return None
    if transfer.source == transfer.destination:
        return None
    if len(slots_before) != PARKING_SLOT_COUNT:
        raise ValueError(f"parking inventory must contain {PARKING_SLOT_COUNT} slots")
    if squares_before.get(transfer.source.name) != transfer.piece:
        return None
    if isinstance(transfer.destination, BoardSquare):
        if transfer.destination.name in squares_before:
            return None
    elif slots_before[transfer.destination.slot_id] is not None:
        return None

    # A detour is unnecessary when the carry already fits measured clearance.
    if _strict_route(transfer, squares_before, slots_before, geometry):
        return None

    expected_squares = dict(squares_before)
    expected_slots = list(slots_before)
    _apply_action(expected_squares, expected_slots, transfer)
    middle = Transfer(transfer.piece, transfer.source, transfer.destination, reasons[1])
    vacant_squares = [
        name for name in chess.SQUARE_NAMES
        if name not in squares_before and BoardSquare(name) != transfer.destination
    ]
    vacant_slots = [
        ParkingSlot(i) for i, piece in enumerate(slots_before)
        if piece is None and ParkingSlot(i) != transfer.destination
    ]

    # Removing a blocker can only make the middle route easier than staging it
    # somewhere else. If the carry still cannot route in that optimistic state,
    # skip every possible staging destination for that blocker.
    blockers: list[tuple[str, str]] = []
    for blocker_name in _rank_blockers(squares_before, transfer.source, transfer.destination):
        without_blocker = dict(squares_before)
        piece = without_blocker.pop(blocker_name)
        if _strict_route(middle, without_blocker, slots_before, geometry):
            blockers.append((blocker_name, piece))

    # Try every board option before temporary parking. A short stage usually
    # clears the corridor without carrying an unrelated piece off the board.
    for board_staging in (True, False):
        for blocker_name, piece in blockers:
            blocker = BoardSquare(blocker_name)
            destinations = (
                [BoardSquare(name) for name in sorted(
                    vacant_squares,
                    key=lambda name: (
                        _square_distance(name, blocker_name)[1],
                        _square_distance(name, blocker_name)[0],
                        name,
                    ),
                )]
                if board_staging else vacant_slots
            )
            for staging in destinations:
                out = Transfer(piece, blocker, staging, reasons[0])
                squares = dict(squares_before)
                slots = list(slots_before)
                if not _strict_route(out, squares, slots, geometry):
                    continue
                _apply_action(squares, slots, out)
                if not _strict_route(middle, squares, slots, geometry):
                    continue
                _apply_action(squares, slots, middle)
                back = Transfer(piece, staging, blocker, reasons[2])
                if not _strict_route(back, squares, slots, geometry):
                    continue
                _apply_action(squares, slots, back)
                if squares == expected_squares and slots == expected_slots:
                    return out, middle, back
    return None


def plan_cleared_transfer(
    squares_before: dict[str, str], slots_before: list[str | None],
    transfer: Transfer, geometry: RouteGeometry,
) -> tuple[Transfer, Transfer, Transfer] | None:
    """Stage one board blocker, carry any piece, then restore that blocker.

    Board-to-board and board-to-parking carries are supported. All three legs
    must meet measured clearance and the 12-waypoint firmware limit. Every
    occupied board square may be considered as the blocker, so this works in
    middlegame layouts as well as crowded opening positions. ``None`` means
    no single-blocker solution was found; it never authorizes an unchecked
    tight route. The caller must preflight any subsequent actions in the
    chess move against the resulting physical inventory.
    """
    return _plan_detour(
        squares_before, slots_before, transfer, geometry,
        ("clearance_stage", "clearance_move", "clearance_restore"),
    )


def plan_tight_knight_move(
    squares_before: dict[str, str], slots_before: list[str | None],
    knight: Transfer, geometry: RouteGeometry,
) -> tuple[Transfer, Transfer, Transfer] | None:
    """Compatibility wrapper retaining the original knight action reasons."""
    if (knight.piece.upper() != "N" or
            not isinstance(knight.source, BoardSquare) or
            not isinstance(knight.destination, BoardSquare)):
        return None
    return _plan_detour(
        squares_before, slots_before, knight, geometry,
        ("knight_stage", "knight_clearance_move", "knight_restore"),
    )

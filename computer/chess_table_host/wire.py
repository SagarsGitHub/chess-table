"""Compact route and occupancy encoding for the Uno's framed protocol.

These helpers serialize planned state; they do not establish that a route is
physically clear or that a completed motor command moved a piece successfully.
"""

from __future__ import annotations

import chess

from .planner import BoardSquare, ParkingInventory, ParkingSlot
from .protocol_limits import MAX_CARRY_WAYPOINTS
from .routing import CarryRoute, location_point


_BASE17 = "0123456789ABCDEFG"


def location_id(location: BoardSquare | ParkingSlot) -> int:
    """Return the firmware's 0..95 position ID (a1=0; P00=64)."""

    if isinstance(location, BoardSquare):
        return (int(location.name[1]) - 1) * 8 + ord(location.name[0]) - ord("a")
    if isinstance(location, ParkingSlot):
        return 64 + location.slot_id
    raise TypeError("location must be a board square or parking slot")


def encode_carry(route: CarryRoute, *, max_waypoints: int = MAX_CARRY_WAYPOINTS) -> tuple[str, str, str]:
    """Build the source, destination, and waypoint arguments for a carry.

    Waypoints include the source and destination. Each x/y coordinate is one
    base-17 character (0..9, A..G for 10..16), making 12 points only 24 bytes.
    For ``CARRY_OFFSET``, the source-center point is an encoding anchor; the
    caller inserts the corner argument and firmware picks up at that corner.
    Firmware validates the path again before energizing the magnet.
    """

    if not isinstance(route, CarryRoute):
        raise TypeError("route must be a CarryRoute")
    if type(max_waypoints) is not int or not 2 <= max_waypoints <= MAX_CARRY_WAYPOINTS:
        raise ValueError(f"max_waypoints must be 2..{MAX_CARRY_WAYPOINTS}")
    if len(route.points) > max_waypoints:
        raise ValueError(f"route has {len(route.points)} points; limit is {max_waypoints}")
    if route.points[0] != location_point(route.transfer.source):
        raise ValueError("route does not start at its transfer source")
    if route.points[-1] != location_point(route.transfer.destination):
        raise ValueError("route does not end at its transfer destination")
    point_chars = "".join(_BASE17[point.x] + _BASE17[point.y] for point in route.points)
    return (str(location_id(route.transfer.source)), str(location_id(route.transfer.destination)), point_chars)


def encode_map(board: chess.Board, parking: ParkingInventory) -> str:
    """Return the 12-byte occupancy map as 24 uppercase hex characters.

    Bit positions 0..63 are python-chess square IDs; 64..95 are slots 00..31.
    The host must call this with a position reconciled to the physical board.
    """

    if not isinstance(board, chess.Board):
        raise TypeError("board must be a chess.Board")
    if not isinstance(parking, ParkingInventory):
        raise TypeError("parking must be a ParkingInventory")
    bitmap = bytearray(12)
    for square in board.piece_map():
        bitmap[square // 8] |= 1 << (square % 8)
    for slot_id, piece in enumerate(parking.slots):
        if piece is not None:
            position = 64 + slot_id
            bitmap[position // 8] |= 1 << (position % 8)
    return bitmap.hex().upper()

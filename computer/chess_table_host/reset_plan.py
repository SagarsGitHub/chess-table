"""Preflight every transfer needed to restore the standard physical setup.

This module plans against copies of the checkpoint inventory. No controller
command is sent until GameRunner saves the completed plan.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

import chess

from .game_errors import GameStateError
from .physical import _accessible_parking, _apply_action, _board_pieces, _parking_approaches, _piece_at
from .planner import BoardSquare, ParkingSlot, Transfer
from .routing import CarryRoute, NoRouteError, RouteError, RouteGeometry, location_point


def plan_reset(
    squares_before: dict[str, str], slots_before: list[str | None],
    geometry: RouteGeometry,
    route_transfer: Callable[[Transfer, dict[str, str], list[str | None], RouteGeometry], CarryRoute],
) -> tuple[Transfer, ...]:
    """Plan every carry against a copied inventory before any motor command.

    Direct returns take priority. Staging breaks cycles only when needed, and
    each candidate is checked for route feasibility and remaining slot exits.
    """
    target = _board_pieces(chess.Board())
    squares = dict(squares_before)
    slots = list(slots_before)
    if Counter([*squares.values(), *(piece for piece in slots if piece)]) != Counter(target.values()):
        raise GameStateError("physical pieces do not match the 32 standard starting pieces")
    if squares == target and not any(slots):
        return ()

    actions: list[Transfer] = []
    staging = tuple(
        f"{file}{rank}" for rank in "3456" for file in "abcdefgh"
    )

    def piece_priority(piece: str) -> int:
        """Prefer awkward and back-rank pieces over pawns."""
        if piece.upper() in "BN":
            return 0
        if piece.upper() in "KQR":
            return 1
        return 2

    def vacant_homes(piece: str, source: BoardSquare | ParkingSlot) -> list[str]:
        """Try matching home squares in nearest-first order."""
        source_point = location_point(source)
        return sorted(
            (name for name, symbol in target.items()
             if symbol == piece and name not in squares),
            key=lambda name: (
                (location_point(BoardSquare(name)).x - source_point.x) ** 2 +
                (location_point(BoardSquare(name)).y - source_point.y) ** 2,
                name,
            ),
        )

    def try_transfer(source: BoardSquare | ParkingSlot, destination: BoardSquare) -> bool:
        """Commit a candidate to the plan only after routing succeeds."""
        piece = _piece_at(squares, slots, source)
        if piece is None or _piece_at(squares, slots, destination) is not None:
            return False
        transfer = Transfer(piece, source, destination, "reset_board")
        try:
            route_transfer(transfer, squares, slots, geometry)
        except RouteError:
            return False
        _apply_action(squares, slots, transfer)
        actions.append(transfer)
        return True

    def accessible_after(transfer: Transfer) -> set[int]:
        """Probe parking exits on copies; candidate checks never mutate the plan."""
        trial_squares = dict(squares)
        trial_slots = list(slots)
        _apply_action(trial_squares, trial_slots, transfer)
        return _accessible_parking(trial_slots, trial_squares)

    def preserves_remaining_exits(transfer: Transfer) -> bool:
        """A parked pickup must leave every other occupied slot retrievable."""
        return len(accessible_after(transfer)) == sum(piece is not None for piece in slots) - 1

    def try_direct_home(name: str) -> bool:
        """Return a board piece without closing an occupied slot's exit."""
        if squares[name] == target.get(name):
            return False
        source = BoardSquare(name)
        before = _accessible_parking(slots, squares)
        for home in vacant_homes(squares[name], source):
            destination = BoardSquare(home)
            trial = Transfer(squares[name], source, destination, "reset_board")
            if not before.issubset(accessible_after(trial)):
                continue
            if try_transfer(source, destination):
                return True
        return False

    def stage(source_name: str, *, opening: int | None = None) -> bool:
        """Use a vacant interior square to break a return or exit cycle."""
        source = BoardSquare(source_name)
        before = _accessible_parking(slots, squares)
        source_point = location_point(source)
        destinations = sorted(
            (name for name in staging if name not in squares and name != source_name),
            key=lambda name: (
                sum(name in _parking_approaches(ParkingSlot(index))
                    for index, piece in enumerate(slots) if piece is not None),
                (location_point(BoardSquare(name)).x - source_point.x) ** 2 +
                (location_point(BoardSquare(name)).y - source_point.y) ** 2,
                name,
            ),
        )
        for name in destinations:
            destination = BoardSquare(name)
            trial = Transfer(squares[source_name], source, destination, "reset_board")
            after = accessible_after(trial)
            if not before.issubset(after) or (opening is not None and opening not in after):
                continue
            if try_transfer(source, destination):
                return True
        return False

    # Prefer a one-carry return over staging. Retry direct homes after each
    # transfer because vacating one square can expose another piece's home.
    # A direct return must not close an existing exit for a parked piece.
    while True:
        wrong_homes = sorted(
            (name for name, piece in squares.items()
             if name[1] in "1278" and piece != target.get(name)),
            key=lambda name: (piece_priority(squares[name]), name),
        )
        if not wrong_homes:
            break
        moved = False
        for name in wrong_homes:
            if try_direct_home(name):
                moved = True
                break
        if moved:
            continue
        # If a cycle needs a temporary square, move the easier blocker
        # first so an awkward piece can use its home in one carry.
        for name in sorted(wrong_homes,
                           key=lambda square: (-piece_priority(squares[square]), square)):
            if stage(name):
                moved = True
                break
        if not moved:
            raise NoRouteError("cannot clear misplaced home-rank pieces for board reset")

    # A corner parking slot has only one board-side exit. Temporarily stage
    # whichever edge piece blocks an occupied slot before retrieval.
    while True:
        accessible = _accessible_parking(slots, squares)
        blocked = [index for index, piece in enumerate(slots)
                   if piece is not None and index not in accessible]
        if not blocked:
            break
        slot_id = blocked[0]
        if not any(
            stage(name, opening=slot_id)
            for name in _parking_approaches(ParkingSlot(slot_id)) if name in squares
        ):
            raise NoRouteError(f"cannot open an approach to parking slot {slot_id}")

    # Return off-home back-rank pieces from the board before retrieving
    # parked pawns. All occupied parking slots are accessible now, and
    # each direct return must preserve every one of those exits.
    while True:
        candidates = sorted(
            (name for name, piece in squares.items()
             if piece_priority(piece) < 2 and piece != target.get(name)),
            key=lambda name: (piece_priority(squares[name]), name),
        )
        if not any(try_direct_home(name) for name in candidates):
            break

    while any(slots):
        accessible = _accessible_parking(slots, squares)
        choices = sorted(
            (index for index, piece in enumerate(slots) if piece is not None),
            key=lambda index: (
                piece_priority(slots[index]),
                sum(name not in squares for name in _parking_approaches(ParkingSlot(index))),
                index,
            ),
        )
        chosen = False
        for index in choices:
            if index not in accessible:
                continue
            source = ParkingSlot(index)
            destinations = vacant_homes(slots[index], source)
            for name in destinations:
                destination = BoardSquare(name)
                trial = Transfer(slots[index], source, destination, "reset_board")
                if not preserves_remaining_exits(trial):
                    continue
                if try_transfer(source, destination):
                    chosen = True
                    break
            if chosen:
                break
        if not chosen:
            # Parked pieces can form an ordering cycle even when every
            # slot has an open approach. For example, returning a queen
            # to d8 can close a bishop's only exit, returning that bishop
            # to f8 can close a rook's exit, and returning the rook to a8
            # can close the queen's. Bring one piece onto an interior
            # square, then retry the newly unblocked home transfers.
            # Keep the same route and remaining-exit checks as a direct
            # return; no speculative CARRY is sent during preflight.
            for index in choices:
                if index not in accessible:
                    continue
                source = ParkingSlot(index)
                source_point = location_point(source)
                destinations = sorted(
                    (name for name in staging if name not in squares),
                    key=lambda name: (
                        sum(name in _parking_approaches(ParkingSlot(other))
                            for other, piece in enumerate(slots)
                            if other != index and piece is not None),
                        (location_point(BoardSquare(name)).x - source_point.x) ** 2 +
                        (location_point(BoardSquare(name)).y - source_point.y) ** 2,
                        name,
                    ),
                )
                for name in destinations:
                    destination = BoardSquare(name)
                    trial = Transfer(slots[index], source, destination, "reset_board")
                    if not preserves_remaining_exits(trial):
                        continue
                    if try_transfer(source, destination):
                        chosen = True
                        break
                if chosen:
                    break
            if not chosen:
                raise NoRouteError("cannot retrieve remaining parked pieces without trapping a slot")

    while squares != target:
        chosen = False
        for name in sorted(tuple(squares),
                           key=lambda square: (piece_priority(squares[square]), square)):
            if squares[name] == target.get(name):
                continue
            source = BoardSquare(name)
            for home in vacant_homes(squares[name], source):
                if try_transfer(source, BoardSquare(home)):
                    chosen = True
                    break
            if chosen:
                break
        if not chosen:
            raise NoRouteError("cannot return staged pieces to the standard setup")
    if any(slots):
        raise GameStateError("reset plan left occupied parking")
    return tuple(actions)

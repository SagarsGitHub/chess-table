"""Preflight every transfer needed to restore the standard physical setup.

This module plans against copies of the checkpoint inventory. No controller
command is sent until GameRunner saves the completed plan.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

import chess

from .game_errors import GameStateError
from .physical import (
    _accessible_parking, _apply_action, _board_pieces, _occupied_slots,
    _occupied_squares, _parking_approaches, _piece_at,
)
from .planner import BoardSquare, ParkingSlot, PieceOffset, Transfer
from .protocol_limits import MAX_CARRY_WAYPOINTS
from .routing import (
    CarryRoute, NoRouteError, RouteError, RouteGeometry, location_point,
    plan_transfer_route,
)


ResetAction = Transfer | PieceOffset
ClearanceBundle = Callable[
    [Transfer, dict[str, str], list[str | None], RouteGeometry],
    tuple[ResetAction, ...] | None,
]


def plan_reset(
    squares_before: dict[str, str], slots_before: list[str | None],
    geometry: RouteGeometry,
    route_transfer: Callable[[Transfer, dict[str, str], list[str | None], RouteGeometry], CarryRoute],
    *, plan_clearance_bundle: ClearanceBundle | None = None,
) -> tuple[ResetAction, ...]:
    """Plan every carry against a copied inventory before any motor command.

    Parking exits stay open while safe, routable returns follow piece priority.
    Staging breaks cycles only when needed.
    """
    target = _board_pieces(chess.Board())
    squares = dict(squares_before)
    slots = list(slots_before)
    if Counter([*squares.values(), *(piece for piece in slots if piece)]) != Counter(target.values()):
        raise GameStateError("physical pieces do not match the 32 standard starting pieces")
    if squares == target and not any(slots):
        return ()

    actions: list[ResetAction] = []
    staging = tuple(
        f"{file}{rank}" for rank in "3456" for file in "abcdefgh"
    )

    def piece_priority(piece: str) -> int:
        """Return outer back-rank pieces first, then central pieces and pawns."""
        return {"R": 0, "N": 1, "B": 2, "K": 3, "Q": 3, "P": 4}[piece.upper()]

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
        selected: tuple[ResetAction, ...] | None = None
        if plan_clearance_bundle is not None:
            # Probe measured clearance without the caller's optional tight
            # fallback. Offset bundles are only useful for a strict failure.
            try:
                plan_transfer_route(
                    transfer,
                    occupied_squares=_occupied_squares(squares),
                    occupied_parking_slots=_occupied_slots(slots),
                    geometry=geometry, max_waypoints=MAX_CARRY_WAYPOINTS,
                )
            except RouteError:
                proposed = plan_clearance_bundle(transfer, squares, slots, geometry)
                if proposed is not None:
                    selected = validate_bundle(transfer, proposed)
            else:
                selected = (transfer,)
        if selected is None:
            try:
                route_transfer(transfer, squares, slots, geometry)
            except RouteError:
                return False
            selected = (transfer,)
        _apply_action(squares, slots, transfer)
        actions.extend(selected)
        return True

    def validate_bundle(
        transfer: Transfer, proposed: tuple[ResetAction, ...],
    ) -> tuple[ResetAction, ...]:
        """Keep a temporary nudge bundle atomic and inventory neutral."""
        if not isinstance(proposed, tuple):
            raise GameStateError("reset clearance bundle must be a tuple")
        trial_squares, trial_slots = dict(squares), list(slots)
        active: dict[BoardSquare, tuple[str, str]] = {}
        out_count = 0
        carried = False
        for action in proposed:
            if isinstance(action, PieceOffset):
                if _piece_at(trial_squares, trial_slots, action.square) != action.piece:
                    raise GameStateError("reset offset piece differs from physical inventory")
                identity = (action.piece, action.corner)
                if action.direction == "OUT":
                    if carried or action.square in active:
                        raise GameStateError("reset clearance offsets must precede the carry")
                    active[action.square] = identity
                    out_count += 1
                elif not carried or active.get(action.square) != identity:
                    raise GameStateError("reset clearance restoration has no matching nudge")
                else:
                    del active[action.square]
            elif isinstance(action, Transfer):
                if carried or not active or (
                    action.piece, action.source, action.destination
                ) != (transfer.piece, transfer.source, transfer.destination) or (
                    action.reason != "clearance_offset_move"
                ):
                    raise GameStateError("reset clearance bundle must carry the requested piece once")
                _apply_action(trial_squares, trial_slots, action)
                carried = True
            else:
                raise GameStateError("reset clearance bundle contains an unsupported action")
        if not carried or active or not 1 <= out_count <= 2:
            raise GameStateError("reset clearance bundle needs one or two restored nudges")
        expected_squares, expected_slots = dict(squares), list(slots)
        _apply_action(expected_squares, expected_slots, transfer)
        if trial_squares != expected_squares or trial_slots != expected_slots:
            raise GameStateError("reset clearance bundle changes the expected inventory")
        return proposed

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

    def parked_candidates() -> list[int]:
        """Rank occupied slots, using open approaches to break piece ties."""
        return sorted(
            (index for index, piece in enumerate(slots) if piece is not None),
            key=lambda index: (
                piece_priority(slots[index]),
                sum(name not in squares for name in _parking_approaches(ParkingSlot(index))),
                index,
            ),
        )

    def ordered_return_sources(
        board_names: list[str], parked_indices: list[int],
    ) -> tuple[BoardSquare | ParkingSlot, ...]:
        """Interleave returns by piece priority, with board pieces winning ties."""
        choices = [
            (piece_priority(squares[name]), 0, order, BoardSquare(name))
            for order, name in enumerate(board_names)
        ]
        choices.extend(
            (piece_priority(slots[index]), 1, order, ParkingSlot(index))
            for order, index in enumerate(parked_indices)
        )
        choices.sort(key=lambda choice: choice[:3])
        return tuple(choice[3] for choice in choices)

    def try_parked_home(index: int, accessible: set[int]) -> bool:
        """Return a parked piece only if all other parked exits stay open."""
        if index not in accessible:
            return False
        source = ParkingSlot(index)
        for name in vacant_homes(slots[index], source):
            destination = BoardSquare(name)
            trial = Transfer(slots[index], source, destination, "reset_board")
            if preserves_remaining_exits(trial) and try_transfer(source, destination):
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
        # Compare direct board and parked returns before staging a blocker.
        # A blocked board rook must not make a reachable parked rook wait
        # behind a pawn on another wrong home square.
        accessible = _accessible_parking(slots, squares)
        for source in ordered_return_sources(wrong_homes, parked_candidates()):
            if isinstance(source, BoardSquare):
                moved = try_direct_home(source.name)
            else:
                moved = try_parked_home(source.slot_id, accessible)
            if moved:
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

    # Compare direct board returns with parked returns by piece priority.
    # All occupied slots have an exit now; every candidate must preserve the
    # remaining exits. A blocked higher-priority piece can be retried after
    # another return opens space.
    while any(slots):
        accessible = _accessible_parking(slots, squares)
        board_choices = sorted(
            (name for name, piece in squares.items()
             if piece.upper() != "P" and piece != target.get(name)),
            key=lambda name: (piece_priority(squares[name]), name),
        )
        parked_choices = parked_candidates()
        chosen = False
        for source in ordered_return_sources(board_choices, parked_choices):
            if isinstance(source, BoardSquare):
                if try_direct_home(source.name):
                    chosen = True
                    break
                continue
            if try_parked_home(source.slot_id, accessible):
                chosen = True
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
            for index in parked_choices:
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

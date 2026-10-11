"""Checkpointed physical execution of legal chess moves.

The committed chess position changes only after every transfer in a move has
been accepted following controller completion or operator confirmation. A
separate physical snapshot represents the temporary position between the
victim and attacker legs of a capture.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Protocol

import chess

from .checkpoint import (
    PROMOTION_RULE, STATE_VERSION, GameState, JsonGameStore, PendingMove, PendingReset,
    RemoteMoveReceipt,
)
from .corner_offset import (
    OffsetRoute, offset_target_mm, plan_offset, plan_rook_offset, require_route_clear_of_offset,
    required_offset_clearance_mm,
)
from .game_errors import GameStateError
from .knight_stage import (
    CLEARANCE_STAGE_REASONS, KNIGHT_STAGE_REASONS,
    plan_cleared_transfer, plan_tight_knight_move,
)
from .physical import (
    _apply_action, _board_pieces, _occupied_slots, _occupied_squares,
    _physical_map, _piece_at, _replay_physical,
)
from .offset_stage import plan_offset_cleared_transfer, route_has_tight_squeeze
from .planner import (
    Action,
    BoardSquare,
    MovePlan,
    ParkingInventory,
    ParkingSlot,
    PieceOffset, PromotionPolicy,
    RookOffset,
    Transfer,
    plan_move,
)
from .protocol_limits import MAX_CARRY_WAYPOINTS
from .routing import (
    CarryRoute, NoRouteError, ReachableParkingSlotSelector,
    RouteGeometry, RouteTooLongError, plan_transfer_route,
)
from .reset_plan import plan_reset
from .wire import encode_carry


class ReconciliationRequired(GameStateError):
    """A physical action has an unknown result and must be inspected."""


class MoveDeferred(GameStateError):
    """The operator paused before a physical action began."""


class CarryLink(Protocol):
    def set_map(self, occupancy_hex: str): ...
    def carry(self, source_id: str, destination_id: str, point_chars: str): ...
    def carry_offset(self, source_id: str, corner: str,
                     destination_id: str, point_chars: str): ...
    def offset(self, square_id: int, corner: str, direction: str): ...


def _offset_wire(action: RookOffset | PieceOffset) -> tuple[str, str, str]:
    """Use the same saved wire arguments for move and reset nudges."""
    return (str(chess.parse_square(action.square.name)), action.corner, action.direction)


def _send_motion(
    link: CarryLink, action: Transfer | RookOffset | PieceOffset,
    wire: tuple[str, ...],
):
    """Dispatch a planned physical action after its in-flight checkpoint."""
    if isinstance(action, Transfer):
        if action.reason == "castle_rook_from_offset":
            return link.carry_offset(*wire)
        return link.carry(*wire)
    return link.offset(chess.parse_square(action.square.name), action.corner, action.direction)


class GameRunner:
    def __init__(
        self, state: GameState, store: JsonGameStore, *, allow_tight_routes: bool = False,
    ):
        state.validate()
        if type(allow_tight_routes) is not bool:
            raise TypeError("allow_tight_routes must be a bool")
        self.state = state
        self.store = store
        self.allow_tight_routes = allow_tight_routes

    def current_map(self) -> str:
        return _physical_map(self.state.physical_squares, self.state.physical_parking)

    @staticmethod
    def _confirmed_rook_offset(pending: PendingMove) -> RookOffset | None:
        """Rebuild the off-center rook state from confirmed checkpoint actions."""
        offset = None
        for action in pending.actions[:pending.next_action]:
            if isinstance(action, RookOffset):
                offset = action if action.direction == "OUT" else None
            elif isinstance(action, Transfer) and action.reason == "castle_rook_from_offset":
                offset = None
        return offset

    @staticmethod
    def _confirmed_piece_offsets(
        actions: tuple[Action, ...], next_action: int, geometry: RouteGeometry,
    ) -> dict[BoardSquare, tuple[float, float]]:
        """Reconstruct the measured positions of confirmed neighbor nudges."""
        offsets: dict[BoardSquare, tuple[float, float]] = {}
        for action in actions[:next_action]:
            if isinstance(action, PieceOffset):
                if action.direction == "OUT":
                    offsets[action.square] = offset_target_mm(action, geometry)
                else:
                    offsets.pop(action.square)
        return offsets

    def _route_transfer(
        self, action: Transfer, squares: dict[str, str], slots: list[str | None],
        geometry: RouteGeometry, *,
        offset_positions_mm: Mapping[BoardSquare, tuple[float, float]] | None = None,
        castle_clearance_override: bool = False,
    ) -> CarryRoute:
        """Route against the inventory at this transfer, with bounded fallback."""
        options = {
            "occupied_squares": _occupied_squares(squares),
            "occupied_parking_slots": _occupied_slots(slots),
            "geometry": geometry,
            "max_waypoints": MAX_CARRY_WAYPOINTS,
            "offset_positions_mm": offset_positions_mm,
        }
        try:
            return plan_transfer_route(action, **options)
        except (NoRouteError, RouteTooLongError):
            if castle_clearance_override:
                if not self.allow_tight_routes or action.reason != "castle_king":
                    raise
                # Only the selected last-resort castle may bypass circular
                # clearance on this carry. The offset rook is checked below
                # against its actual corner, independent of this route search.
                return plan_transfer_route(
                    action, ignore_piece_clearance=True, **options,
                )
            # Staged motions rely on measured clearance for every leg.
            if offset_positions_mm or not self.allow_tight_routes or action.reason in (
                KNIGHT_STAGE_REASONS | CLEARANCE_STAGE_REASONS | {
                    "castle_clearance_stage", "castle_clearance_restore",
                    "castle_king", "castle_rook", "castle_rook_from_offset",
                    "clearance_offset_move",
                }
            ):
                raise
            relaxed = plan_transfer_route(action, ignore_piece_clearance=True, **options)
            if route_has_tight_squeeze(relaxed, squares, slots, geometry):
                raise NoRouteError(
                    "tight carry needs a strict route or a complete offset bundle"
                )
            return relaxed

    @staticmethod
    def _route_rook_from_offset(
        action: Transfer, offset: RookOffset,
        squares: dict[str, str], slots: list[str | None], geometry: RouteGeometry,
        *, ignore_piece_clearance: bool = False,
    ) -> CarryRoute:
        if (action.reason != "castle_rook_from_offset" or
                action.source != offset.square or action.piece != offset.piece):
            raise GameStateError("offset-source carry does not match the parked rook")
        # The firmware's first loaded leg has an 18 mm minimum even if the
        # configured piece model is smaller. Use the same rounding guard as
        # the rook nudge and king's offset-rook clearance check.
        minimum = required_offset_clearance_mm(geometry)
        base_diameter = (geometry.moving_diameter_mm + geometry.occupied_diameter_mm) / 2
        offset_geometry = replace(geometry, margin_mm=minimum - base_diameter)
        options = {
            "occupied_squares": _occupied_squares(squares),
            "occupied_parking_slots": _occupied_slots(slots),
            "geometry": offset_geometry,
            "max_waypoints": MAX_CARRY_WAYPOINTS,
            "source_offset_mm": offset_target_mm(offset, geometry),
        }
        try:
            return plan_transfer_route(action, **options)
        except (NoRouteError, RouteTooLongError):
            if not ignore_piece_clearance:
                raise
            # The router still checks the CARRY_OFFSET first loaded leg and
            # compensated release against the firmware's 18 mm guard.
            return plan_transfer_route(
                action, ignore_piece_clearance=True, **options,
            )

    def preflight_move(self, uci: str, geometry: RouteGeometry) -> MovePlan:
        """Check *all* legs before starting a multi-transfer move."""
        if self.state.reset is not None:
            raise ReconciliationRequired("finish or reconcile the board reset before playing")
        if self.state.pending is not None:
            raise ReconciliationRequired("finish or reconcile the pending move first")
        board = self.state.board()

        def plan_with_selector(ignore_piece_clearance: bool) -> MovePlan:
            """Choose parking using the same clearance policy as route preflight."""
            selector = ReachableParkingSlotSelector(
                _occupied_squares(self.state.physical_squares),
                geometry=geometry, max_waypoints=MAX_CARRY_WAYPOINTS,
                ignore_piece_clearance=ignore_piece_clearance,
            )
            return plan_move(
                board, uci, self.state.committed_parking,
                promotion_policy=PromotionPolicy.PAWN_STAND_IN,
                slot_selector=selector,
            )

        def preflight_actions(logical_plan: MovePlan, *, strict_only: bool = False) -> MovePlan:
            squares = dict(self.state.physical_squares)
            slots = list(self.state.physical_parking)
            physical_actions: list[Action] = []
            active_offset: RookOffset | None = None
            piece_offsets: dict[BoardSquare, tuple[float, float]] = {}
            # Route each leg against the position left by its predecessor.
            for action in logical_plan.actions:
                if isinstance(action, RookOffset):
                    if action.direction == "OUT":
                        if active_offset is not None:
                            raise GameStateError("rook is already offset")
                        active_offset = action
                    elif (active_offset is None or
                          (action.piece, action.square, action.corner) !=
                          (active_offset.piece, active_offset.square, active_offset.corner)):
                        raise GameStateError("rook offset restoration does not match departure")
                    else:
                        active_offset = None
                    plan_rook_offset(action, squares, slots, geometry)
                    _apply_action(squares, slots, action)
                    physical_actions.append(action)
                    continue
                if isinstance(action, Transfer):
                    physical_piece = _piece_at(squares, slots, action.source)
                    if physical_piece is None:
                        raise GameStateError(f"planned transfer source is physically empty: {action.source}")
                    action = replace(action, piece=physical_piece)
                    stageable = (
                        action.reason in {"move", "capture", "promotion_stand_in"}
                        and isinstance(action.source, BoardSquare)
                    )
                    knight = (
                        physical_piece.upper() == "N" and action.reason == "move"
                        and isinstance(action.destination, BoardSquare)
                    )
                    direct = replace(action, reason="knight_clearance_move") if knight else action
                    if stageable:
                        try:
                            plan_transfer_route(
                                direct,
                                occupied_squares=_occupied_squares(squares),
                                occupied_parking_slots=_occupied_slots(slots),
                                geometry=geometry, max_waypoints=MAX_CARRY_WAYPOINTS,
                            )
                        except (NoRouteError, RouteTooLongError) as exc:
                            staged = (
                                plan_tight_knight_move(squares, slots, direct, geometry)
                                if knight else plan_cleared_transfer(
                                    squares, slots, direct, geometry,
                                )
                            )
                            if staged is None:
                                staged = plan_offset_cleared_transfer(
                                    direct, squares, slots, geometry,
                                )
                            if staged is not None:
                                candidates = staged
                            elif knight:
                                raise NoRouteError(
                                    f"knight {action.source.name} to {action.destination.name} "
                                    "has no strictly cleared stage-and-restore route"
                                ) from exc
                            else:
                                candidates = (action,)
                        else:
                            candidates = (direct,)
                    else:
                        candidates = (action,)
                else:
                    raise GameStateError("game runner cannot require a spare promotion piece")
                for candidate in candidates:
                    if isinstance(candidate, PieceOffset):
                        if active_offset is not None:
                            raise GameStateError("cannot combine castling and clearance offsets")
                        plan_offset(
                            candidate, squares, slots, geometry,
                            offset_positions_mm=piece_offsets,
                        )
                        if candidate.direction == "OUT":
                            piece_offsets[candidate.square] = offset_target_mm(candidate, geometry)
                        else:
                            piece_offsets.pop(candidate.square)
                        _apply_action(squares, slots, candidate)
                        physical_actions.append(candidate)
                        continue
                    if not isinstance(candidate, Transfer):
                        raise GameStateError("unsupported planned physical action")
                    if ((isinstance(candidate.source, BoardSquare) and
                         candidate.source in piece_offsets) or
                        (isinstance(candidate.destination, BoardSquare) and
                         candidate.destination in piece_offsets)):
                        raise GameStateError("cannot carry from or into an offset piece square")
                    if active_offset is not None and candidate.source == active_offset.square:
                        route = self._route_rook_from_offset(
                            candidate, active_offset, squares, slots, geometry,
                            ignore_piece_clearance=logical_plan.castle_clearance_override,
                        )
                        active_offset = None
                    else:
                        if candidate.reason == "castle_rook_from_offset":
                            raise GameStateError("offset-source carry has no confirmed rook offset")
                        if active_offset is not None and candidate.destination == active_offset.square:
                            raise GameStateError("cannot transfer into an offset rook square")
                        route = (
                            plan_transfer_route(
                                candidate,
                                occupied_squares=_occupied_squares(squares),
                                occupied_parking_slots=_occupied_slots(slots),
                                geometry=geometry, max_waypoints=MAX_CARRY_WAYPOINTS,
                                offset_positions_mm=piece_offsets,
                            ) if strict_only else
                            self._route_transfer(
                                candidate, squares, slots, geometry,
                                offset_positions_mm=piece_offsets,
                                castle_clearance_override=(
                                    logical_plan.castle_clearance_override and
                                    candidate.reason == "castle_king"
                                ),
                            )
                        )
                        if active_offset is not None:
                            require_route_clear_of_offset(route, active_offset, geometry)
                    _apply_action(squares, slots, candidate)
                    physical_actions.append(candidate)
            if active_offset is not None:
                raise GameStateError("planned move leaves a rook off center")
            if piece_offsets:
                raise GameStateError("planned move leaves a neighbor off center")
            resulting_board, expected_physical = _replay_physical(
                self.state.initial_fen, [*self.state.moves, logical_plan.uci]
            )
            if (resulting_board.fen() != logical_plan.fen_after or
                    squares != expected_physical):
                raise GameStateError("planned physical actions do not reach the legal position")
            return replace(
                logical_plan, actions=tuple(physical_actions),
                parking_after=ParkingInventory(tuple(slots)),
            )

        # Nudge the rook toward an outer-file corner within its own square.
        # Keep its bitmap square occupied and try the other corner on the same
        # file edge if the preferred direction lacks nominal clearance.
        try:
            move = board.parse_uci(uci)
        except ValueError:
            move = None  # plan_move supplies the usual IllegalMoveError.
        if move is not None and board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            kingside = board.is_kingside_castling(move)
            rook_square = BoardSquare(
                chess.square_name(chess.square(7 if kingside else 0, rank))
            )
            preferred_corner = (
                "NE" if kingside else "NW"
            ) if rank == 0 else ("SE" if kingside else "SW")
            alternate_corner = (
                "SE" if kingside else "SW"
            ) if rank == 0 else ("NE" if kingside else "NW")
            first_route_error: NoRouteError | RouteTooLongError | None = None
            logical_plan = plan_with_selector(False)
            king_carry, rook_carry = logical_plan.actions
            assert isinstance(king_carry, Transfer) and isinstance(rook_carry, Transfer)
            rook_piece = _piece_at(
                self.state.physical_squares, self.state.physical_parking,
                rook_square,
            )
            if rook_piece not in {"R", "r"}:
                raise GameStateError("castling rook is missing from its physical square")
            offset_rook_carry = replace(rook_carry, reason="castle_rook_from_offset")
            base_plans = tuple(
                replace(
                    logical_plan,
                    actions=(
                        RookOffset(rook_piece, rook_square, corner, "OUT", "castle_rook_out"),
                        king_carry, offset_rook_carry,
                    ),
                )
                for corner in (preferred_corner, alternate_corner)
            )
            # Minimize physical actions: try both three-action plans before
            # considering a nearby piece stage and restoration for either corner.
            for base_plan in base_plans:
                try:
                    return preflight_actions(base_plan)
                except (NoRouteError, RouteTooLongError) as exc:
                    if first_route_error is None:
                        first_route_error = exc

            # A piece beside the king's destination may close the rook's final
            # carry. Move it temporarily, then restore it.
            for base_plan in base_plans:
                home_rank = "2" if rank == 0 else "7"
                preferred_file = "g" if kingside else "c"
                nearby_files = ("f", "h") if kingside else ("b", "d")
                for file in (preferred_file, *nearby_files):
                    source_name = f"{file}{home_rank}"
                    piece = self.state.physical_squares.get(source_name)
                    if piece is None or piece.isupper() != board.turn:
                        continue
                    destinations = sorted(
                        (f"{dest_file}{dest_rank}"
                         for dest_rank in "3456" for dest_file in "abcdefgh"
                         if f"{dest_file}{dest_rank}" not in self.state.physical_squares),
                        key=lambda name: (
                            abs(ord(name[0]) - ord(file)) +
                            abs(int(name[1]) - int(home_rank)),
                            name[0] != file, name,
                        ),
                    )
                    for destination_name in destinations:
                        stage = Transfer(
                            piece, BoardSquare(source_name), BoardSquare(destination_name),
                            "castle_clearance_stage",
                        )
                        restore = Transfer(
                            piece, BoardSquare(destination_name), BoardSquare(source_name),
                            "castle_clearance_restore",
                        )
                        candidate_plan = replace(
                            base_plan, actions=(stage, *base_plan.actions, restore),
                        )
                        try:
                            return preflight_actions(candidate_plan)
                        except (NoRouteError, RouteTooLongError):
                            continue

            # Two nearby pieces can jointly close the rook's approach or
            # compensated release. Try a bounded pair after shorter castle
            # plans fail. A bishop or knight may occupy the usual pawn square,
            # so use the physical piece there rather than requiring a pawn.
            # Try one or two ranks inward, in the same file or one file inward,
            # then restore both pieces in reverse order after the rook carry.
            pawn_rank = "2" if rank == 0 else "7"
            staging_ranks = ("3", "4") if rank == 0 else ("6", "5")
            nearby_files = ("g", "f", "h") if kingside else ("c", "b", "d")
            nearby_sources = tuple(
                BoardSquare(f"{file}{pawn_rank}") for file in nearby_files
                if (piece := self.state.physical_squares.get(f"{file}{pawn_rank}"))
                is not None and piece.isupper() == board.turn
            )
            inward_file_step = -1 if kingside else 1

            def stage_destinations(source: BoardSquare) -> tuple[BoardSquare, ...]:
                inward_file = chr(ord(source.name[0]) + inward_file_step)
                files = (source.name[0], inward_file)
                return tuple(
                    BoardSquare(f"{file}{stage_rank}")
                    for stage_rank in staging_ranks for file in files
                    if "a" <= file <= "h" and
                    f"{file}{stage_rank}" not in self.state.physical_squares
                )

            for base_plan in base_plans:
                for first_index, first_source in enumerate(nearby_sources):
                    for second_source in nearby_sources[first_index + 1:]:
                        for first_destination in stage_destinations(first_source):
                            for second_destination in stage_destinations(second_source):
                                if first_destination == second_destination:
                                    continue
                                first_piece = self.state.physical_squares[first_source.name]
                                second_piece = self.state.physical_squares[second_source.name]
                                stages = (
                                    Transfer(first_piece, first_source, first_destination,
                                             "castle_clearance_stage"),
                                    Transfer(second_piece, second_source, second_destination,
                                             "castle_clearance_stage"),
                                )
                                restores = (
                                    Transfer(second_piece, second_destination, second_source,
                                             "castle_clearance_restore"),
                                    Transfer(first_piece, first_destination, first_source,
                                             "castle_clearance_restore"),
                                )
                                candidate_plan = replace(
                                    base_plan,
                                    actions=(*stages, *base_plan.actions, *restores),
                                )
                                try:
                                    return preflight_actions(candidate_plan)
                                except (NoRouteError, RouteTooLongError):
                                    continue
            if self.allow_tight_routes:
                # Fall back to the short offset-rook castle only after every
                # strict three-, five-, and seven-action candidate has failed.
                for base_plan in base_plans:
                    try:
                        return preflight_actions(replace(
                            base_plan, castle_clearance_override=True,
                        ))
                    except (NoRouteError, RouteTooLongError):
                        continue
            assert first_route_error is not None
            raise first_route_error

        selector_error: NoRouteError | RouteTooLongError | None = None
        try:
            logical_plan = plan_with_selector(False)
        except (NoRouteError, RouteTooLongError) as exc:
            selector_error = exc
            logical_plan = None

        if move is not None and board.is_capture(move):
            # The direct selector returns one slot. A different slot may have
            # a complete strict stage-and-restore plan, so check the whole
            # move against each vacancy before allowing a tight fallback.
            candidates = [logical_plan] if logical_plan is not None else []
            chosen = {
                action.destination.slot_id
                for plan in candidates for action in plan.actions
                if isinstance(action, Transfer) and action.reason == "capture"
                and isinstance(action.destination, ParkingSlot)
            }
            for slot_id, piece in enumerate(self.state.physical_parking):
                if piece is None and slot_id not in chosen:
                    candidates.append(plan_move(
                        board, uci, self.state.committed_parking,
                        promotion_policy=PromotionPolicy.PAWN_STAND_IN,
                        slot_selector=lambda _source, _piece, _slots, slot_id=slot_id:
                            ParkingSlot(slot_id),
                    ))
            first_route_error: NoRouteError | RouteTooLongError | None = None
            for candidate in candidates:
                try:
                    return preflight_actions(candidate, strict_only=True)
                except (NoRouteError, RouteTooLongError) as exc:
                    if first_route_error is None:
                        first_route_error = exc
            if not self.allow_tight_routes:
                error = first_route_error or selector_error
                assert error is not None
                raise error

        if logical_plan is None:
            # The direct slot selector can fail before temporary staging is
            # considered. An occupied-center-safe slot is the fallback draft.
            logical_plan = plan_with_selector(True)
        return preflight_actions(logical_plan)

    def prepare_move(
        self, uci: str, geometry: RouteGeometry,
        *, remote_receipt: RemoteMoveReceipt | None = None,
    ) -> MovePlan:
        plan = self.preflight_move(uci, geometry)
        if remote_receipt is not None:
            if (self.state.source_kind != "aina" or
                    self.state.source_game_id != remote_receipt.source_game_id or
                    self.state.host_game_id != remote_receipt.host_game_id or
                    len(self.state.moves) != remote_receipt.expected_ply or
                    self.state.board_fen != remote_receipt.expected_fen or
                    plan.uci != remote_receipt.uci or
                    remote_receipt.request_id in self.state.remote_receipts):
                raise GameStateError("remote move receipt differs from the current game")
        self.state.pending = PendingMove(
            uci=plan.uci, fen_after=plan.fen_after,
            parking_after=plan.parking_after, actions=plan.actions,
            castle_clearance_override=plan.castle_clearance_override,
            remote_request_id=remote_receipt.request_id if remote_receipt else None,
        )
        if remote_receipt is not None:
            self.state.remote_receipts[remote_receipt.request_id] = remote_receipt
        self.state.events.append({
            "event": "move_prepared", "uci": plan.uci,
            "castle_clearance_override": plan.castle_clearance_override,
        })
        self.store.save(self.state)
        return plan

    def preflight_reset(self, geometry: RouteGeometry) -> tuple[Transfer | PieceOffset, ...]:
        """Validate the checkpoint, then preflight the complete physical reset."""
        if self.state.pending is not None:
            raise ReconciliationRequired("finish or reconcile the pending move before resetting")
        if self.state.reset is not None:
            raise GameStateError("board reset is already prepared")
        if self.state.initial_fen != chess.Board().fen():
            raise GameStateError("board reset requires a standard-start game")
        return plan_reset(
            self.state.physical_squares, self.state.physical_parking,
            geometry, self._route_transfer,
            plan_clearance_bundle=plan_offset_cleared_transfer,
        )

    def prepare_reset(self, geometry: RouteGeometry) -> tuple[Transfer | PieceOffset, ...]:
        actions = self.preflight_reset(geometry)
        if not actions:
            self._finish_reset()
            return ()
        self.state.reset = PendingReset(actions)
        self.state.events.append({
            "event": "reset_prepared",
            "transfers": sum(isinstance(action, Transfer) for action in actions),
            "offsets": sum(isinstance(action, PieceOffset) for action in actions),
        })
        self.store.save(self.state)
        return actions

    def execute_pending(
        self, link: CarryLink, geometry: RouteGeometry,
        confirm: Callable[[Action], bool],
        approve_route: Callable[[Action, CarryRoute | OffsetRoute, tuple[str, ...]], bool] | None = None,
        *, confirmation_source: str = "operator",
    ) -> None:
        """Checkpoint before and after each carry; leave uncertain work pending."""
        if confirmation_source not in {"operator", "controller_done", "simulation"}:
            raise ValueError("invalid confirmation source")
        pending = self.state.pending
        if pending is None:
            raise GameStateError("no pending move")
        if any(isinstance(action, Transfer) and action.reason in {
            "castle_rook_park", "castle_rook_return",
        } for action in pending.actions):
            raise ReconciliationRequired(
                "saved parking-slot castling plan needs inspection and manual restoration before replanning"
            )
        if pending.actions and isinstance(pending.actions[0], Transfer) and pending.actions[0].reason == "castle_king":
            raise ReconciliationRequired(
                "saved king-first castling plan needs inspection and manual restoration before replanning"
            )
        if pending.phase != "ready":
            raise ReconciliationRequired("inspect and resolve the uncertain action first")
        if pending.castle_clearance_override and not self.allow_tight_routes:
            raise ReconciliationRequired(
                "saved clearance-override castle requires --allow-tight-routes before motion"
            )
        while pending.next_action < len(pending.actions):
            action = pending.actions[pending.next_action]
            if isinstance(action, (Transfer, RookOffset, PieceOffset)):
                piece_offsets = self._confirmed_piece_offsets(
                    pending.actions, pending.next_action, geometry,
                )
                if isinstance(action, Transfer):
                    active_offset = self._confirmed_rook_offset(pending)
                    if action.reason == "castle_rook_from_offset":
                        if active_offset is None:
                            raise GameStateError("offset-source carry has no confirmed rook offset")
                        route = self._route_rook_from_offset(
                            action, active_offset,
                            self.state.physical_squares, self.state.physical_parking,
                            geometry,
                            ignore_piece_clearance=pending.castle_clearance_override,
                        )
                        source_id, destination_id, point_chars = encode_carry(route)
                        wire = (source_id, active_offset.corner, destination_id, point_chars)
                        command = "CARRY_OFFSET"
                    else:
                        route = self._route_transfer(
                            action, self.state.physical_squares, self.state.physical_parking,
                            geometry, offset_positions_mm=piece_offsets,
                            castle_clearance_override=(
                                pending.castle_clearance_override and
                                action.reason == "castle_king"
                            ),
                        )
                        if active_offset is not None:
                            require_route_clear_of_offset(route, active_offset, geometry)
                        wire = encode_carry(route)
                        command = "CARRY"
                elif isinstance(action, RookOffset):
                    route = plan_rook_offset(
                        action, self.state.physical_squares, self.state.physical_parking,
                        geometry,
                    )
                    wire = _offset_wire(action)
                    command = "OFFSET"
                else:
                    route = plan_offset(
                        action, self.state.physical_squares, self.state.physical_parking,
                        geometry, offset_positions_mm=piece_offsets,
                    )
                    wire = _offset_wire(action)
                    command = "OFFSET"
                if approve_route is not None and not approve_route(action, route, wire):
                    raise MoveDeferred("route was not approved; no new motion command was sent")
                link.set_map(self.current_map())
                # Save uncertainty before motion: a disconnect after this point
                # must never be mistaken for a transfer that was not started.
                pending.phase = "in_flight"
                if pending.remote_request_id is not None:
                    self.state.remote_receipts[pending.remote_request_id].state = "in_flight"
                pending.active_route = wire
                self.state.events.append({
                    "event": "carry_started", "uci": pending.uci,
                    "action": pending.next_action, "command": command,
                    "wire": list(wire),
                    "clearance_override": route.clearance_ignored,
                })
                self.store.save(self.state)
                completion = _send_motion(link, action, wire)
                # DONE means the controller finished; the physical snapshot
                # advances only after the confirmation step below.
                pending.phase = "awaiting_confirmation"
                self.state.events.append({
                    "event": "carry_done", "uci": pending.uci,
                    "action": pending.next_action,
                    "command_id": getattr(completion, "command_id", None),
                })
                self.store.save(self.state)
            else:
                pending.phase = "awaiting_confirmation"
                self.state.events.append({
                    "event": "manual_replacement_requested", "uci": pending.uci,
                    "action": pending.next_action,
                })
                self.store.save(self.state)
            if not confirm(action):
                raise ReconciliationRequired("operator did not confirm physical action")
            _apply_action(self.state.physical_squares, self.state.physical_parking, action)
            self.state.events.append({
                "event": "action_confirmed", "uci": pending.uci,
                "action": pending.next_action,
                "confirmation_source": confirmation_source,
            })
            pending.next_action += 1
            pending.phase = "ready"
            if pending.remote_request_id is not None:
                self.state.remote_receipts[pending.remote_request_id].state = "prepared"
            pending.active_route = None
            self._commit_if_complete()
            self.store.save(self.state)

    def execute_reset(
        self, link: CarryLink, geometry: RouteGeometry,
        confirm: Callable[[Transfer | PieceOffset], bool],
        approve_route: Callable[[Transfer | PieceOffset, CarryRoute | OffsetRoute, tuple[str, ...]], bool] | None = None,
        *, confirmation_source: str = "operator",
    ) -> None:
        """Execute a saved return plan with a checkpoint at each physical action."""
        if confirmation_source not in {"operator", "controller_done", "simulation"}:
            raise ValueError("invalid confirmation source")
        pending = self.state.reset
        if pending is None:
            raise GameStateError("no pending board reset")
        if pending.phase != "ready":
            raise ReconciliationRequired("inspect and resolve the uncertain reset action first")
        while pending.next_action < len(pending.actions):
            action = pending.actions[pending.next_action]
            piece_offsets = self._confirmed_piece_offsets(
                pending.actions, pending.next_action, geometry,
            )
            if isinstance(action, Transfer):
                route = self._route_transfer(
                    action, self.state.physical_squares, self.state.physical_parking,
                    geometry, offset_positions_mm=piece_offsets,
                )
                wire = encode_carry(route)
                command = "CARRY"
            else:
                route = plan_offset(
                    action, self.state.physical_squares, self.state.physical_parking,
                    geometry, offset_positions_mm=piece_offsets,
                )
                wire = _offset_wire(action)
                command = "OFFSET"
            if approve_route is not None and not approve_route(action, route, wire):
                raise MoveDeferred("reset route was not approved; no new motion command was sent")
            link.set_map(self.current_map())
            # Keep reset recovery at the same uncertainty boundary as a move.
            pending.phase = "in_flight"
            pending.active_route = wire
            self.state.events.append({
                "event": "reset_carry_started", "action": pending.next_action,
                "command": command, "wire": list(wire),
                "clearance_override": route.clearance_ignored,
            })
            self.store.save(self.state)
            completion = _send_motion(link, action, wire)
            pending.phase = "awaiting_confirmation"
            self.state.events.append({
                "event": "reset_carry_done", "action": pending.next_action,
                "command_id": getattr(completion, "command_id", None),
            })
            self.store.save(self.state)
            if not confirm(action):
                raise ReconciliationRequired("operator did not confirm reset action")
            _apply_action(self.state.physical_squares, self.state.physical_parking, action)
            self.state.events.append({
                "event": "reset_action_confirmed", "action": pending.next_action,
                "confirmation_source": confirmation_source,
            })
            pending.next_action += 1
            pending.phase = "ready"
            pending.active_route = None
            if pending.next_action == len(pending.actions):
                self._finish_reset()
            else:
                self.store.save(self.state)

    def _resolve_inspected_action(
        self, pending: PendingMove | PendingReset, outcome: str,
    ) -> int:
        """Advance only a verified destination, retaining the original event index."""
        if outcome not in {"source", "destination"}:
            raise ValueError("outcome must be source or destination")
        action_index = pending.next_action
        if outcome == "destination":
            _apply_action(
                self.state.physical_squares, self.state.physical_parking,
                pending.actions[action_index],
            )
            pending.next_action += 1
        pending.phase = "ready"
        if isinstance(pending, PendingMove) and pending.remote_request_id is not None:
            self.state.remote_receipts[pending.remote_request_id].state = "prepared"
        pending.active_route = None
        return action_index

    def resolve_uncertain_reset(self, outcome: str) -> None:
        """Record an inspected source or destination result for the active reset carry."""
        pending = self.state.reset
        if pending is None or pending.phase == "ready":
            raise GameStateError("no uncertain reset transfer to resolve")
        action_index = self._resolve_inspected_action(pending, outcome)
        self.state.events.append({
            "event": "reset_action_reconciled", "action": action_index,
            "outcome": outcome,
        })
        if pending.next_action == len(pending.actions):
            self._finish_reset()
        else:
            self.store.save(self.state)

    def abort_reset_after_restore(self) -> None:
        """Discard a reset after restoring the pre-reset position and centers."""
        if self.state.reset is None:
            raise GameStateError("no pending board reset")
        self.state.physical_squares = self.state.expected_physical_squares()
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.reset = None
        self.state.events.append({"event": "reset_aborted_after_manual_restore"})
        self.store.save(self.state)

    def _finish_reset(self) -> None:
        if (self.state.physical_squares != _board_pieces(chess.Board()) or
                any(self.state.physical_parking)):
            raise GameStateError("reset cannot finish before every piece is home")
        ply_count = len(self.state.moves)
        self.state = GameState.new()
        self.state.events.append({"event": "board_reset_completed", "previous_plies": ply_count})
        self.store.save(self.state)

    def resolve_uncertain_action(self, outcome: str) -> None:
        """Record an operator-verified source or destination result.

        ``source`` means the action has not happened (or the operator restored
        it to that state). ``destination`` means it has fully happened. The
        caller must inspect the entire affected path and pieces first.
        """
        pending = self.state.pending
        if pending is None or pending.phase == "ready":
            raise GameStateError("no uncertain action to resolve")
        action_index = self._resolve_inspected_action(pending, outcome)
        self.state.events.append({
            "event": "action_reconciled", "uci": pending.uci,
            "action": action_index,
            "outcome": outcome,
        })
        self._commit_if_complete()
        self.store.save(self.state)

    def abort_pending_after_restore(self) -> None:
        """Discard a move after restoring all pieces to their pre-move positions.

        Every temporarily offset piece must also be returned to its center.
        """
        if self.state.pending is None:
            raise GameStateError("no pending move")
        uci = self.state.pending.uci
        if self.state.pending.remote_request_id is not None:
            self.state.remote_receipts[self.state.pending.remote_request_id].state = "aborted"
        self.state.physical_squares = self.state.expected_physical_squares()
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.pending = None
        self.state.events.append({"event": "move_aborted_after_manual_restore", "uci": uci})
        self.store.save(self.state)

    def cancel_unstarted_remote_move(self) -> bool:
        """Tombstone a remote move only when no physical action has begun."""
        pending = self.state.pending
        if (pending is None or pending.remote_request_id is None or
                pending.next_action != 0 or pending.phase != "ready"):
            return False
        self.state.remote_receipts[pending.remote_request_id].state = "aborted"
        self.state.events.append({
            "event": "remote_move_cancelled_before_motion",
            "uci": pending.uci,
            "request_id": pending.remote_request_id,
        })
        self.state.pending = None
        self.store.save(self.state)
        return True

    def _commit_if_complete(self) -> None:
        pending = self.state.pending
        if pending is None or pending.next_action < len(pending.actions):
            return
        resulting_board, expected_physical = _replay_physical(
            self.state.initial_fen, [*self.state.moves, pending.uci]
        )
        if (resulting_board.fen() != pending.fen_after or
                self.state.physical_squares != expected_physical or
                tuple(self.state.physical_parking) != pending.parking_after.slots):
            raise GameStateError("confirmed actions do not match planned final position")
        self.state.board_fen = pending.fen_after
        self.state.committed_parking = pending.parking_after
        self.state.moves.append(pending.uci)
        if pending.remote_request_id is not None:
            self.state.remote_receipts[pending.remote_request_id].state = "committed"
        self.state.events.append({"event": "move_committed", "uci": pending.uci})
        self.state.pending = None

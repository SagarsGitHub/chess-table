"""Checkpointed physical execution of legal chess moves.

The committed chess position changes only after every transfer in a move has
been accepted following controller completion or operator confirmation. A
separate physical snapshot represents the temporary position between the
victim and attacker legs of a capture.
"""

from __future__ import annotations

from typing import Callable, Protocol

import chess

from .checkpoint import (
    PROMOTION_RULE, STATE_VERSION, GameState, JsonGameStore, PendingMove, PendingReset,
    RemoteMoveReceipt,
)
from .corner_offset import (
    OffsetRoute, plan_offset, plan_rook_offset, require_route_clear_of_offset,
)
from .game_errors import GameStateError, MoveDeferred, ReconciliationRequired
from .physical import _apply_action, _physical_map
from .planner import Action, MovePlan, PieceOffset, RookOffset, Transfer
from .routing import CarryRoute, RouteGeometry
from .wire import encode_carry
from .game_preflight import GamePreflightMixin
from .game_recovery import GameRecoveryMixin


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


class GameRunner(GamePreflightMixin, GameRecoveryMixin):
    """Own state and persistence; execute plans through one caller-owned link."""

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

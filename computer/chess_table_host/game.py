"""Checkpointed physical execution of legal chess moves.

The committed chess position changes only after every transfer in a move has
been accepted following controller completion or operator confirmation. A
separate physical snapshot represents the temporary position between the
victim and attacker legs of a capture.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Protocol

import chess

from .checkpoint import (
    PROMOTION_RULE, STATE_VERSION, GameState, JsonGameStore, PendingMove, PendingReset,
)
from .game_errors import GameStateError
from .physical import (
    _apply_action, _board_pieces, _occupied_slots, _occupied_squares,
    _physical_map, _piece_at, _replay_physical,
)
from .planner import (
    Action,
    MovePlan,
    ParkingInventory,
    PromotionPolicy,
    Transfer,
    plan_move,
)
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

    def _route_transfer(
        self, action: Transfer, squares: dict[str, str], slots: list[str | None],
        geometry: RouteGeometry,
    ) -> CarryRoute:
        """Route against the inventory at this transfer, with bounded fallback."""
        options = {
            "occupied_squares": _occupied_squares(squares),
            "occupied_parking_slots": _occupied_slots(slots),
            "geometry": geometry,
            "max_waypoints": 12,
        }
        try:
            return plan_transfer_route(action, **options)
        except (NoRouteError, RouteTooLongError):
            if not self.allow_tight_routes:
                raise
            return plan_transfer_route(action, ignore_piece_clearance=True, **options)

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
                geometry=geometry, max_waypoints=12,
                ignore_piece_clearance=ignore_piece_clearance,
            )
            return plan_move(
                board, uci, self.state.committed_parking,
                promotion_policy=PromotionPolicy.PAWN_STAND_IN,
                slot_selector=selector,
            )

        try:
            logical_plan = plan_with_selector(False)
        except (NoRouteError, RouteTooLongError):
            if not self.allow_tight_routes:
                raise
            logical_plan = plan_with_selector(True)
        squares = dict(self.state.physical_squares)
        slots = list(self.state.physical_parking)
        physical_actions: list[Action] = []
        # Captures, castling, and promotions can require several carries. Route
        # each leg against the position left by the preceding simulated leg.
        for action in logical_plan.actions:
            if isinstance(action, Transfer):
                physical_piece = _piece_at(squares, slots, action.source)
                if physical_piece is None:
                    raise GameStateError(f"planned transfer source is physically empty: {action.source}")
                action = replace(action, piece=physical_piece)
                self._route_transfer(action, squares, slots, geometry)
            else:
                raise GameStateError("game runner cannot require a spare promotion piece")
            _apply_action(squares, slots, action)
            physical_actions.append(action)
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

    def prepare_move(self, uci: str, geometry: RouteGeometry) -> MovePlan:
        plan = self.preflight_move(uci, geometry)
        self.state.pending = PendingMove(
            uci=plan.uci, fen_after=plan.fen_after,
            parking_after=plan.parking_after, actions=plan.actions,
        )
        self.state.events.append({"event": "move_prepared", "uci": plan.uci})
        self.store.save(self.state)
        return plan

    def preflight_reset(self, geometry: RouteGeometry) -> tuple[Transfer, ...]:
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
        )

    def prepare_reset(self, geometry: RouteGeometry) -> tuple[Transfer, ...]:
        actions = self.preflight_reset(geometry)
        if not actions:
            self._finish_reset()
            return ()
        self.state.reset = PendingReset(actions)
        self.state.events.append({"event": "reset_prepared", "transfers": len(actions)})
        self.store.save(self.state)
        return actions

    def execute_pending(
        self, link: CarryLink, geometry: RouteGeometry,
        confirm: Callable[[Action], bool],
        approve_route: Callable[[Transfer, CarryRoute, tuple[str, str, str]], bool] | None = None,
        *, confirmation_source: str = "operator",
    ) -> None:
        """Checkpoint before and after each carry; leave uncertain work pending."""
        if confirmation_source not in {"operator", "controller_done", "simulation"}:
            raise ValueError("invalid confirmation source")
        pending = self.state.pending
        if pending is None:
            raise GameStateError("no pending move")
        if pending.phase != "ready":
            raise ReconciliationRequired("inspect and resolve the uncertain action first")
        while pending.next_action < len(pending.actions):
            action = pending.actions[pending.next_action]
            if isinstance(action, Transfer):
                route = self._route_transfer(
                    action, self.state.physical_squares, self.state.physical_parking,
                    geometry,
                )
                wire = encode_carry(route)
                if approve_route is not None and not approve_route(action, route, wire):
                    raise MoveDeferred("route was not approved; no new CARRY was sent")
                link.set_map(self.current_map())
                # Save uncertainty before CARRY: a disconnect after this point
                # must never be mistaken for a transfer that was not started.
                pending.phase = "in_flight"
                pending.active_route = wire
                self.state.events.append({
                    "event": "carry_started", "uci": pending.uci,
                    "action": pending.next_action, "wire": list(wire),
                    "clearance_override": route.clearance_ignored,
                })
                self.store.save(self.state)
                completion = link.carry(*wire)
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
            pending.active_route = None
            self._commit_if_complete()
            self.store.save(self.state)

    def execute_reset(
        self, link: CarryLink, geometry: RouteGeometry,
        confirm: Callable[[Transfer], bool],
        approve_route: Callable[[Transfer, CarryRoute, tuple[str, str, str]], bool] | None = None,
        *, confirmation_source: str = "operator",
    ) -> None:
        """Execute a saved return plan with a checkpoint at each transfer."""
        if confirmation_source not in {"operator", "controller_done", "simulation"}:
            raise ValueError("invalid confirmation source")
        pending = self.state.reset
        if pending is None:
            raise GameStateError("no pending board reset")
        if pending.phase != "ready":
            raise ReconciliationRequired("inspect and resolve the uncertain reset transfer first")
        while pending.next_action < len(pending.actions):
            action = pending.actions[pending.next_action]
            route = self._route_transfer(
                action, self.state.physical_squares, self.state.physical_parking,
                geometry,
            )
            wire = encode_carry(route)
            if approve_route is not None and not approve_route(action, route, wire):
                raise MoveDeferred("reset route was not approved; no new CARRY was sent")
            link.set_map(self.current_map())
            # Keep reset recovery at the same uncertainty boundary as a move.
            pending.phase = "in_flight"
            pending.active_route = wire
            self.state.events.append({
                "event": "reset_carry_started", "action": pending.next_action,
                "wire": list(wire), "clearance_override": route.clearance_ignored,
            })
            self.store.save(self.state)
            completion = link.carry(*wire)
            pending.phase = "awaiting_confirmation"
            self.state.events.append({
                "event": "reset_carry_done", "action": pending.next_action,
                "command_id": getattr(completion, "command_id", None),
            })
            self.store.save(self.state)
            if not confirm(action):
                raise ReconciliationRequired("operator did not confirm reset transfer")
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

    def resolve_uncertain_reset(self, outcome: str) -> None:
        """Record an inspected source or destination result for the active reset carry."""
        pending = self.state.reset
        if pending is None or pending.phase == "ready":
            raise GameStateError("no uncertain reset transfer to resolve")
        if outcome not in {"source", "destination"}:
            raise ValueError("outcome must be source or destination")
        action = pending.actions[pending.next_action]
        if outcome == "destination":
            _apply_action(self.state.physical_squares, self.state.physical_parking, action)
            pending.next_action += 1
        self.state.events.append({
            "event": "reset_action_reconciled", "action": (
                pending.next_action if outcome == "source" else pending.next_action - 1
            ), "outcome": outcome,
        })
        pending.phase = "ready"
        pending.active_route = None
        if pending.next_action == len(pending.actions):
            self._finish_reset()
        else:
            self.store.save(self.state)

    def abort_reset_after_restore(self) -> None:
        """Discard a reset only after manually restoring the pre-reset position."""
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
        if outcome not in {"source", "destination"}:
            raise ValueError("outcome must be source or destination")
        action = pending.actions[pending.next_action]
        if outcome == "destination":
            _apply_action(self.state.physical_squares, self.state.physical_parking, action)
            pending.next_action += 1
        self.state.events.append({
            "event": "action_reconciled", "uci": pending.uci,
            "action": pending.next_action if outcome == "source" else pending.next_action - 1,
            "outcome": outcome,
        })
        pending.phase = "ready"
        pending.active_route = None
        self._commit_if_complete()
        self.store.save(self.state)

    def abort_pending_after_restore(self) -> None:
        """Discard a pending move only after manual restoration of pre-move state."""
        if self.state.pending is None:
            raise GameStateError("no pending move")
        uci = self.state.pending.uci
        self.state.physical_squares = self.state.expected_physical_squares()
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.pending = None
        self.state.events.append({"event": "move_aborted_after_manual_restore", "uci": uci})
        self.store.save(self.state)

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
        self.state.events.append({"event": "move_committed", "uci": pending.uci})
        self.state.pending = None

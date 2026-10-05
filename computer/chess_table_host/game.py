"""Checkpointed physical execution of legal chess moves.

The committed chess position changes only after every transfer in a move has
been confirmed by an operator.  A separate physical snapshot represents the
temporary position between the victim and attacker legs of a capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
import json
import os

import chess

from .planner import (
    Action,
    BoardSquare,
    ManualReplacement,
    MovePlan,
    ParkingInventory,
    ParkingSlot,
    Transfer,
    plan_move,
)
from .routing import CarryRoute, ReachableParkingSlotSelector, RouteGeometry, plan_transfer_route
from .wire import encode_carry, encode_map


STATE_VERSION = 2


class GameStateError(ValueError):
    """Saved game state is invalid or inconsistent."""


class ReconciliationRequired(GameStateError):
    """A physical action has an unknown result and must be inspected."""


class MoveDeferred(GameStateError):
    """The operator paused before a physical action began."""


class CarryLink(Protocol):
    def set_map(self, occupancy_hex: str): ...
    def carry(self, source_id: str, destination_id: str, point_chars: str): ...


def _board_pieces(board: chess.Board) -> dict[str, str]:
    return {chess.square_name(square): piece.symbol() for square, piece in board.piece_map().items()}


def _location_to_json(location: BoardSquare | ParkingSlot) -> dict[str, object]:
    if isinstance(location, BoardSquare):
        return {"square": location.name}
    return {"slot": location.slot_id}


def _location_from_json(value: object) -> BoardSquare | ParkingSlot:
    if not isinstance(value, dict) or len(value) != 1:
        raise GameStateError("invalid saved location")
    if "square" in value:
        return BoardSquare(value["square"])
    if "slot" in value:
        return ParkingSlot(value["slot"])
    raise GameStateError("invalid saved location")


def _action_to_json(action: Action) -> dict[str, object]:
    if isinstance(action, Transfer):
        return {
            "type": "transfer", "piece": action.piece,
            "source": _location_to_json(action.source),
            "destination": _location_to_json(action.destination),
            "reason": action.reason,
        }
    return {
        "type": "manual_replacement", "square": action.square.name,
        "pawn": action.pawn, "replacement": action.replacement,
    }


def _action_from_json(value: object) -> Action:
    if not isinstance(value, dict):
        raise GameStateError("invalid saved action")
    if value.get("type") == "transfer":
        return Transfer(
            value["piece"], _location_from_json(value["source"]),
            _location_from_json(value["destination"]), value["reason"],
        )
    if value.get("type") == "manual_replacement":
        return ManualReplacement(
            BoardSquare(value["square"]), value["pawn"], value["replacement"]
        )
    raise GameStateError("unknown saved action type")


def _piece_at(
    squares: dict[str, str], slots: list[str | None], location: BoardSquare | ParkingSlot
) -> str | None:
    return squares.get(location.name) if isinstance(location, BoardSquare) else slots[location.slot_id]


def _put_piece(
    squares: dict[str, str], slots: list[str | None],
    location: BoardSquare | ParkingSlot, piece: str | None,
) -> None:
    if isinstance(location, BoardSquare):
        if piece is None:
            squares.pop(location.name, None)
        else:
            squares[location.name] = piece
    else:
        slots[location.slot_id] = piece


def _apply_action(
    squares: dict[str, str], slots: list[str | None], action: Action
) -> None:
    if isinstance(action, Transfer):
        if _piece_at(squares, slots, action.source) != action.piece:
            raise GameStateError(f"source does not contain {action.piece}: {action.source}")
        if _piece_at(squares, slots, action.destination) is not None:
            raise GameStateError(f"destination is occupied: {action.destination}")
        _put_piece(squares, slots, action.source, None)
        _put_piece(squares, slots, action.destination, action.piece)
    else:
        if squares.get(action.square.name) != action.pawn:
            raise GameStateError(f"promotion square does not contain {action.pawn}")
        squares[action.square.name] = action.replacement


def _occupied_squares(squares: dict[str, str]) -> set[BoardSquare]:
    return {BoardSquare(name) for name in squares}


def _occupied_slots(slots: list[str | None]) -> set[ParkingSlot]:
    return {ParkingSlot(i) for i, piece in enumerate(slots) if piece is not None}


def _physical_map(squares: dict[str, str], slots: list[str | None]) -> str:
    # encode_map inspects piece_map only; this temporary board need not be a
    # legal chess position while a multi-transfer move is in progress.
    board = chess.Board(None)
    for name, symbol in squares.items():
        board.set_piece_at(chess.parse_square(name), chess.Piece.from_symbol(symbol))
    return encode_map(board, ParkingInventory(tuple(slots)))


@dataclass
class PendingMove:
    uci: str
    fen_after: str
    parking_after: ParkingInventory
    actions: tuple[Action, ...]
    next_action: int = 0
    phase: str = "ready"  # ready, in_flight, awaiting_confirmation
    active_route: tuple[str, str, str] | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "uci": self.uci, "fen_after": self.fen_after,
            "parking_after": list(self.parking_after.slots),
            "actions": [_action_to_json(action) for action in self.actions],
            "next_action": self.next_action, "phase": self.phase,
            "active_route": list(self.active_route) if self.active_route else None,
        }

    @classmethod
    def from_json(cls, value: object) -> PendingMove:
        if not isinstance(value, dict):
            raise GameStateError("invalid pending move")
        route = value.get("active_route")
        if route is not None and (not isinstance(route, list) or len(route) != 3 or
                                  any(not isinstance(token, str) for token in route)):
            raise GameStateError("invalid active route")
        return cls(
            uci=value["uci"], fen_after=value["fen_after"],
            parking_after=ParkingInventory(tuple(value["parking_after"])),
            actions=tuple(_action_from_json(item) for item in value["actions"]),
            next_action=value["next_action"], phase=value["phase"],
            active_route=tuple(route) if route is not None else None,
        )


@dataclass
class GameState:
    board_fen: str
    committed_parking: ParkingInventory
    physical_squares: dict[str, str]
    physical_parking: list[str | None]
    moves: list[str] = field(default_factory=list)
    pending: PendingMove | None = None
    events: list[dict[str, object]] = field(default_factory=list)
    initial_fen: str | None = None

    def __post_init__(self) -> None:
        if self.initial_fen is None:
            self.initial_fen = self.board_fen

    @classmethod
    def new(cls) -> GameState:
        board = chess.Board()
        return cls(board.fen(), ParkingInventory.empty(), _board_pieces(board), [None] * 32)

    def board(self) -> chess.Board:
        """Rebuild the legal move stack, including repetition history."""
        try:
            board = chess.Board(self.initial_fen)
            for uci in self.moves:
                board.push(board.parse_uci(uci))
        except (TypeError, ValueError) as exc:
            raise GameStateError(f"invalid saved move history: {exc}") from exc
        if board.fen() != self.board_fen:
            raise GameStateError("saved FEN differs from replayed move history")
        return board

    def validate(self) -> None:
        try:
            board = self.board()
            ParkingInventory(tuple(self.physical_parking))
            for name, symbol in self.physical_squares.items():
                BoardSquare(name)
                chess.Piece.from_symbol(symbol)
        except (ValueError, TypeError) as exc:
            raise GameStateError(f"invalid saved physical position: {exc}") from exc
        if not board.is_valid():
            raise GameStateError("committed chess position is invalid")
        if not isinstance(self.moves, list) or any(not isinstance(move, str) for move in self.moves):
            raise GameStateError("invalid move history")
        if self.pending is None:
            if (self.physical_squares != _board_pieces(board) or
                    tuple(self.physical_parking) != self.committed_parking.slots):
                raise GameStateError("physical snapshot differs from committed game")
            return
        pending = self.pending
        if (not isinstance(pending.next_action, int) or
                not 0 <= pending.next_action <= len(pending.actions) or
                pending.phase not in {"ready", "in_flight", "awaiting_confirmation"}):
            raise GameStateError("invalid pending action cursor")
        if pending.next_action == len(pending.actions):
            raise GameStateError("completed move was not committed")
        if (pending.phase == "ready") != (pending.active_route is None):
            # Manual replacement has no wire route but still awaits confirmation.
            if not (pending.phase == "awaiting_confirmation" and
                    isinstance(pending.actions[pending.next_action], ManualReplacement) and
                    pending.active_route is None):
                raise GameStateError("inconsistent pending phase and route")
        try:
            move = board.parse_uci(pending.uci)
            expected = board.copy()
            expected.push(move)
        except ValueError as exc:
            raise GameStateError("pending UCI is illegal") from exc
        if expected.fen() != pending.fen_after:
            raise GameStateError("pending resulting FEN does not match legal move")
        squares = _board_pieces(board)
        slots = list(self.committed_parking.slots)
        for action in pending.actions[:pending.next_action]:
            _apply_action(squares, slots, action)
        if squares != self.physical_squares or slots != self.physical_parking:
            raise GameStateError("physical checkpoint differs from confirmed action prefix")

    def to_json(self) -> dict[str, object]:
        self.validate()
        return {
            "version": STATE_VERSION,
            "initial_fen": self.initial_fen,
            "board_fen": self.board_fen,
            "committed_parking": list(self.committed_parking.slots),
            "physical_squares": dict(sorted(self.physical_squares.items())),
            "physical_parking": self.physical_parking,
            "moves": self.moves,
            "pending": self.pending.to_json() if self.pending else None,
            "events": self.events,
        }

    @classmethod
    def from_json(cls, value: object) -> GameState:
        if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
            raise GameStateError("unsupported game-state version")
        try:
            state = cls(
                board_fen=value["board_fen"],
                committed_parking=ParkingInventory(tuple(value["committed_parking"])),
                physical_squares=dict(value["physical_squares"]),
                physical_parking=list(value["physical_parking"]),
                moves=list(value["moves"]),
                pending=PendingMove.from_json(value["pending"]) if value["pending"] else None,
                events=list(value.get("events", [])),
                initial_fen=value["initial_fen"],
            )
            state.validate()
            return state
        except (KeyError, TypeError, ValueError) as exc:
            raise GameStateError(f"invalid game-state file: {exc}") from exc


class JsonGameStore:
    """Atomic checkpoint file; every confirmed physical action is persisted."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> GameState:
        with self.path.open("r", encoding="utf-8") as handle:
            return GameState.from_json(json.load(handle))

    def save(self, state: GameState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)


class GameRunner:
    def __init__(self, state: GameState, store: JsonGameStore):
        state.validate()
        self.state = state
        self.store = store

    def current_map(self) -> str:
        return _physical_map(self.state.physical_squares, self.state.physical_parking)

    def preflight_move(self, uci: str, geometry: RouteGeometry) -> MovePlan:
        """Check *all* legs before starting a multi-transfer move."""
        if self.state.pending is not None:
            raise ReconciliationRequired("finish or reconcile the pending move first")
        board = self.state.board()
        selector = ReachableParkingSlotSelector(
            _occupied_squares(self.state.physical_squares),
            geometry=geometry, max_waypoints=12,
        )
        plan = plan_move(
            board, uci, self.state.committed_parking, slot_selector=selector
        )
        squares = dict(self.state.physical_squares)
        slots = list(self.state.physical_parking)
        for action in plan.actions:
            if isinstance(action, Transfer):
                plan_transfer_route(
                    action, occupied_squares=_occupied_squares(squares),
                    occupied_parking_slots=_occupied_slots(slots),
                    geometry=geometry, max_waypoints=12,
                )
            _apply_action(squares, slots, action)
        if (squares != _board_pieces(chess.Board(plan.fen_after)) or
                tuple(slots) != plan.parking_after.slots):
            raise GameStateError("planned physical actions do not reach the legal position")
        return plan

    def prepare_move(self, uci: str, geometry: RouteGeometry) -> MovePlan:
        plan = self.preflight_move(uci, geometry)
        self.state.pending = PendingMove(
            uci=plan.uci, fen_after=plan.fen_after,
            parking_after=plan.parking_after, actions=plan.actions,
        )
        self.state.events.append({"event": "move_prepared", "uci": plan.uci})
        self.store.save(self.state)
        return plan

    def execute_pending(
        self, link: CarryLink, geometry: RouteGeometry,
        confirm: Callable[[Action], bool],
        approve_route: Callable[[Transfer, CarryRoute, tuple[str, str, str]], bool] | None = None,
    ) -> None:
        pending = self.state.pending
        if pending is None:
            raise GameStateError("no pending move")
        if pending.phase != "ready":
            raise ReconciliationRequired("inspect and resolve the uncertain action first")
        while pending.next_action < len(pending.actions):
            action = pending.actions[pending.next_action]
            if isinstance(action, Transfer):
                route = plan_transfer_route(
                    action,
                    occupied_squares=_occupied_squares(self.state.physical_squares),
                    occupied_parking_slots=_occupied_slots(self.state.physical_parking),
                    geometry=geometry, max_waypoints=12,
                )
                wire = encode_carry(route)
                if approve_route is not None and not approve_route(action, route, wire):
                    raise MoveDeferred("route was not approved; no new CARRY was sent")
                link.set_map(self.current_map())
                pending.phase = "in_flight"
                pending.active_route = wire
                self.state.events.append({
                    "event": "carry_started", "uci": pending.uci,
                    "action": pending.next_action, "wire": list(wire),
                })
                self.store.save(self.state)
                completion = link.carry(*wire)
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
            })
            pending.next_action += 1
            pending.phase = "ready"
            pending.active_route = None
            self._commit_if_complete()
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
        self.state.physical_squares = _board_pieces(chess.Board(self.state.board_fen))
        self.state.physical_parking = list(self.state.committed_parking.slots)
        self.state.pending = None
        self.state.events.append({"event": "move_aborted_after_manual_restore", "uci": uci})
        self.store.save(self.state)

    def _commit_if_complete(self) -> None:
        pending = self.state.pending
        if pending is None or pending.next_action < len(pending.actions):
            return
        if (self.state.physical_squares != _board_pieces(chess.Board(pending.fen_after)) or
                tuple(self.state.physical_parking) != pending.parking_after.slots):
            raise GameStateError("confirmed actions do not match planned final position")
        self.state.board_fen = pending.fen_after
        self.state.committed_parking = pending.parking_after
        self.state.moves.append(pending.uci)
        self.state.events.append({"event": "move_committed", "uci": pending.uci})
        self.state.pending = None

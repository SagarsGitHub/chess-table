"""Checkpointed physical execution of legal chess moves.

The committed chess position changes only after every transfer in a move has
been accepted following controller completion or operator confirmation. A
separate physical snapshot represents the temporary position between the
victim and attacker legs of a capture.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
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
    PromotionPolicy,
    Transfer,
    plan_move,
)
from .routing import (
    CarryRoute, NoRouteError, ReachableParkingSlotSelector, RouteError,
    RouteGeometry, RouteTooLongError, location_point, plan_transfer_route,
)
from .wire import encode_carry, encode_map


STATE_VERSION = 4
PROMOTION_RULE = "pawn_stand_in"


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


def _replay_physical(initial_fen: str, moves: list[str]) -> tuple[chess.Board, dict[str, str]]:
    """Replay legal moves while keeping a promoted pawn's physical identity."""
    try:
        board = chess.Board(initial_fen)
        physical = _board_pieces(board)
        for uci in moves:
            move = board.parse_uci(uci)
            source = chess.square_name(move.from_square)
            destination = chess.square_name(move.to_square)
            if board.is_capture(move):
                victim_square = move.to_square
                if board.is_en_passant(move):
                    victim_square += -8 if board.turn == chess.WHITE else 8
                victim = physical.pop(chess.square_name(victim_square), None)
                if victim is None:
                    raise GameStateError(f"physical capture victim missing for {uci}")
            mover = physical.pop(source, None)
            if mover is None:
                raise GameStateError(f"physical mover missing for {uci}")
            if board.is_castling(move):
                rank = chess.square_rank(move.from_square)
                rook_from = chess.square(7 if board.is_kingside_castling(move) else 0, rank)
                rook_to = chess.square(5 if board.is_kingside_castling(move) else 3, rank)
                rook = physical.pop(chess.square_name(rook_from), None)
                if rook is None:
                    raise GameStateError(f"physical castling rook missing for {uci}")
                physical[chess.square_name(rook_to)] = rook
            # On promotion, the pawn moves to the final rank unchanged. The
            # logical board below records the selected Q/R/B/N role.
            physical[destination] = mover
            board.push(move)
    except GameStateError:
        raise
    except (TypeError, ValueError) as exc:
        raise GameStateError(f"invalid saved move history: {exc}") from exc
    return board, physical


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


def _parking_approaches(slot: ParkingSlot) -> tuple[str, ...]:
    """Board centers through which a parked piece can enter or leave its slot."""
    point = location_point(slot)
    return tuple(
        f"{chr(ord('a') + (point.x + dx - 1) // 2)}{(point.y + dy + 1) // 2}"
        for dx in (-1, 1) for dy in (-1, 1)
        if 1 <= point.x + dx <= 15 and 1 <= point.y + dy <= 15
    )


def _accessible_parking(slots: list[str | None], squares: dict[str, str]) -> set[int]:
    return {
        index for index, piece in enumerate(slots)
        if piece is not None and any(
            square not in squares for square in _parking_approaches(ParkingSlot(index))
        )
    }


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
class PendingReset:
    actions: tuple[Transfer, ...]
    next_action: int = 0
    phase: str = "ready"  # ready, in_flight, awaiting_confirmation
    active_route: tuple[str, str, str] | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "actions": [_action_to_json(action) for action in self.actions],
            "next_action": self.next_action,
            "phase": self.phase,
            "active_route": list(self.active_route) if self.active_route else None,
        }

    @classmethod
    def from_json(cls, value: object) -> PendingReset:
        if not isinstance(value, dict):
            raise GameStateError("invalid pending reset")
        route = value.get("active_route")
        if route is not None and (not isinstance(route, list) or len(route) != 3 or
                                  any(not isinstance(token, str) for token in route)):
            raise GameStateError("invalid reset active route")
        actions = tuple(_action_from_json(item) for item in value["actions"])
        if not actions or any(not isinstance(action, Transfer) for action in actions):
            raise GameStateError("pending reset requires transfer actions")
        return cls(
            actions=actions, next_action=value["next_action"],
            phase=value["phase"],
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
    reset: PendingReset | None = None
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
        board, _ = _replay_physical(self.initial_fen, self.moves)
        if board.fen() != self.board_fen:
            raise GameStateError("saved FEN differs from replayed move history")
        return board

    def expected_physical_squares(self) -> dict[str, str]:
        board, physical = _replay_physical(self.initial_fen, self.moves)
        if board.fen() != self.board_fen:
            raise GameStateError("saved FEN differs from replayed move history")
        return physical

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
        if self.reset is not None:
            if self.pending is not None:
                raise GameStateError("move and board reset cannot both be pending")
            if self.initial_fen != chess.Board().fen():
                raise GameStateError("board reset requires a standard-start game")
            reset = self.reset
            if (not reset.actions or any(not isinstance(action, Transfer)
                                         for action in reset.actions)):
                raise GameStateError("pending reset requires transfer actions")
            if (type(reset.next_action) is not int or
                    not 0 <= reset.next_action < len(reset.actions) or
                    reset.phase not in {"ready", "in_flight", "awaiting_confirmation"}):
                raise GameStateError("invalid reset action cursor")
            if (reset.phase == "ready") != (reset.active_route is None):
                raise GameStateError("inconsistent reset phase and route")
            squares = self.expected_physical_squares()
            slots = list(self.committed_parking.slots)
            for action in reset.actions[:reset.next_action]:
                _apply_action(squares, slots, action)
            if squares != self.physical_squares or slots != self.physical_parking:
                raise GameStateError("physical checkpoint differs from confirmed reset prefix")
            for action in reset.actions[reset.next_action:]:
                _apply_action(squares, slots, action)
            if squares != _board_pieces(chess.Board()) or any(slots):
                raise GameStateError("reset actions do not restore the standard position")
            return
        if self.pending is None:
            if (self.physical_squares != self.expected_physical_squares() or
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
        squares = self.expected_physical_squares()
        slots = list(self.committed_parking.slots)
        for action in pending.actions[:pending.next_action]:
            _apply_action(squares, slots, action)
        if squares != self.physical_squares or slots != self.physical_parking:
            raise GameStateError("physical checkpoint differs from confirmed action prefix")

    def to_json(self) -> dict[str, object]:
        self.validate()
        return {
            "version": STATE_VERSION,
            "promotion_rule": PROMOTION_RULE,
            "initial_fen": self.initial_fen,
            "board_fen": self.board_fen,
            "committed_parking": list(self.committed_parking.slots),
            "physical_squares": dict(sorted(self.physical_squares.items())),
            "physical_parking": self.physical_parking,
            "moves": self.moves,
            "pending": self.pending.to_json() if self.pending else None,
            "reset": self.reset.to_json() if self.reset else None,
            "events": self.events,
        }

    @classmethod
    def from_json(cls, value: object) -> GameState:
        if (not isinstance(value, dict) or type(value.get("version")) is not int or
                value["version"] not in {1, 2, 3, STATE_VERSION}):
            raise GameStateError("unsupported game-state version")
        try:
            version = value["version"]
            if version in {3, STATE_VERSION} and value.get("promotion_rule") != PROMOTION_RULE:
                raise GameStateError("game-state promotion rule does not match pawn stand-in design")
            history = list(value["moves"])
            if version in {1, 2}:
                # Older versions physically replaced promoted pawns. Their
                # snapshots cannot be interpreted under the stand-in rule.
                pending = value.get("pending")
                if pending is not None and not isinstance(pending, dict):
                    raise GameStateError("invalid pending move")
                if any(isinstance(uci, str) and len(uci) == 5 for uci in history) or (
                    pending is not None and isinstance(pending.get("uci"), str)
                    and len(pending["uci"]) == 5
                ):
                    raise GameStateError(
                        f"version {version} promotion checkpoint needs manual reconciliation"
                    )
            if version == 1:
                # v1 saved no initial FEN. Its CLI only created standard-start
                # games, so infer that start only when every saved move replays
                # to the exact saved FEN. Do not guess for other positions.
                replay = chess.Board()
                try:
                    for uci in history:
                        replay.push(replay.parse_uci(uci))
                except (TypeError, ValueError) as exc:
                    raise GameStateError("version 1 move history cannot be replayed") from exc
                if replay.fen() != value["board_fen"]:
                    raise GameStateError(
                        "version 1 initial position cannot be inferred from the standard start"
                    )
                initial_fen = chess.Board().fen()
            else:
                initial_fen = value["initial_fen"]
            state = cls(
                board_fen=value["board_fen"],
                committed_parking=ParkingInventory(tuple(value["committed_parking"])),
                physical_squares=dict(value["physical_squares"]),
                physical_parking=list(value["physical_parking"]),
                moves=history,
                pending=PendingMove.from_json(value["pending"]) if value["pending"] is not None else None,
                reset=PendingReset.from_json(value["reset"]) if version == STATE_VERSION and value.get("reset") is not None else None,
                events=list(value.get("events", [])),
                initial_fen=initial_fen,
            )
            state.validate()
            if version in {1, 2} and state.pending is not None:
                # Validate the whole saved plan before resuming any remaining
                # physical action, not just its already-confirmed prefix.
                if any(isinstance(action, ManualReplacement) for action in state.pending.actions):
                    raise GameStateError("legacy pending promotion needs manual reconciliation")
                squares = state.expected_physical_squares()
                slots = list(state.committed_parking.slots)
                for action in state.pending.actions:
                    _apply_action(squares, slots, action)
                after = state.board()
                after.push_uci(state.pending.uci)
                if (squares != _board_pieces(after) or
                        tuple(slots) != state.pending.parking_after.slots):
                    raise GameStateError("legacy pending actions do not match the legal move")
            return state
        except GameStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise GameStateError(f"invalid game-state file: {exc}") from exc


class JsonGameStore:
    """Atomic checkpoint file; every accepted physical action is persisted."""

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
        """Route a full return to the standard setup before moving anything.

        Wrong pieces on home ranks are staged in the middle. Occupied parking
        slots must retain a vacant adjacent edge square until retrieved. This
        also breaks square cycles without requiring a spare parking slot.
        """
        if self.state.pending is not None:
            raise ReconciliationRequired("finish or reconcile the pending move before resetting")
        if self.state.reset is not None:
            raise GameStateError("board reset is already prepared")
        if self.state.initial_fen != chess.Board().fen():
            raise GameStateError("board reset requires a standard-start game")
        target = _board_pieces(chess.Board())
        squares = dict(self.state.physical_squares)
        slots = list(self.state.physical_parking)
        if Counter([*squares.values(), *(piece for piece in slots if piece)]) != Counter(target.values()):
            raise GameStateError("physical pieces do not match the 32 standard starting pieces")
        if squares == target and not any(slots):
            return ()

        actions: list[Transfer] = []
        staging = tuple(
            f"{file}{rank}" for rank in "3456" for file in "abcdefgh"
        )

        def try_transfer(source: BoardSquare | ParkingSlot, destination: BoardSquare) -> bool:
            piece = _piece_at(squares, slots, source)
            if piece is None or _piece_at(squares, slots, destination) is not None:
                return False
            transfer = Transfer(piece, source, destination, "reset_board")
            try:
                self._route_transfer(transfer, squares, slots, geometry)
            except RouteError:
                return False
            _apply_action(squares, slots, transfer)
            actions.append(transfer)
            return True

        def stage(source_name: str, *, opening: int | None = None) -> bool:
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
                trial_squares = dict(squares)
                trial_slots = list(slots)
                _apply_action(trial_squares, trial_slots,
                              Transfer(squares[source_name], source, destination, "reset_board"))
                after = _accessible_parking(trial_slots, trial_squares)
                if not before.issubset(after) or (opening is not None and opening not in after):
                    continue
                if try_transfer(source, destination):
                    return True
            return False

        # Already-correct home pieces stay put unless they block a parked piece.
        for name in sorted(tuple(squares)):
            if name[1] in "1278" and squares[name] != target.get(name):
                if not stage(name):
                    raise NoRouteError(f"cannot stage {name} for board reset")

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

        while any(slots):
            accessible = _accessible_parking(slots, squares)
            choices = sorted(
                (index for index, piece in enumerate(slots) if piece is not None),
                key=lambda index: (
                    sum(name not in squares for name in _parking_approaches(ParkingSlot(index))),
                    index,
                ),
            )
            chosen = False
            for index in choices:
                if index not in accessible:
                    continue
                source = ParkingSlot(index)
                source_point = location_point(source)
                destinations = sorted(
                    (name for name, symbol in target.items()
                     if symbol == slots[index] and name not in squares),
                    key=lambda name: (
                        (location_point(BoardSquare(name)).x - source_point.x) ** 2 +
                        (location_point(BoardSquare(name)).y - source_point.y) ** 2,
                        name,
                    ),
                )
                for name in destinations:
                    destination = BoardSquare(name)
                    trial_squares = dict(squares)
                    trial_slots = list(slots)
                    _apply_action(trial_squares, trial_slots,
                                  Transfer(slots[index], source, destination, "reset_board"))
                    if len(_accessible_parking(trial_slots, trial_squares)) != sum(
                        piece is not None for piece in trial_slots
                    ):
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
            for name in sorted(tuple(squares)):
                if squares[name] == target.get(name):
                    continue
                source = BoardSquare(name)
                source_point = location_point(source)
                destinations = sorted(
                    (home for home, symbol in target.items()
                     if symbol == squares[name] and home not in squares),
                    key=lambda home: (
                        (location_point(BoardSquare(home)).x - source_point.x) ** 2 +
                        (location_point(BoardSquare(home)).y - source_point.y) ** 2,
                        home,
                    ),
                )
                for home in destinations:
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
                pending.phase = "in_flight"
                pending.active_route = wire
                self.state.events.append({
                    "event": "carry_started", "uci": pending.uci,
                    "action": pending.next_action, "wire": list(wire),
                    "clearance_override": route.clearance_ignored,
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

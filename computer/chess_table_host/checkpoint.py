"""Versioned, atomic checkpoints for chess and expected physical state.

The confirmed chess position is separate from the intermediate physical
inventory during captures and resets. Old checkpoint versions are read here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path

import chess

from .game_errors import GameStateError
from .physical import _apply_action, _board_pieces, _replay_physical
from .planner import Action, BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, Transfer


STATE_VERSION = 4
PROMOTION_RULE = "pawn_stand_in"


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
        """Replay moves using pawn bodies for every promoted stand-in."""
        board, physical = _replay_physical(self.initial_fen, self.moves)
        if board.fen() != self.board_fen:
            raise GameStateError("saved FEN differs from replayed move history")
        return physical

    def validate(self) -> None:
        """Check that confirmed actions reproduce the saved physical snapshot.

        A pending action is deliberately excluded: its physical result may
        still be unknown after a disconnect, STOP, or controller reset.
        """
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
            # Reconstruct the confirmed reset prefix, then prove the complete
            # saved plan would restore every piece before trusting the file.
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
        """Load supported checkpoint versions without guessing physical state."""
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
        """Fsync a temporary file before atomically replacing the checkpoint."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

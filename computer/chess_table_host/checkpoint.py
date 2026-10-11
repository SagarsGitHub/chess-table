"""Versioned, atomic checkpoints for chess and expected physical state.

The confirmed chess position is separate from the intermediate physical
inventory during captures and resets. Old checkpoint versions are read here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

import chess

from .game_errors import GameStateError
from .physical import _apply_action, _board_pieces, _replay_physical
from .planner import Action, BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, Transfer


STATE_VERSION = 6
PROMOTION_RULE = "pawn_stand_in"


@dataclass
class RemoteMoveReceipt:
    """Durable identity of one move requested by a local external controller."""

    request_id: str
    host_game_id: str
    source_game_id: str
    expected_ply: int
    expected_fen: str
    uci: str
    state: str = "prepared"

    def to_json(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "host_game_id": self.host_game_id,
            "source_game_id": self.source_game_id,
            "expected_ply": self.expected_ply,
            "expected_fen": self.expected_fen,
            "uci": self.uci,
            "state": self.state,
        }

    @classmethod
    def from_json(cls, value: object) -> RemoteMoveReceipt:
        if not isinstance(value, dict):
            raise GameStateError("invalid remote move receipt")
        try:
            receipt = cls(**{key: value[key] for key in (
                "request_id", "host_game_id", "source_game_id", "expected_ply",
                "expected_fen", "uci", "state",
            )})
        except (KeyError, TypeError) as exc:
            raise GameStateError("invalid remote move receipt") from exc
        receipt.validate()
        return receipt

    def validate(self) -> None:
        """Validate scalar fields before checking their game-specific meaning."""
        if (not isinstance(self.request_id, str) or not self.request_id or
                len(self.request_id) > 128 or
                not isinstance(self.host_game_id, str) or not self.host_game_id or
                not isinstance(self.source_game_id, str) or not self.source_game_id or
                len(self.source_game_id) > 256 or
                type(self.expected_ply) is not int or self.expected_ply < 0 or
                not isinstance(self.expected_fen, str) or
                not 0 < len(self.expected_fen) <= 256 or
                any(ord(char) < 32 for char in self.expected_fen) or
                not isinstance(self.uci, str) or
                re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", self.uci) is None or
                not isinstance(self.state, str) or
                self.state not in {"prepared", "in_flight", "committed", "aborted"}):
            raise GameStateError("invalid remote move receipt")


@dataclass
class RemoteMoveResolution:
    """A durable expression-to-command mapping, independent of move execution."""

    expression: str
    command: RemoteMoveReceipt

    @property
    def payload(self) -> dict[str, object]:
        """Return the position-fenced wire command without receipt lifecycle data."""
        return {
            "host_game_id": self.command.host_game_id,
            "source_game_id": self.command.source_game_id,
            "expected_ply": self.command.expected_ply,
            "expected_fen": self.command.expected_fen,
            "uci": self.command.uci,
        }

    def to_json(self) -> dict[str, object]:
        # Preserve the v5 shape so migration changes no cached wire command.
        return {"expression": self.expression, "payload": self.payload}

    @classmethod
    def from_json(cls, request_id: str, value: object) -> RemoteMoveResolution:
        if (not isinstance(value, dict) or not isinstance(value.get("payload"), dict) or
                set(value["payload"]) != {
                    "host_game_id", "source_game_id", "expected_ply", "expected_fen", "uci",
                }):
            raise GameStateError("invalid saved remote move resolution")
        try:
            command = RemoteMoveReceipt.from_json({
                **value["payload"], "request_id": request_id, "state": "prepared",
            })
            resolution = cls(expression=value["expression"], command=command)
            resolution.validate()
            return resolution
        except (KeyError, TypeError, GameStateError) as exc:
            raise GameStateError("invalid saved remote move resolution") from exc

    def validate(self) -> None:
        if (not isinstance(self.expression, str) or
                not 0 < len(self.expression) <= 256 or
                not isinstance(self.command, RemoteMoveReceipt)):
            raise GameStateError("invalid saved remote move resolution")
        self.command.validate()
        if self.command.state != "prepared":
            raise GameStateError("remote resolution command must have no execution state")


def _migrate_version_five(value: dict[str, object]) -> dict[str, object]:
    """Add the fields introduced during v5 without changing identity or plans.

    Earlier v5 writers knew only manual external control as White and had no
    expression cache. Defaults apply only to those historical missing fields;
    v6 requires all three and rejects damaged current-version checkpoints.
    """
    has_mode = "remote_mode" in value
    has_human = "remote_human" in value
    if (has_mode != has_human or
            ("remote_resolutions" in value and not (has_mode and has_human))):
        raise GameStateError("incomplete version 5 remote session metadata")
    migrated = dict(value, version=STATE_VERSION)
    migrated.setdefault("remote_mode", "external")
    migrated.setdefault("remote_human", "white")
    migrated.setdefault("remote_resolutions", {})
    return migrated


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
    remote_request_id: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "uci": self.uci, "fen_after": self.fen_after,
            "parking_after": list(self.parking_after.slots),
            "actions": [_action_to_json(action) for action in self.actions],
            "next_action": self.next_action, "phase": self.phase,
            "active_route": list(self.active_route) if self.active_route else None,
            "remote_request_id": self.remote_request_id,
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
            remote_request_id=value.get("remote_request_id"),
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
    host_game_id: str = field(default_factory=lambda: str(uuid4()))
    source_kind: str | None = None
    source_game_id: str | None = None
    remote_mode: str = "external"
    remote_human: str = "white"
    remote_start_request_id: str | None = None
    remote_resolutions: dict[str, RemoteMoveResolution] = field(default_factory=dict)
    remote_receipts: dict[str, RemoteMoveReceipt] = field(default_factory=dict)

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
        if not isinstance(self.moves, list) or any(not isinstance(move, str) for move in self.moves):
            raise GameStateError("invalid move history")
        try:
            if not isinstance(self.host_game_id, str):
                raise ValueError("host game ID must be a string")
            UUID(self.host_game_id)
            board = self.board()
            ParkingInventory(tuple(self.physical_parking))
            for name, symbol in self.physical_squares.items():
                BoardSquare(name)
                chess.Piece.from_symbol(symbol)
        except (ValueError, TypeError) as exc:
            raise GameStateError(f"invalid saved physical position: {exc}") from exc
        self._validate_remote_session(board)
        if not board.is_valid():
            raise GameStateError("committed chess position is invalid")
        if self.reset is not None:
            self._validate_reset(self.reset)
            return
        if self.pending is None:
            if (self.physical_squares != self.expected_physical_squares() or
                    tuple(self.physical_parking) != self.committed_parking.slots):
                raise GameStateError("physical snapshot differs from committed game")
            return
        self._validate_pending_move(board, self.pending)

    def _validate_remote_session(self, board: chess.Board) -> None:
        """Tie remote command fences to replayed history before motion resumes."""
        if (not isinstance(self.remote_mode, str) or
                self.remote_mode not in {"external", "human_engine"} or
                not isinstance(self.remote_human, str) or
                self.remote_human not in {"white", "black"}):
            raise GameStateError("invalid remote game mode or human color")
        if not isinstance(self.remote_resolutions, dict):
            raise GameStateError("invalid remote move resolution index")
        if not isinstance(self.remote_receipts, dict):
            raise GameStateError("invalid remote receipt index")
        if self.source_kind is None:
            if (self.source_game_id is not None or self.remote_start_request_id is not None or
                    self.remote_receipts or self.remote_resolutions):
                raise GameStateError("unbound game has remote session data")
        elif (self.source_kind != "aina" or not isinstance(self.source_game_id, str) or
              not self.source_game_id or len(self.source_game_id) > 256 or
              not isinstance(self.remote_start_request_id, str) or
              not self.remote_start_request_id or len(self.remote_start_request_id) > 128):
            raise GameStateError("invalid remote session binding")

        # History may include Stockfish moves without remote receipts. Every
        # cached command still names the exact position at its original ply.
        replay = board.root()
        positions = [replay.fen()]
        for move in board.move_stack:
            replay.push(move)
            positions.append(replay.fen())
        for request_id, record in self.remote_resolutions.items():
            if not isinstance(record, RemoteMoveResolution):
                raise GameStateError("invalid saved remote move resolution")
            record.validate()
            self._validate_remote_command(request_id, record.command, positions)
            receipt = self.remote_receipts.get(request_id)
            if receipt is not None:
                if (not isinstance(receipt, RemoteMoveReceipt) or
                        any(getattr(receipt, key) != value
                            for key, value in record.payload.items())):
                    raise GameStateError("remote receipt differs from saved move resolution")

        committed_plies: set[int] = set()
        for request_id, receipt in self.remote_receipts.items():
            if not isinstance(receipt, RemoteMoveReceipt):
                raise GameStateError("invalid remote move receipt")
            self._validate_remote_command(request_id, receipt, positions)
            if receipt.state == "committed":
                if (receipt.expected_ply >= len(self.moves) or
                        self.moves[receipt.expected_ply] != receipt.uci or
                        receipt.expected_ply in committed_plies):
                    raise GameStateError("remote receipt differs from game history")
                committed_plies.add(receipt.expected_ply)
        active_receipts = [item for item in self.remote_receipts.values()
                           if item.state in {"prepared", "in_flight"}]
        if len(active_receipts) > 1 or (
            active_receipts and (self.pending is None or
                                 self.pending.remote_request_id != active_receipts[0].request_id)
        ) or (self.pending is not None and self.pending.remote_request_id is not None and
              (not active_receipts or
               self.pending.remote_request_id != active_receipts[0].request_id)):
            raise GameStateError("remote receipt differs from pending move")
        if active_receipts:
            receipt = active_receipts[0]
            if (receipt.expected_ply != len(self.moves) or
                    receipt.expected_fen != self.board_fen or
                    receipt.uci != self.pending.uci):
                raise GameStateError("remote receipt differs from pending move")

    def _validate_remote_command(
        self, request_id: str, command: RemoteMoveReceipt, positions: list[str],
    ) -> None:
        command.validate()
        if (request_id != command.request_id or
                command.host_game_id != self.host_game_id or
                command.source_game_id != self.source_game_id or
                command.expected_ply >= len(positions) or
                command.expected_fen != positions[command.expected_ply]):
            raise GameStateError("remote command fence differs from game history")
        try:
            chess.Board(command.expected_fen).parse_uci(command.uci)
        except ValueError as exc:
            raise GameStateError("remote command UCI is illegal at its saved position") from exc

    def _validate_reset(self, reset: PendingReset) -> None:
        """Check the confirmed reset prefix and the planned final setup."""
        if self.pending is not None:
            raise GameStateError("move and board reset cannot both be pending")
        if self.initial_fen != chess.Board().fen():
            raise GameStateError("board reset requires a standard-start game")
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
        # The physical snapshot records only confirmed transfers. Replay that
        # prefix before checking that the remaining plan reaches home.
        for action in reset.actions[:reset.next_action]:
            _apply_action(squares, slots, action)
        if squares != self.physical_squares or slots != self.physical_parking:
            raise GameStateError("physical checkpoint differs from confirmed reset prefix")
        for action in reset.actions[reset.next_action:]:
            _apply_action(squares, slots, action)
        if squares != _board_pieces(chess.Board()) or any(slots):
            raise GameStateError("reset actions do not restore the standard position")

    def _validate_pending_move(self, board: chess.Board, pending: PendingMove) -> None:
        """Check the legal move and the physical prefix already confirmed."""
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
            "host_game_id": self.host_game_id,
            "source_kind": self.source_kind,
            "source_game_id": self.source_game_id,
            "remote_resolutions": {
                key: resolution.to_json()
                for key, resolution in sorted(self.remote_resolutions.items())
            },
            "remote_mode": self.remote_mode,
            "remote_human": self.remote_human,
            "remote_start_request_id": self.remote_start_request_id,
            "remote_receipts": {
                key: receipt.to_json() for key, receipt in sorted(self.remote_receipts.items())
            },
        }

    @classmethod
    def from_json(cls, value: object) -> GameState:
        """Load supported checkpoint versions without guessing physical state."""
        if (not isinstance(value, dict) or type(value.get("version")) is not int or
                value["version"] not in {1, 2, 3, 4, 5, STATE_VERSION}):
            raise GameStateError("unsupported game-state version")
        try:
            if value["version"] == 5:
                value = _migrate_version_five(value)
            version = value["version"]
            if version in {3, 4, STATE_VERSION} and value.get("promotion_rule") != PROMOTION_RULE:
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
            if version == STATE_VERSION:
                # v5 and newer require durable identity and receipt data. A
                # migration must never make an old move request appear new.
                host_game_id = value["host_game_id"]
                source_kind = value["source_kind"]
                source_game_id = value["source_game_id"]
                remote_start_request_id = value["remote_start_request_id"]
                remote_receipts_data = value["remote_receipts"]
                remote_mode = value["remote_mode"]
                remote_human = value["remote_human"]
                remote_resolutions_data = value["remote_resolutions"]
            else:
                host_game_id = str(uuid4())
                source_kind = source_game_id = remote_start_request_id = None
                remote_receipts_data = {}
                remote_mode = value.get("remote_mode", "external")
                remote_human = value.get("remote_human", "white")
                remote_resolutions_data = value.get("remote_resolutions", {})
            if not isinstance(remote_receipts_data, dict):
                raise GameStateError("invalid remote receipt index")
            if not isinstance(remote_resolutions_data, dict):
                raise GameStateError("invalid remote move resolution index")
            state = cls(
                board_fen=value["board_fen"],
                committed_parking=ParkingInventory(tuple(value["committed_parking"])),
                physical_squares=dict(value["physical_squares"]),
                physical_parking=list(value["physical_parking"]),
                moves=history,
                pending=PendingMove.from_json(value["pending"]) if value["pending"] is not None else None,
                reset=PendingReset.from_json(value["reset"]) if version in {4, STATE_VERSION} and value.get("reset") is not None else None,
                events=list(value.get("events", [])),
                initial_fen=initial_fen,
                host_game_id=host_game_id,
                source_kind=source_kind,
                source_game_id=source_game_id,
                remote_resolutions={
                    key: RemoteMoveResolution.from_json(key, item)
                    for key, item in remote_resolutions_data.items()
                },
                remote_mode=remote_mode,
                remote_human=remote_human,
                remote_start_request_id=remote_start_request_id,
                remote_receipts={
                    key: RemoteMoveReceipt.from_json(item)
                    for key, item in remote_receipts_data.items()
                },
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

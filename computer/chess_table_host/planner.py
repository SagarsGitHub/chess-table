"""Convert legal chess moves into ordered, geometry-independent piece transfers.

This module tracks chess rules and parking ownership. It deliberately does not
claim that a transfer is physically routable: the motion layer must check
clearances, assign a path, execute it, and acknowledge each completed action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, TypeAlias

import chess


PARKING_SLOT_COUNT = 32


class PlanError(ValueError):
    """The requested move cannot be represented by this logical planner."""


class IllegalMoveError(PlanError):
    """The UCI move is illegal in the supplied position."""


class NoEmptyParkingSlotError(PlanError):
    """A captured or displaced pawn has no free physical parking slot."""


class PromotionPieceUnavailableError(PlanError):
    """The requested promoted piece is absent from parking."""


class UnsupportedVariantError(PlanError):
    """The planner currently supports standard chess castling only."""


class PromotionPolicy(str, Enum):
    PAWN_STAND_IN = "pawn_stand_in"
    MANUAL_REPLACEMENT = "manual_replacement"
    PARKED_PIECE = "parked_piece"


@dataclass(frozen=True)
class BoardSquare:
    name: str

    def __post_init__(self) -> None:
        if self.name not in chess.SQUARE_NAMES:
            raise ValueError(f"Invalid board square: {self.name!r}")


@dataclass(frozen=True)
class ParkingSlot:
    slot_id: int

    def __post_init__(self) -> None:
        if type(self.slot_id) is not int or not 0 <= self.slot_id < PARKING_SLOT_COUNT:
            raise ValueError(f"Parking slot must be 0..{PARKING_SLOT_COUNT - 1}")


Location: TypeAlias = BoardSquare | ParkingSlot


@dataclass(frozen=True)
class Transfer:
    """One ordered location transfer; routes and motor commands come later.

    ``piece`` uses python-chess symbols: uppercase is White, lowercase Black.
    Standalone plans use the legal board symbol, which can differ from the
    physical pawn standing in for a promoted piece. The game runner resolves
    that difference before routing or committing a transfer.
    ``reason`` is one of move, capture, castle_king, castle_rook,
    promotion_stand_in, promotion_pawn, or promotion_piece.
    """

    piece: str
    source: Location
    destination: Location
    reason: str


@dataclass(frozen=True)
class ManualReplacement:
    """Operator must replace the pawn with the indicated promoted piece."""

    square: BoardSquare
    pawn: str
    replacement: str


Action: TypeAlias = Transfer | ManualReplacement
SlotSelector: TypeAlias = Callable[[BoardSquare, str, tuple[str | None, ...]], ParkingSlot]


@dataclass(frozen=True)
class ParkingInventory:
    """Exactly 32 perimeter slots, each empty or holding one chess piece.

    A slot stores the piece symbol. This tracks piece type and color, not the
    identity of indistinguishable pieces such as two white rooks.
    """

    slots: tuple[str | None, ...]

    def __post_init__(self) -> None:
        slots = tuple(self.slots)
        if len(slots) != PARKING_SLOT_COUNT:
            raise ValueError(f"Parking inventory requires {PARKING_SLOT_COUNT} slots")
        for symbol in slots:
            if symbol is not None:
                try:
                    chess.Piece.from_symbol(symbol)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid parked piece symbol: {symbol!r}") from exc
        object.__setattr__(self, "slots", slots)

    @classmethod
    def empty(cls) -> ParkingInventory:
        return cls((None,) * PARKING_SLOT_COUNT)


@dataclass(frozen=True)
class MovePlan:
    uci: str
    fen_before: str
    fen_after: str
    parking_before: ParkingInventory
    parking_after: ParkingInventory
    actions: tuple[Action, ...]

    @property
    def requires_manual_confirmation(self) -> bool:
        return any(isinstance(action, ManualReplacement) for action in self.actions)


def _board_square(square: chess.Square) -> BoardSquare:
    return BoardSquare(chess.square_name(square))


def _first_empty(slots: list[str | None]) -> int:
    try:
        return slots.index(None)
    except ValueError as exc:
        raise NoEmptyParkingSlotError("All 32 parking slots are occupied") from exc


def _select_empty_slot(
    slots: list[str | None], source: BoardSquare, piece: str, selector: SlotSelector | None
) -> int:
    # The default preserves the original deterministic lowest-vacancy policy.
    if selector is None:
        return _first_empty(slots)
    if all(symbol is not None for symbol in slots):
        raise NoEmptyParkingSlotError("All 32 parking slots are occupied")
    choice = selector(source, piece, tuple(slots))
    if not isinstance(choice, ParkingSlot):
        raise PlanError("Slot selector must return a ParkingSlot")
    if slots[choice.slot_id] is not None:
        raise PlanError(f"Slot selector chose occupied slot {choice.slot_id}")
    return choice.slot_id


def plan_move(
    board: chess.Board,
    uci: str,
    parking: ParkingInventory,
    *,
    promotion_policy: PromotionPolicy = PromotionPolicy.PAWN_STAND_IN,
    slot_selector: SlotSelector | None = None,
) -> MovePlan:
    """Plan an ordered logical move without changing ``board`` or ``parking``.

    Captured pieces are transferred to the lowest numbered empty slot before
    the attacking piece moves. En passant removes the pawn from its actual
    square. Castling emits separate king and rook transfers. By default, a
    promoted pawn remains the same physical pawn while chess rules use the
    selected promoted type. Explicit legacy policies can request a manual
    replacement or a matching parked piece. ``slot_selector`` may choose a physically
    reachable empty slot based on its own occupancy and geometry model; it is
    called with source, legal-board piece symbol, and the current immutable
    slot snapshot. After a promotion, standalone transfer symbols and parking
    inventory are logical drafts; GameRunner resolves the actual pawn token
    before physical routing. No motor route or acknowledgement is implied.
    """

    if board.chess960:
        raise UnsupportedVariantError("Chess960 castling is not implemented")
    if not isinstance(parking, ParkingInventory):
        raise TypeError("parking must be a ParkingInventory")
    try:
        policy = PromotionPolicy(promotion_policy)
    except ValueError as exc:
        raise ValueError(f"Unknown promotion policy: {promotion_policy!r}") from exc
    try:
        move = board.parse_uci(uci)
    except ValueError as exc:
        raise IllegalMoveError(f"Illegal UCI move {uci!r} in {board.fen()}") from exc
    # python-chess parses the UCI null move, which has no physical piece to move.
    if not move or not board.is_legal(move):
        raise IllegalMoveError(f"Illegal UCI move {uci!r} in {board.fen()}")

    mover = board.piece_at(move.from_square)
    assert mover is not None  # parse_uci accepted a legal move
    actions: list[Action] = []
    slots = list(parking.slots)

    if board.is_capture(move):
        if board.is_en_passant(move):
            captured_square = move.to_square + (-8 if board.turn == chess.WHITE else 8)
        else:
            captured_square = move.to_square
        captured = board.piece_at(captured_square)
        assert captured is not None  # a legal capture always has a victim
        slot = _select_empty_slot(slots, _board_square(captured_square), captured.symbol(), slot_selector)
        actions.append(
            Transfer(captured.symbol(), _board_square(captured_square), ParkingSlot(slot), "capture")
        )
        slots[slot] = captured.symbol()

    if board.is_castling(move):
        rank = chess.square_rank(move.from_square)
        kingside = board.is_kingside_castling(move)
        rook_from = chess.square(7 if kingside else 0, rank)
        rook_to = chess.square(5 if kingside else 3, rank)
        actions.extend(
            (
                Transfer(mover.symbol(), _board_square(move.from_square), _board_square(move.to_square), "castle_king"),
                Transfer(chess.Piece(chess.ROOK, board.turn).symbol(), _board_square(rook_from), _board_square(rook_to), "castle_rook"),
            )
        )
    elif move.promotion:
        promoted = chess.Piece(move.promotion, board.turn).symbol()
        if policy is PromotionPolicy.PAWN_STAND_IN:
            actions.append(
                Transfer(mover.symbol(), _board_square(move.from_square),
                         _board_square(move.to_square), "promotion_stand_in")
            )
        elif policy is PromotionPolicy.MANUAL_REPLACEMENT:
            actions.extend(
                (
                    Transfer(mover.symbol(), _board_square(move.from_square), _board_square(move.to_square), "move"),
                    ManualReplacement(_board_square(move.to_square), mover.symbol(), promoted),
                )
            )
        else:
            try:
                promoted_slot = slots.index(promoted)
            except ValueError as exc:
                raise PromotionPieceUnavailableError(
                    f"No {promoted!r} piece is parked for promotion"
                ) from exc
            pawn_slot = _select_empty_slot(slots, _board_square(move.from_square), mover.symbol(), slot_selector)
            actions.extend(
                (
                    Transfer(mover.symbol(), _board_square(move.from_square), ParkingSlot(pawn_slot), "promotion_pawn"),
                    Transfer(promoted, ParkingSlot(promoted_slot), _board_square(move.to_square), "promotion_piece"),
                )
            )
            slots[pawn_slot] = mover.symbol()
            slots[promoted_slot] = None
    else:
        actions.append(
            Transfer(mover.symbol(), _board_square(move.from_square), _board_square(move.to_square), "move")
        )

    next_board = board.copy()
    next_board.push(move)
    return MovePlan(
        uci=board.uci(move),
        fen_before=board.fen(),
        fen_after=next_board.fen(),
        parking_before=parking,
        parking_after=ParkingInventory(tuple(slots)),
        actions=tuple(actions),
    )

"""Expected physical inventory and checkpoint replay helpers.

These helpers operate on piece symbols, without changing the legal chess board.
The host can therefore checkpoint intermediate capture and reset transfers.
"""

from __future__ import annotations

import chess

from .game_errors import GameStateError
from .planner import Action, BoardSquare, ManualReplacement, ParkingInventory, ParkingSlot, Transfer
from .routing import location_point
from .wire import encode_map


def _board_pieces(board: chess.Board) -> dict[str, str]:
    """Snapshot piece symbols by square name for the physical inventory."""
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
    """Apply one confirmed transfer to a mutable inventory snapshot."""
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
    """Return occupied slots with at least one vacant board-side approach."""
    return {
        index for index, piece in enumerate(slots)
        if piece is not None and any(
            square not in squares for square in _parking_approaches(ParkingSlot(index))
        )
    }


def _physical_map(squares: dict[str, str], slots: list[str | None]) -> str:
    """Encode the current physical snapshot for the Uno's occupancy map."""
    # encode_map inspects piece_map only; this temporary board need not be a
    # legal chess position while a multi-transfer move is in progress.
    board = chess.Board(None)
    for name, symbol in squares.items():
        board.set_piece_at(chess.parse_square(name), chess.Piece.from_symbol(symbol))
    return encode_map(board, ParkingInventory(tuple(slots)))

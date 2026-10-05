import unittest

import chess

from chess_table_host.game import GameState
from chess_table_host.planner import ParkingInventory


class PersistedHistoryTests(unittest.TestCase):
    def test_rebuilt_board_retains_threefold_repetition(self) -> None:
        board = chess.Board()
        initial_fen = board.fen()
        moves = ["g1f3", "g8f6", "f3g1", "f6g8"] * 2
        for uci in moves:
            board.push_uci(uci)
        squares = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        state = GameState(
            board.fen(), ParkingInventory.empty(), squares, [None] * 32,
            moves=moves, initial_fen=initial_fen,
        )
        state.validate()
        self.assertTrue(state.board().can_claim_threefold_repetition())
        self.assertFalse(chess.Board(state.board_fen).can_claim_threefold_repetition())


if __name__ == "__main__":
    unittest.main()

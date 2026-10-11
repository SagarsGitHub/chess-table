import unittest
import chess
from chess_table_host.move_expression import resolve_move


class MoveExpressionTests(unittest.TestCase):
    def test_opening_expressions(self):
        for text, uci in [("knight to c3", "b1c3"), ("move my knight from b1 to c3", "b1c3"),
                          ("pawn to e4", "e2e4"), ("e2 to e4", "e2e4"), ("Nc3", "b1c3"), ("b1c3", "b1c3")]:
            with self.subTest(text=text):
                self.assertEqual(resolve_move(chess.Board(), text), uci)

    def test_ambiguity_illegality_and_no_capture(self):
        board = chess.Board("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_move(board, "knight to d2")
        self.assertEqual(resolve_move(board, "knight from b1 to d2"), "b1d2")
        for text in ("knight to c4", "pawn takes e4", "should I play Nc3", "move queen to h8"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                resolve_move(chess.Board(), text)

    def test_castling_capture_promotion_and_black(self):
        castle = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertEqual(resolve_move(castle, "castle kingside"), "e1g1")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_move(castle, "castle")
        promotion = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_move(promotion, "pawn to a8")
        self.assertEqual(resolve_move(promotion, "pawn to a8 promote to knight"), "a7a8n")
        board = chess.Board()
        board.push_uci("e2e4")
        self.assertEqual(resolve_move(board, "knight to c6"), "b8c6")
        board.push_uci("d7d5")
        self.assertEqual(resolve_move(board, "pawn takes d5"), "e4d5")

    def test_coordinate_capture_intent_matches_piece_expressions(self):
        for expression in ("e2 takes e4", "pawn takes e4"):
            with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "no legal move"):
                resolve_move(chess.Board(), expression)

        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("d7d5")
        for expression in ("e4 takes d5", "pawn takes d5", "e4 to d5"):
            with self.subTest(expression=expression):
                self.assertEqual(resolve_move(board, expression), "e4d5")

        # A capture can target an empty square during en passant.
        board = chess.Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        for expression in ("e5 takes d6", "pawn takes d6"):
            with self.subTest(expression=expression):
                self.assertEqual(resolve_move(board, expression), "e5d6")


class TranscribedMoveTests(unittest.TestCase):
    def test_queen_to_d3_after_clearing_d2(self):
        board = chess.Board()
        board.push_uci("d2d4")
        board.push_uci("d7d5")
        for text in ("Queen 2D3", "queen two d3", "queen too d3", "queen from d1 2d3", "D1-D3", "D1D3"):
            with self.subTest(text=text):
                self.assertEqual(resolve_move(board, text), "d1d3")
        for text in ("d2-d3", "D 2 - D 3"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                resolve_move(board, text)

    def test_incomplete_and_blocked_queen_moves_never_guess(self):
        for text in ("2d3", "2 d3", "to d3", "play 2d3"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "incomplete"):
                resolve_move(chess.Board(), text)
        for text in ("Queen 2D3", "Do not Queen 2D3", "Queen 2D3 tomorrow", '"Queen 2D3"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                resolve_move(chess.Board(), text)
        self.assertEqual(resolve_move(chess.Board(), "d2-d3"), "d2d3")


class SpokenCoordinatePairsTests(unittest.TestCase):
    def test_comma_and_space_pairs_keep_exact_source_and_destination(self):
        for text in ("D2, D4", "D2 D4", "D 2, D 4", "move D2, D4", "d22d4", "d2_d4", "d2-d4", "d2 to d4", "d2twoD4", "d2/d4"):
            with self.subTest(text=text):
                self.assertEqual(resolve_move(chess.Board(), text), "d2d4")
        board = chess.Board()
        board.push_uci("d2d4")
        board.push_uci("g8f6")
        with self.assertRaises(ValueError):
            resolve_move(board, "D2, D3")
        self.assertEqual(resolve_move(board, "D1, D3"), "d1d3")

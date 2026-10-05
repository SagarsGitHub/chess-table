import unittest

import chess

from chess_table_host import (
    BoardSquare,
    IllegalMoveError,
    ManualReplacement,
    NoEmptyParkingSlotError,
    ParkingInventory,
    ParkingSlot,
    PromotionPieceUnavailableError,
    PromotionPolicy,
    Transfer,
    UnsupportedVariantError,
    plan_move,
)


def played(*moves: str) -> chess.Board:
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board


class PlannerTests(unittest.TestCase):
    def test_quiet_move_does_not_mutate_inputs(self) -> None:
        board = chess.Board()
        before = board.fen()
        parking = ParkingInventory.empty()

        plan = plan_move(board, "e2e4", parking)

        self.assertEqual(
            plan.actions,
            (Transfer("P", BoardSquare("e2"), BoardSquare("e4"), "move"),),
        )
        self.assertEqual(board.fen(), before)
        self.assertEqual(parking, ParkingInventory.empty())
        self.assertEqual(plan.parking_after, parking)
        self.assertEqual(chess.Board(plan.fen_after).piece_at(chess.E4), chess.Piece(chess.PAWN, chess.WHITE))
        self.assertFalse(plan.requires_manual_confirmation)

    def test_capture_parks_victim_before_attacker_moves(self) -> None:
        board = played("e2e4", "d7d5")
        slots = [None] * 32
        slots[0] = "n"
        parking = ParkingInventory(tuple(slots))

        plan = plan_move(board, "e4d5", parking)

        self.assertEqual(
            plan.actions,
            (
                Transfer("p", BoardSquare("d5"), ParkingSlot(1), "capture"),
                Transfer("P", BoardSquare("e4"), BoardSquare("d5"), "move"),
            ),
        )
        self.assertEqual(plan.parking_after.slots[0:3], ("n", "p", None))

    def test_en_passant_parks_pawn_from_its_actual_square(self) -> None:
        board = played("e2e4", "a7a6", "e4e5", "d7d5")

        plan = plan_move(board, "e5d6", ParkingInventory.empty())

        self.assertEqual(
            plan.actions,
            (
                Transfer("p", BoardSquare("d5"), ParkingSlot(0), "capture"),
                Transfer("P", BoardSquare("e5"), BoardSquare("d6"), "move"),
            ),
        )
        self.assertIsNone(chess.Board(plan.fen_after).piece_at(chess.D5))
        self.assertEqual(plan.parking_after.slots[0], "p")

    def test_black_en_passant_uses_white_pawns_square(self) -> None:
        board = played("a2a3", "d7d5", "a3a4", "d5d4", "e2e4")

        plan = plan_move(board, "d4e3", ParkingInventory.empty())

        self.assertEqual(
            plan.actions,
            (
                Transfer("P", BoardSquare("e4"), ParkingSlot(0), "capture"),
                Transfer("p", BoardSquare("d4"), BoardSquare("e3"), "move"),
            ),
        )

    def test_castling_has_separate_king_and_rook_transfers(self) -> None:
        board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        parking = ParkingInventory.empty()
        for uci, king_destination, rook_source, rook_destination in (
            ("e1g1", "g1", "h1", "f1"),
            ("e1c1", "c1", "a1", "d1"),
        ):
            with self.subTest(uci=uci):
                plan = plan_move(board, uci, parking)
                self.assertEqual(
                    plan.actions,
                    (
                        Transfer("K", BoardSquare("e1"), BoardSquare(king_destination), "castle_king"),
                        Transfer("R", BoardSquare(rook_source), BoardSquare(rook_destination), "castle_rook"),
                    ),
                )

    def test_manual_promotion_has_explicit_operator_step(self) -> None:
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")

        plan = plan_move(board, "a7a8q", ParkingInventory.empty())

        self.assertEqual(
            plan.actions,
            (
                Transfer("P", BoardSquare("a7"), BoardSquare("a8"), "move"),
                ManualReplacement(BoardSquare("a8"), "P", "Q"),
            ),
        )
        self.assertTrue(plan.requires_manual_confirmation)
        self.assertEqual(plan.parking_after, ParkingInventory.empty())
        self.assertEqual(chess.Board(plan.fen_after).piece_at(chess.A8), chess.Piece(chess.QUEEN, chess.WHITE))

    def test_black_underpromotion_uses_lowercase_piece_symbol(self) -> None:
        board = chess.Board("4k3/8/8/8/8/8/p7/4K3 b - - 0 1")
        slots = [None] * 32
        slots[5] = "n"

        plan = plan_move(
            board,
            "a2a1n",
            ParkingInventory(tuple(slots)),
            promotion_policy=PromotionPolicy.PARKED_PIECE,
        )

        self.assertEqual(
            plan.actions,
            (
                Transfer("p", BoardSquare("a2"), ParkingSlot(0), "promotion_pawn"),
                Transfer("n", ParkingSlot(5), BoardSquare("a1"), "promotion_piece"),
            ),
        )

    def test_parked_piece_promotion_swaps_pawn_into_empty_slot(self) -> None:
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        slots = [None] * 32
        slots[4] = "Q"
        parking = ParkingInventory(tuple(slots))

        plan = plan_move(board, "a7a8q", parking, promotion_policy=PromotionPolicy.PARKED_PIECE)

        self.assertEqual(
            plan.actions,
            (
                Transfer("P", BoardSquare("a7"), ParkingSlot(0), "promotion_pawn"),
                Transfer("Q", ParkingSlot(4), BoardSquare("a8"), "promotion_piece"),
            ),
        )
        self.assertEqual(plan.parking_after.slots[0], "P")
        self.assertIsNone(plan.parking_after.slots[4])
        self.assertFalse(plan.requires_manual_confirmation)

    def test_capture_promotion_reserves_distinct_victim_and_pawn_slots(self) -> None:
        board = chess.Board("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        slots = [None] * 32
        slots[4] = "Q"

        plan = plan_move(
            board,
            "a7b8q",
            ParkingInventory(tuple(slots)),
            promotion_policy=PromotionPolicy.PARKED_PIECE,
        )

        self.assertEqual(
            plan.actions,
            (
                Transfer("r", BoardSquare("b8"), ParkingSlot(0), "capture"),
                Transfer("P", BoardSquare("a7"), ParkingSlot(1), "promotion_pawn"),
                Transfer("Q", ParkingSlot(4), BoardSquare("b8"), "promotion_piece"),
            ),
        )
        self.assertEqual(plan.parking_after.slots[0:5], ("r", "P", None, None, None))

    def test_full_storage_rejects_capture_before_changing_inputs(self) -> None:
        board = played("e2e4", "d7d5")
        before = board.fen()
        parking = ParkingInventory(("p",) * 32)

        with self.assertRaises(NoEmptyParkingSlotError):
            plan_move(board, "e4d5", parking)

        self.assertEqual(board.fen(), before)
        self.assertEqual(parking.slots, ("p",) * 32)

    def test_parked_promotion_requires_matching_piece_and_vacancy(self) -> None:
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        with self.assertRaises(PromotionPieceUnavailableError):
            plan_move(board, "a7a8q", ParkingInventory.empty(), promotion_policy=PromotionPolicy.PARKED_PIECE)
        with self.assertRaises(NoEmptyParkingSlotError):
            plan_move(board, "a7a8q", ParkingInventory(("Q",) * 32), promotion_policy=PromotionPolicy.PARKED_PIECE)

    def test_illegal_move_and_unsupported_variant_are_rejected(self) -> None:
        with self.assertRaises(IllegalMoveError):
            plan_move(chess.Board(), "e2e5", ParkingInventory.empty())
        with self.assertRaises(IllegalMoveError):
            plan_move(chess.Board(), "0000", ParkingInventory.empty())
        with self.assertRaises(UnsupportedVariantError):
            plan_move(chess.Board(chess960=True), "e2e4", ParkingInventory.empty())

    def test_inventory_and_slot_validation(self) -> None:
        with self.assertRaises(ValueError):
            ParkingInventory((None,) * 31)
        with self.assertRaises(ValueError):
            ParkingInventory(("x",) + (None,) * 31)
        with self.assertRaises(ValueError):
            ParkingSlot(32)
        with self.assertRaises(ValueError):
            ParkingSlot(1.0)
        with self.assertRaises(ValueError):
            BoardSquare("i9")


if __name__ == "__main__":
    unittest.main()

import unittest

import chess

from chess_table_host import BoardSquare, ParkingInventory, ParkingSlot, Transfer
from chess_table_host.routing import HalfSquarePoint, CarryRoute, plan_transfer_route
from chess_table_host.wire import encode_carry, encode_map, location_id


class WireEncodingTests(unittest.TestCase):
    def test_position_ids_match_firmware_piece_map(self) -> None:
        self.assertEqual(location_id(BoardSquare("a1")), 0)
        self.assertEqual(location_id(BoardSquare("h8")), 63)
        self.assertEqual(location_id(ParkingSlot(0)), 64)
        self.assertEqual(location_id(ParkingSlot(31)), 95)

    def test_initial_position_and_parking_bit_order(self) -> None:
        self.assertEqual(encode_map(chess.Board(), ParkingInventory.empty()), "FFFF00000000FFFF00000000")
        slots = [None] * 32
        slots[0] = "p"
        slots[31] = "N"
        self.assertEqual(encode_map(chess.Board(None), ParkingInventory(tuple(slots))),
                         "000000000000000001000080")

    def test_short_nominal_carry_encoding(self) -> None:
        transfer = Transfer("p", BoardSquare("d5"), ParkingSlot(0), "capture")
        route = plan_transfer_route(transfer, occupied_squares=(BoardSquare("d5"),),
                                    occupied_parking_slots=(), max_waypoints=12)
        source, destination, encoded = encode_carry(route)
        self.assertEqual((source, destination), ("35", "64"))
        self.assertEqual(len(encoded), 2 * len(route.points))
        self.assertEqual(encoded[:2], "79")
        self.assertEqual(encoded[-2:], "00")

    def test_overlong_and_mismatched_endpoints_rejected(self) -> None:
        transfer = Transfer("P", BoardSquare("a1"), BoardSquare("b2"), "move")
        wrong = CarryRoute(transfer, (HalfSquarePoint(3, 3), HalfSquarePoint(2, 2)))
        with self.assertRaises(ValueError):
            encode_carry(wrong)
        valid = CarryRoute(transfer, (HalfSquarePoint(1, 1), HalfSquarePoint(2, 2),
                                      HalfSquarePoint(3, 3)))
        with self.assertRaises(ValueError):
            encode_carry(valid, max_waypoints=2)


if __name__ == "__main__":
    unittest.main()

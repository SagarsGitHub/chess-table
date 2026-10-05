"""Special moves through the GUI's simulated HTTP game session."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

import chess

from chess_table_host.game import GameState, JsonGameStore
from chess_table_host.gui import GameController, GuiHTTPServer
from chess_table_host.planner import ParkingInventory


FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"


def wait_for(predicate):
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the GUI game")


@contextmanager
def seeded_gui(fen: str):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "game.json"
        board = chess.Board(fen)
        assert board.is_valid()
        squares = {
            chess.square_name(square): piece.symbol()
            for square, piece in board.piece_map().items()
        }
        store = JsonGameStore(path)
        store.save(GameState(board.fen(), ParkingInventory.empty(), squares, [None] * 32))
        controller = GameController(
            state_path=path, new=False, simulate=True, geometry_file=FIXTURE,
            serial_port="unused", serial_log=path.with_suffix(".serial.jsonl"),
            stockfish_path="unused", engine_time=0.01,
            moving_diameter=18.0, standing_diameter=18.0, margin=0.0,
            allow_tight_routes=True,
        )
        with GuiHTTPServer(("127.0.0.1", 0), controller) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}", store, controller
            finally:
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                server.shutdown()
                thread.join(timeout=5)
                controller.close()


def get_state(base: str) -> dict:
    with urlopen(base + "/api/state", timeout=5) as response:
        assert response.status == 200
        return json.load(response)


def post(base: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    request = Request(
        base + path, data=data,
        headers={"Content-Type": "application/json", "Origin": base},
    )
    with urlopen(request, timeout=5) as response:
        assert response.status == 202
        return json.load(response)


class GuiSpecialMoveTests(unittest.TestCase):
    def play_one(self, fen: str, uci: str) -> tuple[dict, GameState]:
        with seeded_gui(fen) as (base, store, controller):
            before = get_state(base)
            self.assertIn(uci, before["legal_moves"])
            post(base, "/api/start", {"mode": "manual_both"})
            wait_for(lambda: get_state(base)["can_move"])
            post(base, "/api/move", {"uci": uci})
            after = wait_for(lambda: (
                state if (state := get_state(base))["moves"] == [uci]
                and state["pending"] is None else None
            ))
            self.assertIsNone(after["error"])
            self.assertEqual(after["ply"], 1)
            saved = store.load()
            self.assertEqual(saved.moves, [uci])
            self.assertIsNone(saved.pending)
            return after, saved

    def test_castling_commits_king_and_rook_to_physical_squares(self):
        after, saved = self.play_one("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1")
        self.assertEqual(after["physical_squares"]["g1"], "K")
        self.assertEqual(after["physical_squares"]["f1"], "R")
        self.assertNotIn("e1", after["physical_squares"])
        self.assertNotIn("h1", after["physical_squares"])
        self.assertFalse(any(after["physical_parking"]))
        self.assertEqual(chess.Board(saved.board_fen).piece_at(chess.G1), chess.Piece(chess.KING, chess.WHITE))

    def test_en_passant_parks_the_pawn_from_its_actual_square(self):
        after, saved = self.play_one("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1", "e5d6")
        self.assertEqual(after["physical_squares"]["d6"], "P")
        self.assertNotIn("d5", after["physical_squares"])
        self.assertNotIn("e5", after["physical_squares"])
        self.assertEqual(after["physical_parking"].count("p"), 1)
        self.assertEqual(saved.committed_parking.slots, tuple(after["physical_parking"]))
        self.assertEqual(chess.Board(saved.board_fen).piece_at(chess.D6), chess.Piece(chess.PAWN, chess.WHITE))

    def test_promotion_keeps_physical_pawn_and_logical_queen(self):
        after, saved = self.play_one("4k3/P7/8/8/8/8/8/4K3 w - - 0 1", "a7a8q")
        self.assertEqual(after["physical_squares"]["a8"], "P")
        self.assertNotIn("a7", after["physical_squares"])
        self.assertFalse(any(after["physical_parking"]))
        self.assertEqual(chess.Board(saved.board_fen).piece_at(chess.A8), chess.Piece(chess.QUEEN, chess.WHITE))
        self.assertEqual(after["moves_san"], ["a8=Q+"])


if __name__ == "__main__":
    unittest.main()

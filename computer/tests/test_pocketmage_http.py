"""PocketMage LAN control uses the existing worker and durable move fences."""

from contextlib import contextmanager
from http.client import HTTPConnection
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chess

from chess_table_host.checkpoint import GameState
from chess_table_host.game import GameStateError
from chess_table_host.remote_http import PocketMageHTTPServer
from test_remote_http import (
    FIXTURE, HeldLink, close_controller, controller_for, fake_engine, http_server,
    move_body, remote_server, request, start_body, wait_for,
)


@contextmanager
def pocketmage_server(controller):
    with http_server(PocketMageHTTPServer(("127.0.0.1", 0), controller)) as base:
        yield base


def start_intent(status, **options):
    return dict(start_body(status), source_kind="pocketmage", **options)


def waiting(base, ply=0):
    return wait_for(lambda: (
        value if (value := request(base, "/v1/status")[1])["can_move"]
        and value["ply"] == ply else None
    ))


class PocketMageHTTPTests(unittest.TestCase):
    def test_transport_and_limited_routes_without_a_bearer_token(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base:
                    code, status = request(base, "/v1/status", token=None)
                    self.assertEqual(code, 200)
                    self.assertEqual(status["turn"], "white")
                    self.assertFalse(status["check"])
                    self.assertIsNone(status["result"])
                    self.assertFalse(status["can_move"])
                    self.assertEqual(set(status["legal_moves"]),
                                     {move.uci() for move in chess.Board().legal_moves})
                    self.assertEqual(request(base, "/v1/status", origin=base)[0], 403)
                    for route in ("/api/state", "/v1/reconcile",
                                  "/v1/moves/resolve", "/v1/raw"):
                        self.assertEqual(request(base, route, {}, token=None)[0], 404)
                    self.assertEqual(request(base, "/v1/session/start", start_body(status))[0], 400)
                    for invalid in (dict(start_intent(status), mode="self_play"),
                                    dict(start_intent(status), extra=True)):
                        self.assertEqual(request(base, "/v1/session/start", invalid)[0], 400)
                    port = int(base.rsplit(":", 1)[1])
                    for headers, body, expected in (
                        ({"Host": f"evil.example:{port}"}, None, 403),
                        ({"Host": f"localhost:{port}"}, None, 403),
                        ({"Host": f"127.0.0.1:{port + 1}"}, None, 403),
                        ({"Content-Type": "text/plain"}, "{}", 415),
                        ({"Content-Type": "application/json"}, "[1]", 400),
                        ({"Content-Type": "application/json"}, "{" * 8193, 413),
                        ({"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, "{}", 400),
                    ):
                        with self.subTest(headers=headers, expected=expected):
                            connection = HTTPConnection("127.0.0.1", port, timeout=5)
                            try:
                                connection.request("POST" if body is not None else "GET",
                                                   "/v1/stop" if body is not None else "/v1/status",
                                                   body=body, headers=headers)
                                response = connection.getresponse()
                                self.assertEqual(response.status, expected)
                                self.assertIn("error", json.loads(response.read()))
                            finally:
                                connection.close()
                    self.assertIsNone(controller._worker)
                    self.assertIsNone(controller.link)
            finally:
                close_controller(controller)

    def test_manual_moves_fences_duplicate_delivery_and_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base, remote_server(controller) as aina:
                    start = start_intent(request(base, "/v1/status")[1], mode="external", human="black")
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    status = waiting(base)
                    self.assertEqual(status["source_kind"], "pocketmage")
                    body = move_body(status)
                    self.assertEqual(request(base, "/v1/moves", dict(body, expected_ply=1))[0], 409)
                    self.assertEqual(request(base, "/v1/moves", dict(body, expected_fen="wrong"))[0], 409)
                    self.assertEqual(request(base, "/v1/moves", dict(body, uci="e2e5"))[0], 422)
                    self.assertIn(request(base, "/v1/moves", body)[0], {200, 202})
                    status = waiting(base, ply=1)
                    self.assertEqual(status["turn"], "black")
                    self.assertEqual(request(base, "/v1/moves", body)[0], 200)
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"], "committed")
                    self.assertEqual(request(base, "/v1/moves", dict(body, uci="d2d4"))[0], 409)
                    # Even identical source IDs and request IDs cannot cross listeners.
                    self.assertEqual(request(aina, "/v1/moves", body)[0], 409)
                    self.assertEqual(request(aina, "/v1/moves/" + body["request_id"])[0], 409)
                    self.assertEqual(request(aina, "/v1/session/start", dict(start, source_kind="aina"))[0], 409)
                    self.assertEqual(request(aina, "/v1/session/reset", {"host_game_id": status["host_game_id"]})[0], 409)
                    self.assertEqual(request(aina, "/v1/moves/resolve", {"request_id": "resolve-1", "expression": "e7e5"})[0], 409)
                    second = dict(body, request_id="black-move", expected_ply=1,
                                  expected_fen=status["fen"], uci="e7e5")
                    self.assertIn(request(base, "/v1/moves", second)[0], {200, 202})
                    waiting(base, ply=2)
                    self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                    wait_for(lambda: not controller.remote_status()["busy"])
            finally:
                close_controller(controller)
            resumed = controller_for(path, new=False, simulate=True)
            try:
                with pocketmage_server(resumed) as base:
                    status = request(base, "/v1/status")[1]
                    self.assertEqual(status["ply"], 2)
                    self.assertFalse(status["can_move"])
                    self.assertIsNone(resumed._worker)
                    self.assertEqual(request(base, "/v1/moves", body)[0], 200)
                    self.assertEqual(request(base, "/v1/session/start", dict(start, human="white"))[0], 409)
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    waiting(base, ply=2)
            finally:
                close_controller(resumed)

    def test_aina_owner_rejects_pocketmage_start_moves_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base, remote_server(controller) as aina:
                    status = request(aina, "/v1/status")[1]
                    self.assertEqual(request(aina, "/v1/session/start", start_body(status))[0], 202)
                    status = waiting(aina)
                    self.assertEqual(request(base, "/v1/session/start", start_intent(status))[0], 409)
                    body = move_body(status)
                    self.assertEqual(request(base, "/v1/moves", body)[0], 409)
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[0], 409)
                    self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                    wait_for(lambda: not controller.remote_status()["busy"])
            finally:
                close_controller(controller)

    def test_stockfish_mode_accepts_only_each_chosen_human_turn(self):
        for human in ("white", "black"):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory, fake_engine():
                controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
                try:
                    with pocketmage_server(controller) as base:
                        initial = request(base, "/v1/status")[1]
                        start = start_intent(initial, mode="human_engine", human=human)
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        first_ply = 0 if human == "white" else 1
                        status = waiting(base, ply=first_ply)
                        self.assertEqual(status["turn"], human)
                        body = dict(move_body(status), expected_ply=first_ply,
                                    uci="e2e4" if human == "white" else "e7e5")
                        self.assertIn(request(base, "/v1/moves", body)[0], {200, 202})
                        status = waiting(base, ply=first_ply + 2)
                        self.assertEqual(status["turn"], human)
                        self.assertEqual(status["mode"], "human_engine")
                finally:
                    close_controller(controller)

    def test_engine_engine_plays_both_colors_to_game_over(self):
        sequence = ("f2f3", "e7e5", "g2g4", "d8h4")
        turns = []

        class MatingEngine:
            def play(self, board, _limit, *, root_moves):
                turns.append(board.turn)
                move = chess.Move.from_uci(sequence[board.ply()])
                if move not in root_moves:
                    raise AssertionError("mating move failed route preflight")
                return SimpleNamespace(move=move)

            def quit(self):
                pass

        with tempfile.TemporaryDirectory() as directory, fake_engine(), \
             patch("chess.engine.SimpleEngine.popen_uci", return_value=MatingEngine()):
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base:
                    initial = request(base, "/v1/status")[1]
                    start = start_intent(initial, mode="engine_engine")
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    status = wait_for(lambda: (
                        value if (value := request(base, "/v1/status")[1])["phase"] == "game_over"
                        else None), timeout=20)
                    self.assertEqual(turns, [chess.WHITE, chess.BLACK, chess.WHITE, chess.BLACK])
                    self.assertEqual(status["mode"], "engine_engine")
                    self.assertEqual(status["ply"], 4)
                    self.assertEqual(status["result"], "0-1")
                    self.assertTrue(status["check"])
                    self.assertFalse(status["can_move"])
                    self.assertEqual(status["legal_moves"], [])
                    saved = controller.store.load()
                    self.assertEqual(saved.remote_mode, "engine_engine")
                    self.assertEqual(saved.moves, list(sequence))
                    self.assertEqual(saved.remote_receipts, {})
                    self.assertEqual(GameState.from_json(saved.to_json()).remote_mode, "engine_engine")
            finally:
                close_controller(controller)

    def test_engine_unavailable_leaves_checkpoint_unbound(self):
        for mode in ("human_engine", "engine_engine"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
                try:
                    with pocketmage_server(controller) as base:
                        initial = request(base, "/v1/status")[1]
                        with patch("chess_table_host.gui_remote._resolve_engine_path",
                                   side_effect=GameStateError("private engine path")):
                            code, error = request(base, "/v1/session/start", start_intent(initial, mode=mode))
                        self.assertEqual(code, 503)
                        self.assertNotIn("private engine path", json.dumps(error))
                        self.assertIsNone(controller.store.load().source_kind)
                        self.assertIsNone(controller._worker)
                finally:
                    close_controller(controller)

    def test_stop_during_carry_preserves_uncertainty_across_host_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = HeldLink()
            controller = controller_for(path, new=True, simulate=False, link=link)
            try:
                with pocketmage_server(controller) as base:
                    initial = request(base, "/v1/status")[1]
                    start = start_intent(initial)
                    request(base, "/v1/session/start", start)
                    body = move_body(waiting(base))
                    self.assertEqual(request(base, "/v1/moves", body)[0], 202)
                    self.assertTrue(link.entered.wait(timeout=5))
                    self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                    wait_for(lambda: not controller.remote_status()["busy"])
                    self.assertEqual(link.carries, 1)
            finally:
                close_controller(controller)
            resumed = controller_for(path, new=False, simulate=True)
            try:
                with pocketmage_server(resumed) as base:
                    status = request(base, "/v1/status")[1]
                    self.assertEqual(status["phase"], "reconciliation_required")
                    self.assertFalse(status["can_move"])
                    self.assertEqual(status["legal_moves"], [])
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"],
                                     "reconciliation_required")
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    self.assertIsNone(resumed._worker)
                    self.assertEqual(request(base, "/v1/moves", dict(body, request_id="new-move"))[0], 409)
            finally:
                close_controller(resumed)

    def test_cli_enables_idle_listener_without_opening_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs" / "boot.json"
            with socket.socket() as gui, socket.socket() as pocket:
                gui.bind(("127.0.0.1", 0))
                pocket.bind(("127.0.0.1", 0))
                gui_port, pocket_port = gui.getsockname()[1], pocket.getsockname()[1]
            process = subprocess.Popen([
                sys.executable, "-m", "chess_table_host.gui", "--new", "--state", str(path),
                "--simulate", "--geometry-file", str(FIXTURE), "--http-port", str(gui_port),
                "--enable-pocketmage", "--pocketmage-bind", "127.0.0.1", "--pocketmage-port", str(pocket_port),
            ], cwd=FIXTURE.parent.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                def ready():
                    if process.poll() is not None:
                        raise AssertionError(process.communicate()[1].decode())
                    try:
                        return request(f"http://127.0.0.1:{pocket_port}", "/v1/status", token=None)
                    except OSError:
                        return None
                code, status = wait_for(ready)
                self.assertEqual(code, 200)
                self.assertEqual(status["phase"], "idle")
                self.assertFalse(status["busy"])
                self.assertEqual(len(status["legal_moves"]), 20)
                self.assertTrue(path.is_file())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

"""Browser API and motion-owner tests without a real Arduino."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chess_table_host.gui import GameController, GuiHTTPServer
from chess_table_host.play import _geometry_from_file, _route_geometry
from chess_table_host.serial_link import LinkError
from chess_table_host.session_lock import SessionLock


FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("timed out waiting for GUI state")


@contextmanager
def game_controller(*, simulate: bool = True):
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "game.json"
        controller = GameController(
            state_path=state, new=True, simulate=simulate,
            geometry_file=FIXTURE if simulate else None,
            serial_port="TEST-COM-GUI", serial_log=state.with_suffix(".serial.jsonl"),
            stockfish_path="unused", engine_time=0.01,
            moving_diameter=18.0, standing_diameter=18.0, margin=0.0,
            allow_tight_routes=True,
        )
        try:
            yield controller
        finally:
            controller.stop()
            worker = controller._worker
            if worker is not None:
                worker.join(timeout=5)
            controller.close()


class GuiTests(unittest.TestCase):
    def test_human_color_gates_engine_turns(self):
        class Engine:
            def play(self, _board, _limit, *, root_moves):
                return SimpleNamespace(move=next(iter(root_moves)))

            def quit(self):
                pass

        with patch("chess_table_host.gui._resolve_engine_path", return_value="fake"), \
             patch("chess_table_host.gui.chess.engine.SimpleEngine.popen_uci", return_value=Engine()):
            with game_controller() as controller:
                controller.start("human_engine", human="white")
                wait_for(lambda: controller.state()["can_move"])
                self.assertEqual(controller.state()["turn"], "white")
                self.assertEqual(controller.state()["moves"], [])
                controller.submit_move("e2e4")
                wait_for(lambda: controller.state()["ply"] >= 2 and controller.state()["can_move"])
                self.assertEqual(controller.state()["turn"], "white")
            with game_controller() as controller:
                controller.start("human_engine", human="black")
                wait_for(lambda: controller.state()["can_move"])
                self.assertEqual(controller.state()["ply"], 1)
                self.assertEqual(controller.state()["turn"], "black")

    def test_http_manual_game_rejects_illegal_move_and_tracks_capture(self):
        with game_controller() as controller:
            with GuiHTTPServer(("127.0.0.1", 0), controller) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"

                def request(path, data=None, origin=None):
                    encoded = json.dumps(data).encode() if data is not None else None
                    headers = {"Content-Type": "application/json"} if encoded is not None else {}
                    if origin:
                        headers["Origin"] = origin
                    req = Request(base + path, data=encoded, headers=headers)
                    try:
                        with urlopen(req, timeout=5) as response:
                            return response.status, json.load(response)
                    except HTTPError as exc:
                        try:
                            return exc.code, json.load(exc)
                        finally:
                            exc.close()

                try:
                    status, state = request("/api/state")
                    self.assertEqual(status, 200)
                    self.assertEqual(state["phase"], "idle")
                    self.assertEqual(state["physical_squares"]["e2"], "P")
                    self.assertIn("e2e4", state["legal_moves"])
                    status, state = request("/api/start", {"mode": "manual_both"}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: request("/api/state")[1]["can_move"])
                    status, body = request("/api/move", {"uci": "e2e5"}, base)
                    self.assertEqual(status, 422)
                    self.assertIn("illegal", body["error"])
                    for ply, uci in enumerate(("e2e4", "d7d5", "e4d5"), 1):
                        wait_for(lambda: request("/api/state")[1]["can_move"])
                        status, _ = request("/api/move", {"uci": uci}, base)
                        self.assertEqual(status, 202)
                        wait_for(lambda: request("/api/state")[1]["ply"] == ply)
                    _, state = request("/api/state")
                    self.assertEqual(state["physical_squares"]["d5"], "P")
                    self.assertIn("p", state["physical_parking"])
                    self.assertEqual(state["moves_san"], ["e4", "d5", "exd5"])
                    with patch("chess_table_host.gui.JsonGameStore.load",
                               side_effect=FileNotFoundError("replacement window")):
                        status, cached = request("/api/state")
                        self.assertEqual(status, 200)
                        self.assertTrue(cached["checkpoint_cached"])
                        self.assertFalse(cached["can_move"])
                        self.assertFalse(cached["can_start"])
                    status, body = request("/api/stop", {}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: not request("/api/state")[1]["busy"])
                    status, body = request("/api/reset-board", {}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: request("/api/state")[1]["ply"] == 0 and
                             not request("/api/state")[1]["busy"])
                    _, state = request("/api/state")
                    self.assertEqual(sum(item is not None for item in state["physical_parking"]), 0)
                    self.assertEqual(len(state["physical_squares"]), 32)
                    self.assertEqual(len(list(Path(controller.store.path).parent.glob("game.before-reset-*.json"))), 1)
                    status, _ = request("/api/start", {"mode": "manual_both"}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: request("/api/state")[1]["can_move"])
                    status, _ = request("/api/move", {"uci": "e2e4"}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: request("/api/state")[1]["ply"] == 1)
                    status, _ = request("/api/end-game-reset", {}, base)
                    self.assertEqual(status, 202)
                    wait_for(lambda: request("/api/state")[1]["ply"] == 0 and
                             not request("/api/state")[1]["busy"])
                    self.assertEqual(len(list(Path(controller.store.path).parent.glob("game.before-reset-*.json"))), 2)
                    status, body = request("/api/start", {"mode": "manual_both"}, "http://evil.example")
                    self.assertEqual(status, 403)
                    original_load = controller.store.load
                    with patch.object(controller.store, "load", side_effect=[
                        FileNotFoundError("brief replacement window"), original_load(),
                    ]):
                        self.assertEqual(controller.state()["ply"], 0)
                    controller.store._last_save_at = time.monotonic() - 3.0
                    with patch("chess_table_host.gui.JsonGameStore.load",
                               side_effect=FileNotFoundError("missing")):
                        status, body = request("/api/state")
                        self.assertEqual(status, 503)
                        self.assertIn("temporarily unavailable", body["error"])
                finally:
                    server.shutdown()
                    thread.join(timeout=5)

    def test_stop_interrupts_physical_carry_and_requires_reconciliation(self):
        class BlockingLink:
            def __init__(self):
                self.started = threading.Event()
                self.stopped = threading.Event()

            def set_map(self, _bitmap):
                pass

            def carry(self, *_arguments):
                self.started.set()
                self.stopped.wait(timeout=5)
                raise LinkError("motion stopped")

            def stop(self):
                self.stopped.set()

            def close(self):
                pass

        with game_controller(simulate=False) as controller:
            link = BlockingLink()
            controller.link = link
            geometry = _geometry_from_file(FIXTURE)
            controller.geometry = _route_geometry(geometry, 18.0, 18.0, 0.0)
            controller.start("manual_both")
            wait_for(lambda: controller.state()["can_move"])
            controller.submit_move("e2e4")
            self.assertTrue(link.started.wait(timeout=5))
            wait_for(lambda: controller.state()["pending"] is not None and
                     controller.state()["pending"]["phase"] == "in_flight")
            controller.end_game_and_reset()
            self.assertTrue(controller.state()["reset_requested"])
            controller.stop()
            self.assertTrue(link.stopped.wait(timeout=5))
            wait_for(lambda: not controller.state()["busy"])
            self.assertFalse(controller.state()["reset_requested"])
            self.assertIsNone(controller.state()["reset"])
            self.assertFalse(list(Path(controller.store.path).parent.glob("game.before-reset-*.json")))
            self.assertEqual(controller.state()["phase"], "reconciliation_required")
            self.assertEqual(controller.state()["moves"], [])
            with self.assertRaisesRegex(ValueError, "confirm inspection"):
                controller.reconcile("source", False)
            controller.reconcile("source", True)
            self.assertEqual(controller.state()["pending"]["phase"], "ready")
            self.assertEqual(controller.state()["physical_squares"]["e2"], "P")

    def test_end_game_during_carry_finishes_move_then_resets_and_archives(self):
        class HeldLink:
            def __init__(self):
                self.started = threading.Event()
                self.release = threading.Event()
                self.stop_calls = 0

            def set_map(self, _bitmap):
                pass

            def carry(self, *_arguments):
                if not self.started.is_set():
                    self.started.set()
                    self.release.wait(timeout=5)

            def stop(self):
                self.stop_calls += 1

            def close(self):
                pass

        with game_controller(simulate=False) as controller:
            link = HeldLink()
            controller.link = link
            controller.geometry = _route_geometry(_geometry_from_file(FIXTURE), 18.0, 18.0, 0.0)
            controller.start("manual_both")
            wait_for(lambda: controller.state()["can_move"])
            controller.submit_move("e2e4")
            self.assertTrue(link.started.wait(timeout=5))
            controller.end_game_and_reset()
            ending = controller.state()
            self.assertEqual(ending["phase"], "ending_game")
            self.assertTrue(ending["reset_requested"])
            self.assertFalse(ending["can_move"])
            self.assertFalse(ending["can_end_and_reset"])
            link.release.set()
            finished = wait_for(lambda: (
                state if (state := controller.state())["ply"] == 0
                and not state["busy"] and state["mode"] is None else None
            ), timeout=10)
            self.assertIsNone(finished["error"])
            self.assertEqual(len(finished["physical_squares"]), 32)
            self.assertFalse(any(finished["physical_parking"]))
            self.assertEqual(link.stop_calls, 0)
            archives = list(Path(controller.store.path).parent.glob("game.before-reset-*.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text())["moves"], ["e2e4"])

    def test_end_game_from_human_turn_resets_without_waiting_for_a_move(self):
        with game_controller() as controller:
            controller.start("manual_both")
            wait_for(lambda: controller.state()["can_move"])
            controller.end_game_and_reset()
            finished = wait_for(lambda: (
                state if (state := controller.state())["ply"] == 0
                and not state["busy"] and state["mode"] is None else None
            ))
            self.assertIsNone(finished["error"])
            self.assertFalse(finished["reset_requested"])

    def test_end_game_after_paused_move_finishes_it_before_reset(self):
        with game_controller() as controller:
            _link, geometry = controller._connection()
            controller.runner.prepare_move("e2e4", geometry)
            self.assertEqual(controller.state()["pending"]["phase"], "ready")
            self.assertTrue(controller.state()["can_end_and_reset"])
            controller.end_game_and_reset()
            finished = wait_for(lambda: (
                state if (state := controller.state())["ply"] == 0
                and not state["busy"] and state["pending"] is None else None
            ))
            self.assertIsNone(finished["error"])
            archives = list(Path(controller.store.path).parent.glob("game.before-reset-*.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text())["moves"], ["e2e4"])

    def test_second_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "game.json"
            with SessionLock(state, "TEST-COM-LOCK"):
                with self.assertRaisesRegex(RuntimeError, "another process owns"):
                    with SessionLock(state, "TEST-COM-LOCK"):
                        pass


if __name__ == "__main__":
    unittest.main()

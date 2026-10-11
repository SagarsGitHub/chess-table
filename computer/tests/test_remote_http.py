"""Authenticated remote control, durable receipts, and restart safety."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import chess

from chess_table_host.gui import GameController
from chess_table_host.checkpoint import GameState, JsonGameStore, STATE_VERSION
from chess_table_host.game import GameRunner, GameStateError
from chess_table_host.gui_http import GuiHTTPServer
from chess_table_host.link_adapters import SimulatedLink
from chess_table_host.play import _geometry_from_file, _route_geometry
from chess_table_host.remote_http import RemoteHTTPServer, read_remote_token
from chess_table_host.serial_link import LinkError


FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"
TOKEN = "a-remote-test-token-with-more-than-32-characters"


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("timed out waiting for remote state")


class HeldLink:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.stopped = threading.Event()
        self.carries = 0

    def set_map(self, _bitmap):
        pass

    def carry(self, *_arguments):
        self.carries += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise LinkError("test carry timed out")
        if self.stopped.is_set():
            raise LinkError("motion stopped")
        return SimpleNamespace(command_id=self.carries)

    def carry_offset(self, *_arguments):
        return self.carry(*_arguments)

    def offset(self, *_arguments):
        return self.carry(*_arguments)

    def stop(self):
        self.stopped.set()
        self.release.set()

    def close(self):
        self.release.set()


def controller_for(path: Path, *, new: bool, simulate: bool, link: HeldLink | None = None):
    controller = GameController(
        state_path=path, new=new, simulate=simulate,
        geometry_file=FIXTURE if simulate else None,
        serial_port="TEST-COM-REMOTE", serial_log=path.with_suffix(".serial.jsonl"),
        stockfish_path="unused", engine_time=0.01,
        moving_diameter=18.0, standing_diameter=18.0, margin=0.0,
        allow_tight_routes=True,
    )
    if link is not None:
        controller.link = link
        controller.geometry = _route_geometry(_geometry_from_file(FIXTURE), 18.0, 18.0, 0.0)
    return controller


@contextmanager
def http_server(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@contextmanager
def remote_server(controller: GameController):
    with http_server(RemoteHTTPServer(("127.0.0.1", 0), controller, TOKEN)) as base:
        yield base


@contextmanager
def gui_server(controller: GameController):
    with http_server(GuiHTTPServer(("127.0.0.1", 0), controller)) as base:
        yield base


def request(base: str, path: str, body=None, *, token: str | None = TOKEN, origin=None):
    encoded = json.dumps(body).encode() if body is not None else None
    headers = {}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    req = Request(base + path, data=encoded, headers=headers)
    try:
        with urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        finally:
            exc.close()


def start_body(status):
    return {
        "host_game_id": status["host_game_id"],
        "source_kind": "aina",
        "source_game_id": "completed-game-123",
        "request_id": "session-start-1",
    }


def move_body(status):
    return {
        "request_id": "completed-game-123-ply-1",
        "host_game_id": status["host_game_id"],
        "source_game_id": "completed-game-123",
        "expected_ply": 0,
        "expected_fen": status["fen"],
        "uci": "e2e4",
    }


class OpeningEngine:
    """Deterministic legal engine replies, restricted to host-preflighted moves."""

    def play(self, board, _limit, *, root_moves):
        preferred = ("e2e4", "g1f3", "b1c3") if board.turn else ("e7e5", "g8f6", "b8c6")
        for uci in preferred:
            move = chess.Move.from_uci(uci)
            if move in root_moves:
                return SimpleNamespace(move=move)
        return SimpleNamespace(move=root_moves[0])

    def quit(self):
        pass


@contextmanager
def fake_engine():
    with patch("chess_table_host.gui._resolve_engine_path", return_value="fake"), \
         patch("chess.engine.SimpleEngine.popen_uci", side_effect=lambda _path: OpeningEngine()):
        yield


def waiting_status(base: str, *, ply: int | None = None):
    return wait_for(lambda: (
        status if (status := request(base, "/v1/status")[1])["can_move"]
        and (ply is None or status["ply"] == ply) else None
    ))


def stop_remote(base: str, controller: GameController):
    code, _ = request(base, "/v1/stop", {})
    if code != 202:
        raise AssertionError(f"STOP returned {code}")
    wait_for(lambda: not controller.remote_status()["busy"])


def close_controller(controller: GameController):
    """Release a test worker even when an HTTP assertion fails."""
    controller.stop()
    worker = controller._worker
    if worker is not None:
        worker.join(timeout=5)
    controller.close()


class RemoteHTTPTests(unittest.TestCase):
    def test_reset_requires_authentication_current_uuid_and_exact_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            try:
                with remote_server(controller) as base:
                    status = request(base, "/v1/status")[1]
                    body = {"host_game_id": status["host_game_id"]}
                    before = path.read_bytes()
                    for token in (None, "incorrect-token"):
                        with self.subTest(token=token):
                            self.assertEqual(request(base, "/v1/session/reset", body,
                                                     token=token)[0], 401)
                    self.assertEqual(request(base, "/v1/session/reset", body,
                                             origin=base)[0], 403)
                    malformed = ({}, {"host_game_id": None}, {"host_game_id": False},
                                 {"host_game_id": 1}, {"host_game_id": "not-a-uuid"},
                                 {"host_game_id": str(uuid4()).replace("-", "")},
                                 {"host_game_id": "{" + status["host_game_id"] + "}"},
                                 dict(body, request_id="reset-1"))
                    for invalid in malformed:
                        with self.subTest(body=invalid):
                            self.assertEqual(request(base, "/v1/session/reset", invalid)[0], 400)
                    code, error = request(base, "/v1/session/reset",
                                          {"host_game_id": str(uuid4())})
                    self.assertEqual(code, 409)
                    self.assertIn("current game", error["error"]["message"])
                    self.assertEqual(path.read_bytes(), before)
                    self.assertIsNone(controller._worker)
                    self.assertIsNone(controller.link)
            finally:
                close_controller(controller)

    def test_reset_from_waiting_empty_game_clears_binding_and_rejects_stale_retry(self):
        for mode in ("external", "human_engine"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, fake_engine():
                path = Path(directory) / "game.json"
                controller = controller_for(path, new=True, simulate=True)
                try:
                    with remote_server(controller) as base:
                        initial = request(base, "/v1/status")[1]
                        start = dict(start_body(initial), mode=mode, human="white")
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        waiting_status(base, ply=0)
                        body = {"host_game_id": initial["host_game_id"]}
                        code, accepted = request(base, "/v1/session/reset", body)
                        self.assertEqual(code, 202)
                        self.assertFalse(accepted["can_move"])
                        reset = wait_for(lambda: (
                            value if (value := request(base, "/v1/status")[1])["phase"] == "idle"
                            and not value["busy"] and value["host_game_id"] != body["host_game_id"]
                            else None
                        ))
                        self.assertIsNone(reset["source_kind"])
                        self.assertIsNone(reset["source_game_id"])
                        self.assertEqual(reset["ply"], 0)
                        self.assertEqual(reset["fen"], chess.Board().fen())
                        self.assertIsNone(reset["pending"])
                        self.assertIsNone(reset["error"])
                        before = path.read_bytes()
                        self.assertEqual(request(base, "/v1/session/reset", body)[0], 409)
                        self.assertEqual(path.read_bytes(), before)
                        self.assertIsNone(controller._worker)
                finally:
                    close_controller(controller)

    def test_reset_during_carry_waits_for_commit_and_reuses_reset_worker(self):
        class TwoHeldCarries(HeldLink):
            def __init__(self):
                super().__init__()
                self.reset_entered = threading.Event()
                self.reset_release = threading.Event()

            def carry(self, *_arguments):
                self.carries += 1
                entered = self.entered if self.carries == 1 else self.reset_entered
                release = self.release if self.carries == 1 else self.reset_release
                entered.set()
                if not release.wait(timeout=5):
                    raise LinkError("test carry timed out")
                return SimpleNamespace(command_id=self.carries)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = TwoHeldCarries()
            controller = controller_for(path, new=True, simulate=True, link=link)
            try:
                with remote_server(controller) as base:
                    initial = request(base, "/v1/status")[1]
                    request(base, "/v1/session/start", start_body(initial))
                    move = move_body(waiting_status(base, ply=0))
                    self.assertEqual(request(base, "/v1/moves", move)[0], 202)
                    self.assertTrue(link.entered.wait(timeout=5))
                    game_worker = controller._worker
                    body = {"host_game_id": initial["host_game_id"]}
                    code, accepted = request(base, "/v1/session/reset", body)
                    self.assertEqual(code, 202)
                    self.assertEqual(accepted["phase"], "resetting")
                    self.assertFalse(accepted["can_move"])
                    self.assertEqual(request(base, "/v1/session/reset", body)[0], 202)
                    self.assertIs(controller._worker, game_worker)
                    self.assertEqual(link.carries, 1)
                    before_done = controller.store.load()
                    self.assertEqual(before_done.moves, [])
                    self.assertEqual(before_done.pending.phase, "in_flight")
                    self.assertIsNone(before_done.reset)
                    self.assertFalse(list(path.parent.glob("game.before-reset-*.json")))

                    link.release.set()
                    self.assertTrue(link.reset_entered.wait(timeout=5))
                    reset_worker = controller._worker
                    self.assertIsNot(reset_worker, game_worker)
                    resetting = controller.store.load()
                    self.assertEqual(resetting.moves, ["e2e4"])
                    self.assertIsNone(resetting.pending)
                    self.assertIsNotNone(resetting.reset)
                    self.assertEqual(resetting.remote_receipts[move["request_id"]].state, "committed")
                    archives = list(path.parent.glob("game.before-reset-*.json"))
                    self.assertEqual(len(archives), 1)
                    archived = JsonGameStore(archives[0]).load()
                    self.assertEqual(archived.moves, ["e2e4"])
                    self.assertEqual(archived.host_game_id, body["host_game_id"])
                    code, duplicate = request(base, "/v1/session/reset", body)
                    self.assertEqual(code, 202)
                    self.assertEqual(duplicate["phase"], "resetting")
                    self.assertIs(controller._worker, reset_worker)
                    self.assertEqual(link.carries, 2)
                    link.reset_release.set()
                    wait_for(lambda: not controller.remote_status()["busy"])
                    reset = request(base, "/v1/status")[1]
                    self.assertEqual(reset["phase"], "idle")
                    self.assertEqual(reset["fen"], chess.Board().fen())
                    self.assertNotEqual(reset["host_game_id"], body["host_game_id"])
                    self.assertIsNone(reset["source_kind"])
                    self.assertEqual(controller.store.load().remote_receipts, {})
            finally:
                link.release.set()
                link.reset_release.set()
                close_controller(controller)

    def test_reset_rejects_uncertain_saved_reset_without_opening_controller(self):
        class InterruptedReset(SimulatedLink):
            def carry(self, *_arguments):
                raise LinkError("simulated interrupted reset")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            store = JsonGameStore(path)
            runner = GameRunner(GameState.new(), store, allow_tight_routes=True)
            geometry = _route_geometry(_geometry_from_file(FIXTURE), 18.0, 18.0, 0.0)
            store.save(runner.state)
            runner.prepare_move("e2e4", geometry)
            runner.execute_pending(SimulatedLink(), geometry, lambda _action: True)
            runner.prepare_reset(geometry)
            with self.assertRaises(LinkError):
                runner.execute_reset(InterruptedReset(), geometry, lambda _action: True)
            controller = controller_for(path, new=False, simulate=True)
            try:
                with remote_server(controller) as base:
                    status = request(base, "/v1/status")[1]
                    self.assertEqual(status["phase"], "reconciliation_required")
                    before = path.read_bytes()
                    code, error = request(base, "/v1/session/reset",
                                          {"host_game_id": status["host_game_id"]})
                    self.assertEqual(code, 409)
                    self.assertIn("reconcile", error["error"]["message"])
                    self.assertEqual(path.read_bytes(), before)
                    self.assertIsNone(controller._worker)
                    self.assertIsNone(controller.link)
            finally:
                close_controller(controller)

    def test_cli_creates_first_checkpoint_with_remote_listener(self):
        # Exercise the exact remote options used by the Linux launcher, with
        # simulated hardware and an initially absent checkpoint directory.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs" / "boot-physical.json"
            token_path = Path(directory) / "token"
            token_path.write_text(TOKEN, encoding="ascii")
            with socket.socket() as gui_socket, socket.socket() as remote_socket:
                gui_socket.bind(("127.0.0.1", 0))
                remote_socket.bind(("127.0.0.1", 0))
                gui_port = gui_socket.getsockname()[1]
                remote_port = remote_socket.getsockname()[1]
            process = subprocess.Popen([
                sys.executable, "-m", "chess_table_host.gui", "--new",
                "--state", str(path), "--simulate", "--geometry-file", str(FIXTURE),
                "--http-port", str(gui_port), "--remote-token-file", str(token_path),
                "--remote-http-port", str(remote_port),
            ], cwd=FIXTURE.parent.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                def ready():
                    if process.poll() is not None:
                        raise AssertionError(process.communicate()[1].decode())
                    try:
                        return request(f"http://127.0.0.1:{remote_port}", "/v1/status")
                    except OSError:
                        return None
                code, status = wait_for(ready)
                self.assertEqual(code, 200)
                self.assertEqual(status["phase"], "idle")
                self.assertFalse(status["can_move"])
                self.assertTrue(path.is_file())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=10)

    def test_authentication_and_local_route_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            try:
                with remote_server(controller) as base:
                    self.assertEqual(request(base, "/v1/status", token=None)[0], 401)
                    self.assertEqual(request(base, "/v1/status", token="wrong")[0], 401)
                    self.assertEqual(request(base, "/v1/status", origin="https://elsewhere.invalid")[0], 403)
                    with self.assertRaises(HTTPError) as unauthenticated:
                        urlopen(Request(base + "/v1/status", method="OPTIONS"), timeout=5)
                    self.assertEqual(unauthenticated.exception.code, 401)
                    unauthenticated.exception.close()
                    self.assertEqual(request(base, "/api/state")[0], 404)
                    self.assertEqual(request(base, "/v1/reconcile", {})[0], 404)
                    status_code, status = request(base, "/v1/status")
                    self.assertEqual(status_code, 200)
                    self.assertEqual(status["schema_version"], 1)
                    self.assertFalse(status["can_move"])
                    with patch.object(controller, "remote_status",
                                      side_effect=OSError("private checkpoint path")):
                        code, error = request(base, "/v1/status")
                    self.assertEqual(code, 503)
                    self.assertNotIn("private checkpoint path", json.dumps(error))
                    controller._error = "private checkpoint path"
                    error_status = request(base, "/v1/status")[1]
                    self.assertEqual(error_status["phase"], "error")
                    self.assertEqual(error_status["error"], "host operation failed")
                    self.assertNotIn("private checkpoint path", json.dumps(error_status))
            finally:
                controller.close()

    def test_worker_connection_failure_is_reported_without_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            controller.geometry_file = Path(directory) / "missing-private-geometry.json"
            try:
                with remote_server(controller) as base:
                    _, status = request(base, "/v1/status")
                    request(base, "/v1/session/start", start_body(status))
                    failed = wait_for(lambda: (
                        value if (value := request(base, "/v1/status")[1])["phase"] == "error"
                        else None
                    ))
                    self.assertFalse(failed["can_move"])
                    self.assertEqual(failed["error"], "host operation failed")
                    self.assertNotIn("missing-private-geometry.json", json.dumps(failed))
            finally:
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

    def test_move_receipt_is_saved_before_motion_and_retries_never_repeat_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = HeldLink()
            controller = controller_for(path, new=True, simulate=True, link=link)
            try:
                with remote_server(controller) as base:
                    _, status = request(base, "/v1/status")
                    self.assertIsNone(status["last_move_uci"])
                    start = start_body(status)
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    _, status = request(base, "/v1/status")
                    body = move_body(status)
                    code, receipt = request(base, "/v1/moves", body)
                    self.assertEqual(code, 202)
                    self.assertTrue(link.entered.wait(timeout=5))
                    saved = controller.store.load()
                    self.assertEqual(saved.pending.remote_request_id, body["request_id"])
                    self.assertIn(body["request_id"], saved.remote_receipts)
                    self.assertIn(receipt["state"], {"prepared", "in_flight"})
                    self.assertEqual(request(base, "/v1/moves", body)[0], 202)
                    changed = dict(body, uci="d2d4")
                    self.assertEqual(request(base, "/v1/moves", changed)[0], 409)
                    self.assertEqual(request(base, "/v1/moves/unknown")[0], 404)
                    link.release.set()
                    committed = wait_for(lambda: (
                        value if (value := request(base, "/v1/moves/" + body["request_id"])[1])
                        ["state"] == "committed" else None
                    ))
                    self.assertEqual(committed["uci"], "e2e4")
                    self.assertEqual(request(base, "/v1/moves", body)[0], 200)
                    self.assertEqual(link.carries, 1)
                    self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                    wait_for(lambda: not controller.remote_status()["busy"])
            finally:
                link.release.set()
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

            resumed = controller_for(path, new=False, simulate=True)
            try:
                with remote_server(resumed) as base:
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"],
                                     "committed")
                    self.assertEqual(request(base, "/v1/moves", body)[0], 200)
                    resumed_status = request(base, "/v1/status")[1]
                    self.assertEqual(resumed_status["ply"], 1)
                    self.assertEqual(resumed_status["last_move_uci"], "e2e4")
                    with self.assertRaisesRegex(ValueError, "remote-bound"):
                        resumed.start("human_engine")
                    with self.assertRaisesRegex(ValueError, "remote-bound"):
                        resumed.start("manual_both")
            finally:
                resumed.close()

    def test_uncertain_carry_blocks_new_moves_and_abort_keeps_tombstone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = HeldLink()
            controller = controller_for(path, new=True, simulate=False, link=link)
            try:
                with remote_server(controller) as base:
                    _, status = request(base, "/v1/status")
                    request(base, "/v1/session/start", start_body(status))
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    _, status = request(base, "/v1/status")
                    body = move_body(status)
                    self.assertEqual(request(base, "/v1/moves", body)[0], 202)
                    self.assertTrue(link.entered.wait(timeout=5))
                    request(base, "/v1/stop", {})
                    wait_for(lambda: not controller.remote_status()["busy"])
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"],
                                     "reconciliation_required")
                    second = dict(body, request_id="another-move")
                    self.assertEqual(request(base, "/v1/moves", second)[0], 409)
                    checkpoint = path.read_bytes()
                    code, error = request(base, "/v1/session/reset",
                                          {"host_game_id": status["host_game_id"]})
                    self.assertEqual(code, 409)
                    self.assertIn("reconcile", error["error"]["message"])
                    self.assertEqual(path.read_bytes(), checkpoint)
                    self.assertIsNone(controller._worker)
                    self.assertEqual(link.carries, 1)
            finally:
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

            resumed = controller_for(path, new=False, simulate=True)
            try:
                with remote_server(resumed) as base:
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"],
                                     "reconciliation_required")
                    self.assertEqual(request(base, "/v1/moves", second)[0], 409)
                    resumed.reconcile("abort", True)
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[1]["state"],
                                     "aborted")
                    self.assertEqual(request(base, "/v1/moves", body)[0], 200)
            finally:
                resumed.close()

    def test_concurrent_duplicate_waits_for_one_checkpointed_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = HeldLink()
            controller = controller_for(path, new=True, simulate=True, link=link)
            entered = threading.Event()
            release_preflight = threading.Event()
            original_prepare = controller.runner.prepare_move

            def delayed_prepare(*args, **kwargs):
                entered.set()
                if not release_preflight.wait(timeout=5):
                    raise AssertionError("test preflight was not released")
                return original_prepare(*args, **kwargs)

            try:
                with remote_server(controller) as base, \
                     patch.object(controller.runner, "prepare_move", side_effect=delayed_prepare):
                    _, status = request(base, "/v1/status")
                    request(base, "/v1/session/start", start_body(status))
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    body = move_body(request(base, "/v1/status")[1])
                    responses: list[tuple[int, dict]] = []

                    def post():
                        responses.append(request(base, "/v1/moves", body))

                    first = threading.Thread(target=post)
                    second = threading.Thread(target=post)
                    first.start()
                    self.assertTrue(entered.wait(timeout=5))
                    second.start()
                    release_preflight.set()
                    first.join(timeout=5)
                    second.join(timeout=5)
                    self.assertEqual(len(responses), 2)
                    self.assertEqual({item[1]["request_id"] for item in responses},
                                     {body["request_id"]})
                    self.assertTrue(all(code == 202 for code, _ in responses))
                    self.assertTrue(link.entered.wait(timeout=5))
                    self.assertEqual(link.carries, 1)
                    self.assertEqual(len(controller.store.load().remote_receipts), 1)
                    link.release.set()
                    wait_for(lambda: controller.remote_receipt(body["request_id"])["state"]
                             == "committed")
            finally:
                release_preflight.set()
                link.release.set()
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

    def test_stop_during_claimed_preflight_aborts_without_replay_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            link = HeldLink()
            controller = controller_for(path, new=True, simulate=True, link=link)
            entered = threading.Event()
            release_preflight = threading.Event()
            original_prepare = controller.runner.prepare_move

            def delayed_prepare(*args, **kwargs):
                entered.set()
                if not release_preflight.wait(timeout=5):
                    raise AssertionError("test preflight was not released")
                return original_prepare(*args, **kwargs)

            try:
                with remote_server(controller) as base, \
                     patch.object(controller.runner, "prepare_move", side_effect=delayed_prepare):
                    _, status = request(base, "/v1/status")
                    start = start_body(status)
                    request(base, "/v1/session/start", start)
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    body = move_body(request(base, "/v1/status")[1])
                    responses: list[tuple[int, dict]] = []
                    caller = threading.Thread(
                        target=lambda: responses.append(request(base, "/v1/moves", body)),
                    )
                    caller.start()
                    self.assertTrue(entered.wait(timeout=5))
                    controller.stop()
                    release_preflight.set()
                    caller.join(timeout=5)
                    self.assertEqual(len(responses), 1)
                    wait_for(lambda: not controller.remote_status()["busy"])
                    saved = controller.store.load()
                    self.assertIsNone(saved.pending)
                    self.assertEqual(saved.remote_receipts[body["request_id"]].state, "aborted")
                    self.assertEqual(link.carries, 0)
                    self.assertEqual(request(base, "/v1/moves", body)[1]["state"], "aborted")

                    request(base, "/v1/session/start", start)
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    self.assertEqual(link.carries, 0)
                    request(base, "/v1/stop", {})
                    wait_for(lambda: not controller.remote_status()["busy"])
            finally:
                release_preflight.set()
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

    def test_token_file_and_bind_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            with self.assertRaisesRegex(ValueError, "cannot read"):
                read_remote_token(token_file)
            token_file.write_text("short\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "32"):
                read_remote_token(token_file)
            token_file.write_text(TOKEN + "\n", encoding="utf-8")
            self.assertEqual(read_remote_token(token_file), TOKEN)
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with self.assertRaisesRegex(ValueError, "127.0.0.1"):
                    RemoteHTTPServer(("0.0.0.0", 0), controller, TOKEN)
            finally:
                controller.close()

    def test_reset_creates_new_identity_and_old_request_cannot_move_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            try:
                with remote_server(controller) as base:
                    _, status = request(base, "/v1/status")
                    request(base, "/v1/session/start", start_body(status))
                    wait_for(lambda: request(base, "/v1/status")[1]["can_move"])
                    _, status = request(base, "/v1/status")
                    body = move_body(status)
                    self.assertIn(request(base, "/v1/moves", body)[0], {200, 202})
                    wait_for(lambda: request(base, "/v1/moves/" + body["request_id"])[1]
                             ["state"] == "committed")
                    request(base, "/v1/stop", {})
                    wait_for(lambda: not controller.remote_status()["busy"])
                    controller.reset_board()
                    reset = wait_for(lambda: (
                        value if (value := request(base, "/v1/status")[1])["ply"] == 0
                        and not value["busy"] and value["host_game_id"] != body["host_game_id"]
                        else None
                    ))
                    self.assertIsNone(reset["source_game_id"])
                    self.assertEqual(request(base, "/v1/moves", body)[0], 409)
                    self.assertEqual(request(base, "/v1/moves/" + body["request_id"])[0], 404)
            finally:
                controller.stop()
                worker = controller._worker
                if worker is not None:
                    worker.join(timeout=5)
                controller.close()

    def test_v4_pending_move_and_reset_migrate_without_losing_physical_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            geometry = _route_geometry(_geometry_from_file(FIXTURE), 18.0, 18.0, 0.0)
            for kind in ("move", "reset"):
                path = Path(directory) / f"{kind}.json"
                store = JsonGameStore(path)
                runner = GameRunner(GameState.new(), store, allow_tight_routes=True)
                store.save(runner.state)
                runner.prepare_move("e2e4", geometry)
                if kind == "reset":
                    runner.execute_pending(SimulatedLink(), geometry, lambda _action: True)
                    runner.prepare_reset(geometry)
                saved = json.loads(path.read_text(encoding="utf-8"))
                saved["version"] = 4
                for key in ("host_game_id", "source_kind", "source_game_id",
                            "remote_start_request_id", "remote_receipts"):
                    saved.pop(key)
                if saved["pending"] is not None:
                    saved["pending"].pop("remote_request_id", None)
                path.write_text(json.dumps(saved), encoding="utf-8")
                controller = controller_for(path, new=False, simulate=True)
                try:
                    loaded = controller.store.load()
                    self.assertEqual(loaded.board_fen, saved["board_fen"])
                    self.assertEqual(loaded.physical_squares, saved["physical_squares"])
                    self.assertEqual(loaded.physical_parking, saved["physical_parking"])
                    self.assertIsNotNone(loaded.pending if kind == "move" else loaded.reset)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], STATE_VERSION)
                    damaged = json.loads(path.read_text(encoding="utf-8"))
                    damaged.pop("host_game_id")
                    with self.assertRaises(GameStateError):
                        GameState.from_json(damaged)
                finally:
                    controller.close()


class RemoteGameOptionsTests(unittest.TestCase):
    def test_missing_engine_returns_private_path_free_error_without_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            controller.stockfish_path = str(Path(directory) / "private-engine-directory" / "missing-stockfish")
            try:
                with remote_server(controller) as base:
                    initial = request(base, "/v1/status")[1]
                    checkpoint = path.read_bytes()
                    start = dict(start_body(initial), mode="human_engine", human="black")
                    code, error = request(base, "/v1/session/start", start)
                    self.assertEqual(code, 503)
                    self.assertEqual(error["error"]["code"], "unavailable")
                    self.assertEqual(error["error"]["message"],
                                     "Stockfish unavailable; check host engine configuration")
                    self.assertNotIn(directory, json.dumps(error))
                    self.assertNotIn("private-engine-directory", json.dumps(error))
                    self.assertEqual(path.read_bytes(), checkpoint)
                    unchanged = request(base, "/v1/status")[1]
                    self.assertIsNone(unchanged["source_game_id"])
                    self.assertFalse(unchanged["busy"])
                    self.assertEqual(unchanged["fen"], initial["fen"])
                    self.assertIsNone(controller.link)
            finally:
                close_controller(controller)

    def test_bound_engine_game_rejects_gui_moves_and_preserves_remote_control(self):
        for human in ("white", "black"):
            with self.subTest(human=human), tempfile.TemporaryDirectory() as directory, fake_engine():
                path = Path(directory) / "game.json"
                controller = controller_for(path, new=True, simulate=True)
                initial_ply = 0 if human == "white" else 1
                uci = "e2e4" if human == "white" else "e7e5"
                try:
                    with remote_server(controller) as base, gui_server(controller) as browser:
                        start = dict(start_body(request(base, "/v1/status")[1]),
                                     mode="human_engine", human=human)
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        status = waiting_status(base, ply=initial_ply)
                        checkpoint = path.read_bytes()
                        browser_state = request(browser, "/api/state", token=None)[1]
                        self.assertFalse(browser_state["can_move"])
                        self.assertTrue(status["can_move"])
                        code, error = request(browser, "/api/move", {"uci": uci}, token=None)
                        self.assertEqual(code, 409)
                        self.assertIn("remote-bound", error["error"])
                        self.assertEqual(path.read_bytes(), checkpoint)
                        self.assertTrue(controller._moves.empty())
                        self.assertIsNone(controller._remote_ticket)
                        self.assertEqual(controller.store.load().remote_receipts, {})
                        self.assertEqual(request(base, "/v1/status")[1]["ply"], initial_ply)

                        code, payload = request(base, "/v1/moves/resolve", {
                            "request_id": "remote-after-gui-refusal", "expression": uci,
                        })
                        self.assertEqual(code, 200)
                        move = dict(payload, request_id="remote-after-gui-refusal")
                        self.assertIn(request(base, "/v1/moves", move)[0], {200, 202})
                        wait_for(lambda: request(base, "/v1/moves/remote-after-gui-refusal")[1]
                                 ["state"] == "committed")
                        waiting_status(base, ply=initial_ply + 2)
                        self.assertFalse(request(browser, "/api/state", token=None)[1]["can_move"])
                        stop_remote(base, controller)
                finally:
                    close_controller(controller)

    def test_invalid_session_options_return_json_without_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with remote_server(controller) as base:
                    initial = request(base, "/v1/status")[1]
                    for field in ("mode", "human"):
                        for value in ([], {}, None, True, 1, "unknown"):
                            with self.subTest(field=field, value=value):
                                body = dict(start_body(initial), **{field: value})
                                code, error = request(base, "/v1/session/start", body)
                                self.assertEqual(code, 400)
                                self.assertEqual(error["error"]["code"], "bad_request")
                                self.assertIsInstance(error["error"]["message"], str)
                    unchanged = request(base, "/v1/status")[1]
                    self.assertEqual(unchanged["host_game_id"], initial["host_game_id"])
                    self.assertEqual(unchanged["fen"], initial["fen"])
                    self.assertIsNone(unchanged["source_game_id"])
                    self.assertFalse(unchanged["busy"])
            finally:
                close_controller(controller)

    def test_resolution_errors_and_position_fences_over_http(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with remote_server(controller) as base:
                    start = start_body(request(base, "/v1/status")[1])
                    self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                    status = waiting_status(base, ply=0)
                    for expression in ("pawn to e5", "Queen 2D3", "2d3", "castle kingside"):
                        with self.subTest(expression=expression):
                            code, error = request(base, "/v1/moves/resolve", {
                                "request_id": "invalid-expression", "expression": expression,
                            })
                            self.assertEqual(code, 422)
                            self.assertEqual(error["error"]["code"], "invalid_move")
                    code, payload = request(base, "/v1/moves/resolve", {
                        "request_id": "spoken-d4", "expression": "D2, D4",
                    })
                    self.assertEqual(code, 200)
                    self.assertEqual(payload, {
                        "host_game_id": status["host_game_id"],
                        "source_game_id": status["source_game_id"],
                        "expected_ply": 0, "expected_fen": status["fen"], "uci": "d2d4",
                    })
                    for field, value in (("host_game_id", "other-host"),
                                         ("source_game_id", "other-source"),
                                         ("expected_ply", 1),
                                         ("expected_fen", chess.Board().fen().replace("w KQ", "b KQ"))):
                        with self.subTest(stale_field=field):
                            body = dict(payload, request_id=f"wrong-{field}", **{field: value})
                            code, error = request(base, "/v1/moves", body)
                            self.assertEqual(code, 409)
                            self.assertEqual(error["error"]["code"], "conflict")
                    self.assertEqual(request(base, "/v1/status")[1]["ply"], 0)
                    self.assertEqual(controller.store.load().remote_receipts, {})
                    stop_remote(base, controller)
            finally:
                close_controller(controller)

    def test_session_resolution_and_receipt_survive_restart_for_each_game_mode(self):
        for mode, human in (("external", "white"), ("human_engine", "white"),
                            ("human_engine", "black")):
            with self.subTest(mode=mode, human=human), \
                 tempfile.TemporaryDirectory() as directory, fake_engine():
                path = Path(directory) / "game.json"
                initial_ply = 1 if mode == "human_engine" and human == "black" else 0
                committed_ply = initial_ply + (2 if mode == "human_engine" else 1)
                expression = "pawn to e5" if human == "black" else "pawn to e4"
                resolved_body = {"request_id": "spoken-1", "expression": expression}
                controller = controller_for(path, new=True, simulate=True)
                try:
                    with remote_server(controller) as base:
                        start = dict(start_body(request(base, "/v1/status")[1]), mode=mode, human=human)
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        status = waiting_status(base, ply=initial_ply)
                        self.assertEqual(status["mode"], mode)
                        self.assertEqual(status["human"], human)
                        code, payload = request(base, "/v1/moves/resolve", resolved_body)
                        self.assertEqual(code, 200)
                        self.assertEqual(payload["expected_ply"], initial_ply)
                        self.assertEqual(payload["uci"], "e7e5" if human == "black" else "e2e4")
                        saved = controller.store.load()
                        self.assertEqual(saved.remote_mode, mode)
                        self.assertEqual(saved.remote_human, human)
                        self.assertEqual(saved.remote_resolutions["spoken-1"].payload, payload)
                        self.assertEqual(saved.remote_receipts, {})
                        self.assertEqual(len(saved.moves), initial_ply)
                        stop_remote(base, controller)
                finally:
                    close_controller(controller)

                resumed = controller_for(path, new=False, simulate=True)
                try:
                    with remote_server(resumed) as base:
                        status = request(base, "/v1/status")[1]
                        self.assertFalse(status["busy"])
                        self.assertEqual((status["mode"], status["human"], status["ply"]),
                                         (mode, human, initial_ply))
                        self.assertEqual(request(base, "/v1/moves/resolve", resolved_body), (200, payload))
                        self.assertEqual(request(base, "/v1/moves/resolve", {
                            "request_id": "unresolved-while-idle", "expression": expression,
                        })[0], 409)
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        waiting_status(base, ply=initial_ply)
                        changed_start = dict(start, human="white" if human == "black" else "black")
                        self.assertEqual(request(base, "/v1/session/start", changed_start)[0], 409)
                        changed_start = dict(start, mode="external" if mode == "human_engine" else "human_engine")
                        self.assertEqual(request(base, "/v1/session/start", changed_start)[0], 409)

                        move = dict(payload, request_id="spoken-1")
                        self.assertIn(request(base, "/v1/moves", move)[0], {200, 202})
                        wait_for(lambda: request(base, "/v1/moves/spoken-1")[1]["state"] == "committed")
                        waiting_status(base, ply=committed_ply)
                        self.assertEqual(request(base, "/v1/moves/resolve", resolved_body), (200, payload))
                        code, error = request(base, "/v1/moves/resolve", dict(resolved_body, expression="knight to c3"))
                        self.assertEqual(code, 409)
                        self.assertEqual(error["error"]["code"], "conflict")
                        self.assertEqual(request(base, "/v1/moves", dict(move, uci="d2d4"))[0], 409)
                        self.assertEqual(request(base, "/v1/moves", dict(move, request_id="stale-position"))[0], 409)
                        self.assertEqual(request(base, "/v1/moves", move)[0], 200)
                        self.assertEqual(request(base, "/v1/status")[1]["ply"], committed_ply)
                        stop_remote(base, resumed)
                finally:
                    close_controller(resumed)

                reopened = controller_for(path, new=False, simulate=True)
                try:
                    with remote_server(reopened) as base:
                        code, receipt = request(base, "/v1/moves/spoken-1")
                        self.assertEqual((code, receipt["state"], receipt["uci"]),
                                         (200, "committed", payload["uci"]))
                        self.assertEqual(request(base, "/v1/moves/resolve", resolved_body), (200, payload))
                        self.assertEqual(request(base, "/v1/moves", move)[0], 200)
                        self.assertEqual(request(base, "/v1/session/start", start)[0], 202)
                        status = waiting_status(base, ply=committed_ply)
                        self.assertEqual((status["mode"], status["human"]), (mode, human))
                        self.assertIsNone(status["pending"])
                        self.assertIsNone(status["error"])
                        stop_remote(base, reopened)
                finally:
                    close_controller(reopened)


if __name__ == "__main__":
    unittest.main()

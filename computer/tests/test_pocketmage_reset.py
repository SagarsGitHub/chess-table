"""Fenced automatic reset reuses the single worker and never replays on retry."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import chess

from chess_table_host.checkpoint import GameState, JsonGameStore, STATE_VERSION
from chess_table_host.game_errors import GameStateError
from chess_table_host.game_errors import MoveDeferred
from chess_table_host.serial_link import LinkError
from chess_table_host.reset_receipt import RemoteResetReceipt
from chess_table_host.gui_snapshot import SynchronizedGameStore
from test_pocketmage_http import pocketmage_server, start_intent, waiting
from test_remote_http import (
    HeldLink, close_controller, controller_for, move_body, remote_server,
    request, start_body, wait_for,
)


def reset_body(status, request_id="reset-1"):
    return {
        "request_id": request_id,
        "host_game_id": status["host_game_id"],
        "source_game_id": status["source_game_id"],
        "expected_ply": status["ply"],
        "expected_fen": status["fen"],
        "confirmed": True,
    }


def start(base):
    initial = request(base, "/v1/status")[1]
    assert request(base, "/v1/session/start", start_intent(initial))[0] == 202
    return waiting(base)


def play(base, uci, ply):
    status = waiting(base, ply)
    body = dict(move_body(status), request_id="move-" + str(ply),
                expected_ply=ply, expected_fen=status["fen"], uci=uci)
    assert request(base, "/v1/moves", body)[0] in {200, 202}
    return waiting(base, ply + 1)


class ResetHeldLink(HeldLink):
    def carry(self, *_arguments):
        self.carries += 1
        self.entered.set()
        if not self.release.wait(timeout=30):
            raise LinkError("test carry timed out")
        if self.stopped.is_set():
            raise LinkError("motion stopped")
        return SimpleNamespace(command_id=self.carries)


class PocketMageResetTests(unittest.TestCase):
    def test_confirm_exact_body_owner_and_position_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base:
                    status = request(base, "/v1/status")[1]
                    self.assertEqual(request(base, "/v1/session/reset", dict(reset_body(status), source_game_id="unbound"))[0], 409)
                    status = start(base)
                    body = reset_body(status)
                    bad = [
                        ({key: value for key, value in body.items() if key != "confirmed"}, 400),
                        (dict(body, confirmed=False), 400),
                        (dict(body, confirmed=1), 400),
                        (dict(body, extra=True), 400),
                        (dict(body, expected_ply=True), 400),
                        (dict(body, host_game_id="bad"), 400),
                        (dict(body, host_game_id=str(uuid4())), 409),
                        (dict(body, source_game_id="another-device"), 409),
                        (dict(body, expected_ply=1), 409),
                        (dict(body, expected_fen="wrong"), 409),
                    ]
                    for invalid, expected in bad:
                        with self.subTest(invalid=invalid):
                            code, error = request(base, "/v1/session/reset", invalid)
                            self.assertEqual(code, expected)
                            if expected == 409:
                                self.assertEqual(error["error"]["code"], "reset_not_accepted")
                                self.assertEqual(error["error"]["request"], invalid)
                                self.assertEqual(request(base, "/v1/resets/" + invalid["request_id"])[0], 404)
                    self.assertEqual(controller.store.load().remote_reset_receipts, {})
                    self.assertEqual(request(base, "/v1/resets/missing")[0], 404)
                    self.assertEqual(request(base, "/v1/resets/invalid%20id")[0], 400)
            finally:
                close_controller(controller)

    def test_aina_owner_and_reset_receipt_routes_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base, remote_server(controller) as aina:
                    status = request(aina, "/v1/status")[1]
                    self.assertEqual(request(aina, "/v1/session/start", start_body(status))[0], 202)
                    status = wait_for(lambda: (value if (value := request(aina, "/v1/status")[1])["can_move"] else None))
                    self.assertEqual(request(base, "/v1/session/reset", reset_body(status))[0], 409)
                    self.assertEqual(request(aina, "/v1/resets/missing")[0], 404)
            finally:
                close_controller(controller)

    def test_capture_reset_completion_receipt_survives_new_game_and_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            controller = controller_for(path, new=True, simulate=True)
            try:
                with pocketmage_server(controller) as base:
                    start(base)
                    for ply, uci in enumerate(("e2e4", "d7d5", "e4d5")):
                        status = play(base, uci, ply)
                    self.assertTrue(any(controller.store.load().physical_parking))
                    body = reset_body(status)
                    self.assertIn(request(base, "/v1/session/reset", body)[0], {200, 202})
                    receipt = wait_for(lambda: (
                        value if (value := request(base, "/v1/resets/reset-1")[1])["state"] == "committed" else None
                    ), timeout=20)
                    for key, expected in body.items():
                        self.assertEqual(receipt[key], expected)
                    self.assertNotEqual(receipt["resulting_host_game_id"], body["host_game_id"])
                    wait_for(lambda: not controller.remote_status()["busy"])
                    fresh = controller.remote_status()
                    self.assertEqual(fresh["host_game_id"], receipt["resulting_host_game_id"])
                    self.assertEqual(fresh["fen"], chess.Board().fen())
                    self.assertEqual(fresh["ply"], 0)
                    self.assertIsNone(fresh["source_kind"])
                    self.assertFalse(any(controller.store.load().physical_parking))
                    with patch.object(controller, "end_game_and_reset", side_effect=AssertionError("duplicate restarted reset")):
                        self.assertEqual(request(base, "/v1/session/reset", body), (200, receipt))
                    self.assertEqual(request(base, "/v1/session/reset", dict(body, expected_ply=0))[0], 409)
                    self.assertEqual(request(base, "/v1/session/reset", dict(body, expected_ply=0))[1]["error"]["code"], "conflict")
                    self.assertEqual(request(base, "/v1/session/reset", dict(body, request_id="another"))[0], 409)
                    archives = list(path.parent.glob("game.before-reset-*.json"))
                    self.assertEqual(len(archives), 1)
                    self.assertEqual(GameState.from_json(json.loads(archives[0].read_text())).moves, ["e2e4", "d7d5", "e4d5"])
            finally:
                close_controller(controller)
            resumed = controller_for(path, new=False, simulate=True)
            try:
                with pocketmage_server(resumed) as base:
                    self.assertEqual(request(base, "/v1/resets/reset-1")[1], receipt)
                    with patch.object(resumed, "end_game_and_reset", side_effect=AssertionError("reboot retry restarted reset")):
                        self.assertEqual(request(base, "/v1/session/reset", body), (200, receipt))
                    self.assertIsNone(resumed._worker)
            finally:
                close_controller(resumed)

    def test_already_standard_game_commits_reset_without_a_carry(self):
        with tempfile.TemporaryDirectory() as directory:
            held = ResetHeldLink()
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=False, link=held)
            try:
                with pocketmage_server(controller) as base:
                    status = start(base)
                    body = reset_body(status)
                    self.assertIn(request(base, "/v1/session/reset", body)[0], {200, 202})
                    receipt = wait_for(lambda: (
                        value if (value := request(base, "/v1/resets/reset-1")[1])["state"] == "committed" else None
                    ))
                    self.assertNotEqual(receipt["resulting_host_game_id"], status["host_game_id"])
                    self.assertEqual(held.carries, 0)
            finally:
                close_controller(controller)

    def test_duplicate_while_resetting_and_stop_never_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            held = ResetHeldLink()
            held.release.set()
            controller = controller_for(path, new=True, simulate=False, link=held)
            try:
                with pocketmage_server(controller) as base:
                    start(base)
                    status = play(base, "e2e4", 0)
                    body = reset_body(status)
                    # HeldLink is the entire transport; no hardware is opened.
                    before = held.carries
                    held.release.clear()
                    held.entered.clear()
                    code, first = request(base, "/v1/session/reset", body)
                    self.assertIn(code, {200, 202})
                    self.assertTrue(held.entered.wait(5))
                    receipt = request(base, "/v1/resets/reset-1")[1]
                    self.assertEqual(receipt["state"], "in_flight")
                    self.assertEqual(receipt["phase"], "resetting")
                    self.assertEqual(receipt["action_number"], 1)
                    self.assertGreater(receipt["total_actions"], 0)
                    worker = controller._worker
                    self.assertEqual(request(base, "/v1/session/reset", body)[0], 202)
                    self.assertIs(controller._worker, worker)
                    self.assertEqual(held.carries, before + 1)
                    self.assertEqual(request(base, "/v1/session/reset", dict(body, request_id="second-reset"))[0], 409)
                    self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                    wait_for(lambda: not controller.remote_status()["busy"])
                    stopped = request(base, "/v1/resets/reset-1")[1]
                    self.assertEqual(stopped["state"], "reconciliation_required")
                    self.assertEqual(request(base, "/v1/session/start", start_intent(controller.remote_status()))[0], 409)
                    with patch.object(controller, "end_game_and_reset", side_effect=AssertionError("STOP retry restarted reset")):
                        self.assertEqual(request(base, "/v1/session/reset", body)[1], stopped)
                    self.assertEqual(held.carries, before + 1)
            finally:
                close_controller(controller)
            resumed = controller_for(path, new=False, simulate=True)
            try:
                with pocketmage_server(resumed) as base:
                    self.assertEqual(request(base, "/v1/resets/reset-1")[1]["state"], "reconciliation_required")
                    self.assertEqual(request(base, "/v1/session/reset", body)[1]["state"], "reconciliation_required")
                    self.assertEqual(request(base, "/v1/session/start", start_intent(resumed.remote_status()))[0], 409)
                    self.assertIsNone(resumed._worker)
            finally:
                close_controller(resumed)

    def test_reset_finishes_the_current_chess_move_before_returning_pieces(self):
        with tempfile.TemporaryDirectory() as directory:
            held = ResetHeldLink()
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=False, link=held)
            try:
                with pocketmage_server(controller) as base:
                    status = start(base)
                    self.assertIn(request(base, "/v1/moves", move_body(status))[0], {200, 202})
                    self.assertTrue(held.entered.wait(5))
                    moving = request(base, "/v1/status")[1]
                    self.assertEqual(moving["ply"], 0)
                    body = reset_body(moving)
                    self.assertEqual(request(base, "/v1/session/reset", body)[0], 202)
                    receipt = request(base, "/v1/resets/reset-1")[1]
                    self.assertEqual(receipt["phase"], "finishing_move")
                    self.assertEqual(held.carries, 1)
                    held.release.set()
                    wait_for(lambda: request(base, "/v1/resets/reset-1")[1]["state"] == "committed", timeout=20)
                    wait_for(lambda: not controller.remote_status()["busy"])
                    self.assertEqual(held.carries, 2)
                    self.assertEqual(controller.remote_status()["fen"], chess.Board().fen())
            finally:
                held.release.set()
                close_controller(controller)

    def test_stop_before_first_reset_carry_aborts_and_duplicate_only_observes(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
            entered, release = threading.Event(), threading.Event()

            def paused_preflight():
                entered.set()
                release.wait(10)
                raise MoveDeferred("stopped before reset preflight")

            try:
                with pocketmage_server(controller) as base:
                    body = reset_body(start(base))
                    with patch.object(controller, "_reset_loop", side_effect=paused_preflight):
                        self.assertEqual(request(base, "/v1/session/reset", body)[0], 202)
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(request(base, "/v1/stop", {})[0], 202)
                        release.set()
                        wait_for(lambda: not controller.remote_status()["busy"])
                    receipt = request(base, "/v1/resets/reset-1")[1]
                    self.assertEqual(receipt["state"], "aborted")
                    with patch.object(controller, "end_game_and_reset", side_effect=AssertionError("aborted retry restarted reset")):
                        self.assertEqual(request(base, "/v1/session/reset", body), (200, receipt))
                    self.assertIsNone(controller._worker)
            finally:
                release.set()
                close_controller(controller)

    def test_reboot_before_dispatch_aborts_without_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.json"
            state = GameState.new()
            state.source_kind = "pocketmage"
            state.source_game_id = "saved-device"
            state.remote_start_request_id = "start-1"
            body = dict(request_id="saved-reset", host_game_id=state.host_game_id,
                        source_game_id=state.source_game_id, expected_ply=0,
                        expected_fen=state.board_fen, confirmed=True)
            state.remote_reset_receipts["saved-reset"] = RemoteResetReceipt(**body)
            JsonGameStore(path).save(state)
            controller = controller_for(path, new=False, simulate=True)
            try:
                with pocketmage_server(controller) as base:
                    code, receipt = request(base, "/v1/session/reset", body)
                    self.assertEqual(code, 200)
                    self.assertEqual(receipt["state"], "aborted")
                    self.assertEqual(controller.store.load().remote_reset_receipts["saved-reset"].state, "aborted")
                    self.assertIsNone(controller._worker)
            finally:
                close_controller(controller)

    def test_invalid_checkpoint_during_final_settlement_releases_worker(self):
        for error in (GameStateError("corrupt checkpoint"), json.JSONDecodeError("invalid", "?", 0)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                controller = controller_for(Path(directory) / "game.json", new=True, simulate=True)
                try:
                    controller._worker = threading.current_thread()
                    with patch.object(controller, "_load_checkpoint", side_effect=error):
                        controller._work(lambda: None)
                    self.assertIsNone(controller._worker)
                    self.assertIn("Could not save the stopped reset", controller._status)
                finally:
                    close_controller(controller)

    def test_v6_migration_and_v7_reject_damaged_reset_metadata(self):
        state = GameState.new().to_json()
        self.assertEqual(state["version"], STATE_VERSION)
        historical = dict(state, version=6)
        del historical["remote_reset_receipts"]
        self.assertEqual(GameState.from_json(historical).remote_reset_receipts, {})
        damaged = deepcopy(state)
        del damaged["remote_reset_receipts"]
        with self.assertRaises(GameStateError):
            GameState.from_json(damaged)
        damaged = deepcopy(state)
        damaged["remote_reset_receipts"] = {"bad": {"state": "committed"}}
        with self.assertRaises(GameStateError):
            GameState.from_json(damaged)

    def test_transient_missing_checkpoint_does_not_drop_registered_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SynchronizedGameStore(Path(directory) / "game.json")
            state = GameState.new()
            state.source_kind = "pocketmage"
            state.source_game_id = "saved-device"
            state.remote_start_request_id = "start-1"
            store.save(state)
            stale_worker = deepcopy(state)
            receipt = RemoteResetReceipt("reset-1", state.host_game_id,
                                         state.source_game_id, 0, state.board_fen)
            store.register_reset_receipt(receipt, lambda _state: None)
            self.assertFalse(stale_worker.remote_reset_receipts)
            with patch.object(JsonGameStore, "load", side_effect=FileNotFoundError("DrvFS visibility gap")):
                store.save(stale_worker)
            self.assertEqual(store.load().remote_reset_receipts["reset-1"].to_json(), receipt.to_json())


if __name__ == "__main__":
    unittest.main()

"""Preview isolation and recorded replay through the sole checkpointed worker."""

from contextlib import contextmanager
from http import HTTPStatus
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chess_table_host.api_errors import ApiError
from chess_table_host.gui_http import GuiHTTPServer
from chess_table_host.gui_controller import GameController
from chess_table_host.serial_link import LinkError
from test_gui import FIXTURE, game_controller, wait_for


GAME = "opera-1858-morphy-duke-count"


def select_opera(controller):
    return controller.chesscom_select({"kind": "historical", "id": GAME})


def start_replay(controller):
    selected = select_opera(controller)
    return controller.chesscom_replay_start({"confirmed": True, "selection_token": selected["selection_token"]})


def next_intent(controller):
    view = controller.chesscom_selection()["replay"]
    return {"selection_token": view["selection_token"], "host_game_id": view["host_game_id"],
            "expected_ply": view["index"], "expected_fen": view["expected_fen"]}


@contextmanager
def serve(controller):
    with GuiHTTPServer(("127.0.0.1", 0), controller) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            worker.join(timeout=5)


class ChessComControllerTests(unittest.TestCase):
    def test_historical_search_validated_and_preview_never_changes_checkpoint(self):
        with game_controller() as controller:
            before = controller.store.path.read_bytes()
            all_games = controller.chesscom_list("historical", {})["games"]
            self.assertGreaterEqual(len(all_games), 3)
            self.assertTrue(all(game["replay_supported"] for game in all_games))
            self.assertTrue(all(len(game["positions"]) == len(game["moves"]) + 1 for game in all_games))
            self.assertEqual(len(controller.chesscom_list("historical", {"q": "Morphy"})["games"]), 1)
            for game in all_games:
                controller.chesscom_select({"kind": "historical", "id": game["id"]})
            self.assertIsNone(controller.link)
            self.assertIsNone(controller._worker)
            self.assertEqual(controller.store.path.read_bytes(), before)

    def test_replay_requires_confirmation_and_fences_then_commits_exactly_one_move(self):
        with game_controller() as controller:
            selected = select_opera(controller)
            with self.assertRaisesRegex(ApiError, "confirm"):
                controller.chesscom_replay_start({"selection_token": selected["selection_token"]})
            start_replay(controller)
            self.assertIsNone(controller.link)
            self.assertFalse(controller.state()["can_start"])
            with self.assertRaisesRegex(ApiError, "recorded replay"):
                controller.start("self_play")
            with self.assertRaisesRegex(ApiError, "changed"):
                controller.chesscom_replay_next({})
            intent = next_intent(controller)
            controller.chesscom_replay_next(intent)
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], ["e2e4"])
            self.assertTrue(controller.chesscom_selection()["replay"]["can_next"])
            with self.assertRaisesRegex(ApiError, "changed"):
                controller.chesscom_replay_next(intent)
            self.assertEqual(controller.state()["moves"], ["e2e4"])

    def test_other_preview_cannot_drive_bound_game_and_reset_allows_switch(self):
        with game_controller() as controller:
            start_replay(controller)
            intent = next_intent(controller)
            controller.chesscom_replay_next(intent)
            wait_for(lambda: not controller.state()["busy"])
            games = controller.chesscom_list("historical", {})["games"]
            other = next(game for game in games if game["id"] != GAME)
            selection = controller.chesscom_select({"kind": "historical", "id": other["id"]})
            self.assertFalse(selection["replay"]["can_next"])
            self.assertFalse(selection["replay"]["can_start"])
            with self.assertRaises(ApiError):
                controller.chesscom_replay_next(next_intent(controller))
            controller.end_game_and_reset()
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], [])
            self.assertFalse(controller.chesscom_selection()["replay"]["active"])
            self.assertTrue(controller.chesscom_selection()["replay"]["can_start"])

    def test_daily_missing_pgn_is_preview_only_and_no_remote_binding_is_replaced(self):
        with game_controller() as controller:
            game = {"id": "https://www.chess.com/game/daily/123", "title": "ofroni vs friend",
                    "pgn": "", "moves": [], "positions": [], "replay_supported": False,
                    "unsupported_reason": "No PGN"}
            controller._chesscom_client.daily = lambda username: {"games": [game]}
            selected = controller.chesscom_select({"kind": "daily", "id": game["id"], "username": "ofroni"})
            self.assertFalse(selected["replay"]["can_start"])
            self.assertEqual(selected["selection"]["kind"], "daily")
            self.assertIsNone(controller.link)
            state = controller._load_checkpoint()
            state.source_kind = "pocketmage"
            state.source_game_id = "existing"
            state.remote_start_request_id = "start-existing"
            controller.store.save(state)
            selected = select_opera(controller)
            with self.assertRaisesRegex(ApiError, "fresh"):
                controller.chesscom_replay_start({"confirmed": True, "selection_token": selected["selection_token"]})

    def test_uncertain_carry_blocks_next_until_local_inspection(self):
        class FailedLink:
            def set_map(self, bitmap):
                pass

            def carry(self, *args):
                raise LinkError("test disconnect during carry")

            def close(self):
                pass

        with game_controller() as controller:
            start_replay(controller)
            controller._connection()
            controller.link = FailedLink()
            intent = next_intent(controller)
            controller.chesscom_replay_next(intent)
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["phase"], "reconciliation_required")
            self.assertFalse(controller.chesscom_selection()["replay"]["can_next"])
            with self.assertRaises(ApiError):
                controller.chesscom_replay_next(intent)
            self.assertEqual(controller.state()["moves"], [])

    def test_reselecting_same_snapshot_keeps_replay_token(self):
        with game_controller() as controller:
            view = start_replay(controller)
            again = select_opera(controller)
            self.assertEqual(view["selection_token"], again["selection_token"])
            self.assertTrue(again["replay"]["can_next"])

    def test_restart_retains_ownership_guard_without_resuming_motion(self):
        with game_controller() as controller:
            start_replay(controller)
            controller.chesscom_replay_next(next_intent(controller))
            wait_for(lambda: not controller.state()["busy"])
            controller.close()
            restored = GameController(
                state_path=controller.store.path, new=False, simulate=True,
                geometry_file=FIXTURE, serial_port="TEST-COM-GUI", serial_log=controller.serial_log,
                stockfish_path="unused", engine_time=0.01, moving_diameter=18.0,
                standing_diameter=18.0, margin=0.0, allow_tight_routes=True,
            )
            try:
                self.assertIsNone(restored.link)
                self.assertIsNone(restored._worker)
                self.assertEqual(restored.state()["moves"], ["e2e4"])
                self.assertFalse(restored.state()["can_start"])
                self.assertFalse(restored.chesscom_selection()["replay"]["can_next"])
                with self.assertRaisesRegex(ApiError, "recorded replay"):
                    restored.start("human_engine")
                with self.assertRaisesRegex(ApiError, "recorded replay"):
                    restored.remote_start({"host_game_id": restored.runner.state.host_game_id,
                                           "source_kind": "pocketmage", "source_game_id": "other",
                                           "request_id": "start", "mode": "external", "human": "white"},
                                          source_kind="pocketmage")
                restored.end_game_and_reset()
                wait_for(lambda: not restored.state()["busy"])
                self.assertTrue(restored.state()["can_start"])
            finally:
                restored.stop()
                if restored._worker:
                    restored._worker.join(timeout=5)
                restored.close()

    def test_reset_during_replay_finishes_move_then_archives_marker_and_resets(self):
        with game_controller() as controller:
            start_replay(controller)
            original_id = controller.runner.state.host_game_id
            link, _ = controller._connection()
            started, release = threading.Event(), threading.Event()
            carry = link.carry

            def held_carry(*args):
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("held carry timed out")
                return carry(*args)

            link.carry = held_carry
            controller.chesscom_replay_next(next_intent(controller))
            try:
                self.assertTrue(started.wait(timeout=5))
                controller.end_game_and_reset()
                self.assertTrue(controller.state()["reset_requested"])
            finally:
                release.set()
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], [])
            self.assertNotEqual(controller.runner.state.host_game_id, original_id)
            markers = list(controller.store.path.parent.glob("game.before-reset-*.replay.json"))
            self.assertEqual(len(markers), 1)
            self.assertEqual(json.loads(markers[0].read_text())["host_game_id"], original_id)

    def test_old_snapshot_cannot_clear_new_replay_binding(self):
        with game_controller() as controller:
            old = controller._load_checkpoint()
            controller.reset_board()
            wait_for(lambda: not controller.state()["busy"])
            start_replay(controller)
            self.assertFalse(controller._chesscom_bound(old))
            self.assertFalse(controller.state()["can_start"])
            self.assertTrue(controller.chesscom_selection()["replay"]["can_next"])


class ChessComHTTPTests(unittest.TestCase):
    def test_transport_historical_selection_validation_and_origin_policy(self):
        with game_controller() as controller, serve(controller) as base:
            with urlopen(base + "/api/chesscom/historical?q=Morphy") as response:
                game = json.load(response)["games"][0]
            body = json.dumps({"kind": "historical", "id": game["id"]}).encode()
            request = Request(base + "/api/chesscom/select", body, {"Content-Type": "application/json"})
            with urlopen(request) as response:
                selected = json.load(response)
                self.assertEqual(response.status, HTTPStatus.ACCEPTED)
                self.assertEqual(selected["selection"]["id"], GAME)
            self.assertIsNone(controller.link)
            request.add_header("Origin", "https://other.example")
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            for route in ("historical?q=a&q=b", "selection?extra=x", "unknown"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/chesscom/" + route)
                self.assertIn(error.exception.code, {400, 404})
                error.exception.close()
            request = Request(base + "/api/chesscom/select", b'{"kind":[],"id":"x"}',
                              {"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

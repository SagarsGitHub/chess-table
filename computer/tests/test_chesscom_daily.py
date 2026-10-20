"""Daily switching, scheduled following and fault gates with simulated motion."""

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import threading
import time
import unittest
from unittest.mock import patch

import chess
import chess.pgn

from chess_table_host.api_errors import ApiError
from chess_table_host.chesscom import _normalize_game
from chess_table_host.gui_daily import DAILY_POLL_SECONDS
from chess_table_host.serial_link import LinkError
from test_gui import game_controller, wait_for


def daily_game(number, moves=()):
    board = chess.Board()
    for move in moves:
        board.push_uci(move)
    game = chess.pgn.Game.from_board(board)
    game.headers.update({"White": "ofroni", "Black": "friend", "Result": "*"})
    pgn = game.accept(chess.pgn.StringExporter(headers=True, comments=False, variations=False))
    return _normalize_game({
        "url": f"https://www.chess.com/game/daily/{number}",
        "white": {"username": "ofroni"}, "black": {"username": "friend"},
        "pgn": pgn, "fen": board.fen(), "rules": "chess", "time_class": "daily",
        "turn": "white" if board.turn else "black",
    }, finished=False)


class DailyClient:
    def __init__(self, games):
        self.games = list(games)
        self.calls = []

    def daily(self, username="ofroni", *, refresh=False):
        self.calls.append((username, refresh))
        return {"username": username, "games": deepcopy(self.games)}


@contextmanager
def daily_controller(games):
    with game_controller() as controller, \
            patch.object(controller, "_ensure_daily_poll"), \
            redirect_stdout(io.StringIO()):
        client = DailyClient(games)
        controller._chesscom_client = client
        yield controller, client


def select_daily(controller, game):
    return controller.chesscom_select({"kind": "daily", "id": game["id"], "username": "ofroni"})


def switch_daily(controller, game):
    selected = select_daily(controller, game)
    result = controller.chesscom_daily_switch({"selection_token": selected["selection_token"],
                                               "confirmed": True})
    wait_for(lambda: not controller.state()["busy"])
    return result


class ChessComDailyTests(unittest.TestCase):
    def test_switch_resets_different_position_and_recreates_snapshot_without_engine(self):
        game = daily_game(1, ("e2e4", "e7e5"))
        with daily_controller([game]) as (controller, _client), \
                patch("chess_table_host.gui_worker.chess.engine.SimpleEngine.popen_uci") as engine:
            link, geometry = controller._connection()
            controller.runner.prepare_move("a2a4", geometry)
            controller.runner.execute_pending(link, geometry, lambda _action: True,
                                              confirmation_source="simulation")
            old_id = controller.runner.state.host_game_id
            switch_daily(controller, game)
            saved = controller.store.load()
            self.assertEqual(saved.moves, game["moves"])
            self.assertEqual(saved.board_fen, game["fen"])
            self.assertNotEqual(saved.host_game_id, old_id)
            self.assertTrue(controller.chesscom_selection()["daily"]["active"])
            self.assertFalse(controller.chesscom_selection()["replay"]["can_next"])
            engine.assert_not_called()

    def test_switch_between_two_daily_games_resets_instead_of_reusing_moves(self):
        first = daily_game(1, ("e2e4", "e7e5"))
        second = daily_game(2, ("d2d4", "d7d5"))
        with daily_controller([first, second]) as (controller, _client):
            switch_daily(controller, first)
            first_id = controller.store.load().host_game_id
            switch_daily(controller, second)
            saved = controller.store.load()
            self.assertNotEqual(saved.host_game_id, first_id)
            self.assertEqual(saved.moves, second["moves"])
            self.assertEqual(saved.board_fen, second["fen"])
            self.assertEqual(controller.chesscom_selection()["daily"]["game_id"], second["id"])

    def test_zero_ply_daily_can_be_set_and_follow_its_first_recorded_move(self):
        empty = daily_game(1)
        self.assertTrue(empty["replay_supported"])
        with daily_controller([empty]) as (controller, client):
            switch_daily(controller, empty)
            self.assertEqual(controller.store.load().moves, [])
            self.assertTrue(controller.chesscom_selection()["daily"]["active"])
            first_move = daily_game(1, ("e2e4",))
            client.games = [first_move]
            controller.chesscom_daily_check({})
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.store.load().moves, ["e2e4"])

    def test_switch_requires_exact_selection_and_explicit_confirmation(self):
        game = daily_game(1, ("e2e4",))
        with daily_controller([game]) as (controller, _client):
            selected = select_daily(controller, game)
            before = controller.store.path.read_bytes()
            for body in ({"selection_token": selected["selection_token"]},
                         {"selection_token": "old-token", "confirmed": True},
                         {"selection_token": selected["selection_token"], "confirmed": 1},
                         {"selection_token": selected["selection_token"], "confirmed": True, "extra": True}):
                with self.subTest(body=body), self.assertRaises(ApiError):
                    controller.chesscom_daily_switch(body)
            self.assertEqual(controller.store.path.read_bytes(), before)
            self.assertIsNone(controller.link)
            self.assertIsNone(controller._worker)

    def test_manual_check_applies_only_appended_recorded_moves(self):
        first = daily_game(1, ("e2e4",))
        latest = daily_game(1, ("e2e4", "e7e5", "g1f3"))
        with daily_controller([first]) as (controller, client):
            switch_daily(controller, first)
            client.games = [latest]
            controller.chesscom_daily_check({})
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.store.load().moves, latest["moves"])
            self.assertEqual(controller.store.load().board_fen, latest["fen"])
            self.assertEqual(client.calls[-1], ("ofroni", True))
            before = controller.store.path.read_bytes()
            controller.chesscom_daily_check({})
            self.assertIsNone(controller._worker)
            self.assertEqual(controller.store.path.read_bytes(), before)

    def test_due_poll_forces_refresh_once_and_schedules_next_1800_seconds(self):
        first = daily_game(1, ("e2e4",))
        latest = daily_game(1, ("e2e4", "e7e5"))
        with daily_controller([first]) as (controller, client):
            switch_daily(controller, first)
            self.assertEqual(DAILY_POLL_SECONDS, 1800)
            initial_calls = len(client.calls)
            controller._daily_poll_once()
            self.assertEqual(len(client.calls), initial_calls)
            client.games = [latest]
            controller._chesscom_daily["next_check"] = time.monotonic() - 1
            controller._daily_poll_once()
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(len(client.calls), initial_calls + 1)
            self.assertEqual(client.calls[-1], ("ofroni", True))
            self.assertEqual(controller.store.load().moves, latest["moves"])
            delay = controller._chesscom_daily["next_check"] - time.monotonic()
            self.assertGreater(delay, 1790)
            self.assertLessEqual(delay, 1800)
            controller._daily_poll_once()
            self.assertEqual(len(client.calls), initial_calls + 1)

    def test_missing_shorter_or_divergent_history_pauses_without_motion(self):
        first = daily_game(1, ("e2e4", "e7e5"))
        alternatives = ([], [daily_game(1, ("e2e4",))], [daily_game(1, ("d2d4", "d7d5"))])
        for updates in alternatives:
            with self.subTest(updates=updates), daily_controller([first]) as (controller, client):
                switch_daily(controller, first)
                before = controller.store.path.read_bytes()
                client.games = updates
                controller.chesscom_daily_check({})
                self.assertTrue(controller.chesscom_selection()["daily"]["paused"])
                self.assertIsNone(controller._worker)
                self.assertEqual(controller.store.path.read_bytes(), before)

    def test_stop_disables_automatic_following_even_when_table_is_idle(self):
        first = daily_game(1, ("e2e4",))
        latest = daily_game(1, ("e2e4", "e7e5"))
        with daily_controller([first]) as (controller, client):
            switch_daily(controller, first)
            controller.stop()
            before = controller.store.path.read_bytes()
            client.games = [latest]
            controller._chesscom_daily["next_check"] = time.monotonic() - 1
            controller._daily_poll_once()
            self.assertTrue(controller.chesscom_selection()["daily"]["paused"])
            self.assertEqual(controller.store.path.read_bytes(), before)
            self.assertIsNone(controller._worker)

    def test_remote_owned_and_pending_checkpoints_refuse_switch(self):
        game = daily_game(1, ("d2d4",))
        with daily_controller([game]) as (controller, _client):
            selected = select_daily(controller, game)
            saved = controller.store.load()
            saved.source_kind = "pocketmage"
            saved.source_game_id = "existing"
            saved.remote_start_request_id = "start-existing"
            controller.store.save(saved)
            with self.assertRaises(ApiError):
                controller.chesscom_daily_switch({"selection_token": selected["selection_token"], "confirmed": True})
            self.assertIsNone(controller.link)
        with daily_controller([game]) as (controller, _client):
            selected = select_daily(controller, game)
            _link, geometry = controller._connection()
            controller.runner.prepare_move("e2e4", geometry)
            before = controller.store.path.read_bytes()
            with self.assertRaises(ApiError):
                controller.chesscom_daily_switch({"selection_token": selected["selection_token"], "confirmed": True})
            self.assertEqual(controller.store.path.read_bytes(), before)
            self.assertIsNone(controller._worker)

    def test_catchup_fault_preserves_uncertain_checkpoint_and_pauses_poll_motion(self):
        game = daily_game(1, ("e2e4", "e7e5"))
        with daily_controller([game]) as (controller, client):
            link, _geometry = controller._connection()
            with patch.object(link, "carry", side_effect=LinkError("simulated daily disconnect")), \
                    redirect_stdout(io.StringIO()):
                switch_daily(controller, game)
            saved = controller.store.load()
            self.assertEqual(saved.moves, [])
            self.assertIsNotNone(saved.pending)
            self.assertEqual(saved.pending.phase, "in_flight")
            self.assertTrue(controller.chesscom_selection()["daily"]["paused"])
            self.assertEqual(controller.state()["phase"], "reconciliation_required")
            before = controller.store.path.read_bytes()
            client.games = [daily_game(1, ("e2e4", "e7e5", "g1f3"))]
            controller.chesscom_daily_check({})
            self.assertEqual(controller.store.path.read_bytes(), before)
            self.assertIsNone(controller._worker)
            with self.assertRaises(ApiError):
                controller.chesscom_daily_switch({"selection_token": controller._chesscom_token, "confirmed": True})

    def test_stale_fetch_cannot_apply_old_game_after_switch(self):
        first = daily_game(1, ("e2e4",))
        second = daily_game(2, ("d2d4",))
        with daily_controller([first, second]) as (controller, client):
            switch_daily(controller, first)
            entered, release = threading.Event(), threading.Event()
            original = client.daily
            errors = []

            def held_fetch(username, *, refresh=False):
                response = original(username, refresh=refresh)
                if refresh:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("held daily fetch timed out")
                return response

            def check():
                try:
                    controller.chesscom_daily_check({})
                except BaseException as exc:
                    errors.append(exc)

            client.daily = held_fetch
            checker = threading.Thread(target=check)
            checker.start()
            try:
                self.assertTrue(entered.wait(5))
                switch_daily(controller, second)
            finally:
                release.set()
                checker.join(5)
            self.assertFalse(checker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(controller.chesscom_selection()["daily"]["game_id"], second["id"])
            self.assertEqual(controller.store.load().moves, second["moves"])

    def test_update_arriving_after_catchup_return_is_applied_when_worker_exits(self):
        first = daily_game(1, ("e2e4",))
        latest = daily_game(1, ("e2e4", "e7e5"))
        with daily_controller([first]) as (controller, client):
            finished, release = threading.Event(), threading.Event()
            original = controller._chesscom_daily_catchup
            calls = []

            def held_after_return():
                original()
                calls.append(True)
                if len(calls) == 1:
                    finished.set()
                    if not release.wait(5):
                        raise RuntimeError("held catchup return timed out")

            with patch.object(controller, "_chesscom_daily_catchup", new=held_after_return):
                selected = select_daily(controller, first)
                controller.chesscom_daily_switch({"selection_token": selected["selection_token"], "confirmed": True})
                try:
                    self.assertTrue(finished.wait(5))
                    self.assertEqual(controller.store.load().moves, first["moves"])
                    client.games = [latest]
                    controller.chesscom_daily_check({})
                    self.assertTrue(controller.state()["busy"])
                finally:
                    release.set()
                wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.store.load().moves, latest["moves"])
            self.assertEqual(len(calls), 2)
            self.assertFalse(controller._chesscom_daily["needs_catchup"])

    def test_busy_switch_is_refused_and_graceful_reset_finishes_only_current_move(self):
        first = daily_game(1, ("e2e4", "e7e5"))
        second = daily_game(2, ("d2d4",))
        with daily_controller([first, second]) as (controller, _client):
            link, _geometry = controller._connection()
            entered, release = threading.Event(), threading.Event()
            carry = link.carry
            calls = []

            def held_first_carry(*args):
                calls.append(args)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("held daily carry timed out")
                return carry(*args)

            with patch.object(link, "carry", new=held_first_carry):
                selected = select_daily(controller, first)
                controller.chesscom_daily_switch({"selection_token": selected["selection_token"], "confirmed": True})
                try:
                    self.assertTrue(entered.wait(5))
                    selected_other = select_daily(controller, second)
                    with self.assertRaises(ApiError):
                        controller.chesscom_daily_switch({"selection_token": selected_other["selection_token"],
                                                         "confirmed": True})
                    controller.end_game_and_reset()
                finally:
                    release.set()
                wait_for(lambda: not controller.state()["busy"])
            saved = controller.store.load()
            self.assertEqual(saved.moves, [])
            self.assertEqual(saved.board_fen, chess.STARTING_FEN)
            self.assertFalse(controller.chesscom_selection()["daily"]["active"])
            self.assertEqual(len(calls), 2)  # e2-e4, then e4-e2; no e7-e5.


if __name__ == "__main__":
    unittest.main()

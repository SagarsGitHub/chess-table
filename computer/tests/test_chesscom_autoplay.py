"""Timed recorded-game playback retains one worker and durable move boundaries."""

import threading
import time
import unittest
from unittest.mock import patch

from chess_table_host.api_errors import ApiError
from chess_table_host.game_errors import GameStateError
from test_chesscom_gui import next_intent, start_replay
from test_gui import game_controller, wait_for


def autoplay_intent(controller, wait_seconds=0.1):
    return {**next_intent(controller), "wait_seconds": wait_seconds}


class ChessComAutoplayTests(unittest.TestCase):
    def test_delay_starts_after_committed_ply_and_pause_keeps_next_manual(self):
        with game_controller() as controller:
            start_replay(controller)
            timings = []
            step = controller._chesscom_step

            def timed_step():
                started = time.monotonic()
                step()
                timings.append((started, time.monotonic(), len(controller._load_checkpoint().moves)))
                if len(timings) == 2:
                    controller.chesscom_replay_pause({})

            with patch.object(controller, "_chesscom_step", side_effect=timed_step):
                controller.chesscom_replay_autoplay(autoplay_intent(controller))
                wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], ["e2e4", "e7e5"])
            self.assertEqual([item[2] for item in timings], [1, 2])
            self.assertGreaterEqual(timings[1][0] - timings[0][1], 0.095)
            self.assertFalse(controller._chesscom_autoplay)
            self.assertTrue(controller.chesscom_selection()["replay"]["can_next"])
            controller.chesscom_replay_next(next_intent(controller))
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(len(controller.state()["moves"]), 3)

    def test_pause_finishes_active_move_without_motor_stop(self):
        with game_controller() as controller:
            start_replay(controller)
            link, _geometry = controller._connection()
            began, release = threading.Event(), threading.Event()
            carry = link.carry

            def held_carry(*args):
                began.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test carry was not released")
                return carry(*args)

            with patch.object(link, "carry", side_effect=held_carry):
                controller.chesscom_replay_autoplay(autoplay_intent(controller))
                try:
                    self.assertTrue(began.wait(timeout=5))
                    controller.chesscom_replay_pause({})
                    self.assertFalse(controller._stop.is_set())
                    self.assertTrue(controller.state()["busy"])
                finally:
                    release.set()
                wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], ["e2e4"])
            self.assertIsNone(controller._load_checkpoint().pending)
            self.assertFalse(controller._chesscom_autoplay)

    def test_stale_or_duplicate_autoplay_fences_never_start_another_worker(self):
        with game_controller() as controller:
            start_replay(controller)
            intent = autoplay_intent(controller, 30)
            controller.chesscom_replay_autoplay(intent)
            try:
                with self.assertRaises(ApiError):
                    controller.chesscom_replay_autoplay(intent)
                wait_for(lambda: controller._activity == "replay_wait")
                with self.assertRaisesRegex(ApiError, "changed"):
                    controller.chesscom_replay_autoplay(intent)
                self.assertEqual(len(controller.state()["moves"]), 1)
            finally:
                controller.chesscom_replay_pause({})
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(len(controller.state()["moves"]), 1)

    def test_stop_during_long_wait_wakes_without_starting_next_move(self):
        with game_controller() as controller:
            start_replay(controller)
            controller.chesscom_replay_autoplay(autoplay_intent(controller, 3600))
            wait_for(lambda: controller._activity == "replay_wait")
            before = time.monotonic()
            controller.stop()
            wait_for(lambda: not controller.state()["busy"], timeout=1)
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(controller.state()["moves"], ["e2e4"])
            self.assertIsNone(controller._load_checkpoint().pending)
            self.assertFalse(controller._chesscom_autoplay)

    def test_pause_during_long_wait_wakes_without_starting_next_move(self):
        with game_controller() as controller:
            start_replay(controller)
            controller.chesscom_replay_autoplay(autoplay_intent(controller, 3600))
            wait_for(lambda: controller._activity == "replay_wait")
            before = time.monotonic()
            controller.chesscom_replay_pause({})
            wait_for(lambda: not controller.state()["busy"], timeout=1)
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(controller.state()["moves"], ["e2e4"])
            self.assertFalse(controller._stop.is_set())

    def test_invalid_delays_and_unknown_fields_are_rejected_before_motion(self):
        with game_controller() as controller:
            start_replay(controller)
            for delay in [True, False, None, "1", 0, 0.099, 3600.1, float("nan"), float("inf"), 10 ** 1000]:
                with self.subTest(delay=delay), self.assertRaisesRegex(ApiError, "wait_seconds"):
                    controller.chesscom_replay_autoplay(autoplay_intent(controller, delay))
            with self.assertRaisesRegex(ApiError, "unknown"):
                controller.chesscom_replay_autoplay({**autoplay_intent(controller), "extra": True})
            with self.assertRaisesRegex(ApiError, "unknown"):
                controller.chesscom_replay_pause({"stop": True})
            self.assertIsNone(controller.link)
            self.assertIsNone(controller._worker)

    def test_daily_game_cannot_use_recorded_autoplay(self):
        with game_controller() as controller:
            start_replay(controller)
            controller._chesscom_replay["game"]["kind"] = "daily"
            with self.assertRaisesRegex(ApiError, "historical and finished"):
                controller.chesscom_replay_autoplay(autoplay_intent(controller))
            self.assertIsNone(controller.link)

    def test_finished_recording_stops_at_last_ply(self):
        with game_controller() as controller:
            start_replay(controller)
            game = controller._chesscom_replay["game"]
            game["kind"] = "finished"
            game["moves"] = game["moves"][:2]
            game["positions"] = game["positions"][:3]
            controller.chesscom_replay_autoplay(autoplay_intent(controller))
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], ["e2e4", "e7e5"])
            self.assertFalse(controller._chesscom_autoplay)
            self.assertFalse(controller.chesscom_selection()["replay"]["can_next"])
            self.assertIsNone(controller.state()["error"])

    def test_autoplay_failure_stops_worker_and_never_retries(self):
        with game_controller() as controller:
            start_replay(controller)
            with patch.object(controller, "_chesscom_step", side_effect=GameStateError("route blocked")) as step:
                controller.chesscom_replay_autoplay(autoplay_intent(controller))
                wait_for(lambda: not controller.state()["busy"])
                self.assertEqual(step.call_count, 1)
            self.assertEqual(controller.state()["moves"], [])
            self.assertFalse(controller._chesscom_autoplay)
            self.assertIn("route blocked", controller.state()["error"])
            with self.assertRaisesRegex(ApiError, "not ready"):
                controller.chesscom_replay_autoplay(autoplay_intent(controller))

    def test_new_preview_during_wait_blocks_further_moves(self):
        with game_controller() as controller:
            start_replay(controller)
            controller.chesscom_replay_autoplay(autoplay_intent(controller, 0.2))
            wait_for(lambda: controller._activity == "replay_wait")
            other = controller.chesscom_list("historical", {"q": "Immortal"})["games"][0]
            controller.chesscom_select({"kind": "historical", "id": other["id"]})
            wait_for(lambda: not controller.state()["busy"])
            self.assertEqual(controller.state()["moves"], ["e2e4"])
            self.assertFalse(controller._chesscom_autoplay)
            self.assertIn("selection or board changed", controller.state()["error"])

    def test_reset_during_autoplay_wait_wakes_and_hands_off_to_reset(self):
        with game_controller() as controller:
            start_replay(controller)
            original_id = controller._load_checkpoint().host_game_id
            controller.chesscom_replay_autoplay(autoplay_intent(controller, 3600))
            wait_for(lambda: controller._activity == "replay_wait")
            controller.end_game_and_reset()
            wait_for(lambda: not controller.state()["busy"])
            saved = controller._load_checkpoint()
            self.assertEqual(saved.moves, [])
            self.assertNotEqual(saved.host_game_id, original_id)
            self.assertFalse(controller._chesscom_autoplay)
            self.assertFalse(controller.chesscom_selection()["replay"]["active"])


if __name__ == "__main__":
    unittest.main()

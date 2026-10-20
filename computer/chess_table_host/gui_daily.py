"""Explicit daily-game switching and 30-minute runtime synchronization.

The scheduler only fetches public recordings. Appended moves use the sole
checkpointed worker; STOP, faults, divergence and restarts never auto-resume.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import threading
import time

from .api_errors import ApiError
from .game_errors import GameStateError
from .replay_guard import save_replay_marker


DAILY_POLL_SECONDS = 1800


class ChessComDailyMixin:
    def _init_chesscom_daily(self):
        self._chesscom_daily = None
        self._chesscom_poll_stop = threading.Event()
        self._chesscom_poll_thread = None
        self._chesscom_checking = False

    def _bind_recording(self, game, token, state):
        marker = {"schema_version": 1, "host_game_id": state.host_game_id,
                  "kind": game["kind"], "game_id": game["id"]}
        save_replay_marker(self.store.path, marker)
        self._chesscom_marker = marker
        self._chesscom_replay = {"game": deepcopy(game), "selection_token": token,
                                 "host_game_id": state.host_game_id}

    def _daily_active(self, state):
        return bool(self._chesscom_daily and self._chesscom_daily["host_game_id"] == state.host_game_id)

    def _daily_view(self, state, cached=False):
        selected = self._chesscom_selection
        active = self._daily_active(state)
        daily = self._chesscom_daily if active else {}
        return {**{key: daily.get(key) for key in (
            "game_id", "title", "username", "next_check_at", "last_checked_at", "status",
        )}, "active": active, "paused": daily.get("paused", False),
            "checking": self._chesscom_checking, "poll_seconds": DAILY_POLL_SECONDS,
            "can_check": bool(active and not self._chesscom_checking),
            "can_switch": bool(not cached and self._worker is None and state.source_kind is None and
                               state.pending is None and state.reset is None and selected and
                               selected.get("kind") == "daily" and selected.get("replay_supported"))}

    def _schedule_daily_check(self, daily):
        daily["next_check"] = time.monotonic() + DAILY_POLL_SECONDS
        daily["next_check_at"] = (datetime.now(timezone.utc) + timedelta(seconds=DAILY_POLL_SECONDS)).isoformat()

    def _ensure_daily_poll(self):
        if self._chesscom_poll_thread is None:
            self._chesscom_poll_thread = threading.Thread(target=self._daily_poll_loop, daemon=True)
            self._chesscom_poll_thread.start()

    def _daily_poll_loop(self):
        while not self._chesscom_poll_stop.wait(0.5):
            try:
                self._daily_poll_once()
            except (OSError, ValueError, GameStateError):
                # API errors are recorded by check; a storage failure must not
                # turn the timer into a second motion/recovery owner.
                continue

    def _daily_poll_once(self):
        with self._guard:
            if self._chesscom_poll_stop.is_set() or not self._chesscom_daily:
                return
            daily = self._chesscom_daily
            if time.monotonic() < daily["next_check"] or self._chesscom_checking:
                return
            if not self._daily_active(self._load_checkpoint()):
                if self._worker is None:
                    self._chesscom_daily = None
                return
        self.chesscom_daily_check({})

    def chesscom_daily_switch(self, body):
        self._chesscom_fields(body, {"selection_token", "confirmed"})
        if body.get("confirmed") is not True:
            raise ApiError("confirm the table and parking match the saved physical state", HTTPStatus.BAD_REQUEST)
        with self._guard:
            if not self._chesscom_token or body.get("selection_token") != self._chesscom_token:
                raise ApiError("selection changed; refresh the daily preview")
            state = self._load_checkpoint()
            if not self._daily_view(state)["can_switch"]:
                raise ApiError("daily switching needs a supported daily game, idle board, and no pending or remote-owned work")
            game = deepcopy(self._chesscom_selection)
            if (self._daily_active(state) and not self._chesscom_daily["paused"] and
                    self._chesscom_daily["selection_token"] == self._chesscom_token):
                return self.chesscom_selection()
            self.runner.state = state
            self._bind_recording(game, self._chesscom_token, state)
            self._chesscom_daily = {
                "game_id": game["id"], "title": game["title"], "username": game.get("username") or "ofroni",
                "selection_token": self._chesscom_token, "host_game_id": state.host_game_id,
                "paused": False, "last_checked_at": None, "status": "Resetting, then setting the selected daily position.",
            }
            self._schedule_daily_check(self._chesscom_daily)
            self._start_worker(self._chesscom_daily_switch_loop, mode="daily_sync")
            self._ensure_daily_poll()
            return self.chesscom_selection()

    def _chesscom_daily_switch_loop(self):
        try:
            self._reset_loop()
            if self._stop.is_set() or self._reset_requested.is_set():
                return
            with self._guard:
                state = self._load_checkpoint()
                game = self._chesscom_replay["game"]
                self._bind_recording(game, self._chesscom_daily["selection_token"], state)
                self._chesscom_daily["host_game_id"] = state.host_game_id
            self._chesscom_daily_catchup()
        except BaseException:
            with self._guard:
                self._chesscom_daily["paused"] = True
                self._chesscom_daily["status"] = "Daily setup paused; inspect host recovery before another switch."
            raise

    def _chesscom_daily_catchup(self):
        try:
            while not self._stop.is_set() and not self._reset_requested.is_set():
                with self._guard:
                    if self._stop.is_set() or self._reset_requested.is_set():
                        return
                    state = self._load_checkpoint()
                    replay = self._chesscom_replay
                    if (not self._daily_active(state) or not self._chesscom_bound(state) or
                            self._chesscom_daily["paused"] or state.source_kind is not None or
                            state.pending is not None or state.reset is not None):
                        raise GameStateError("daily board changed; inspect recovery before following")
                    game = replay["game"]
                    index = len(state.moves)
                    if (state.moves != game["moves"][:index] or index >= len(game["positions"]) or
                            state.board_fen != game["positions"][index]):
                        raise GameStateError("daily position differs from the recorded prefix")
                    if index == len(game["moves"]):
                        self._chesscom_daily["needs_catchup"] = False
                        self._chesscom_daily["status"] = "Daily position is current. Checking Chess.com every 30 minutes."
                        return
                    self.runner.state = state
                self._chesscom_step()
        except BaseException:
            with self._guard:
                self._chesscom_daily["paused"] = True
                self._chesscom_daily["status"] = "Daily following paused; inspect the checkpoint and recovery controls."
            raise

    def _daily_worker_finished(self):
        """Close the check-versus-worker-exit race while holding the owner guard."""
        daily = self._chesscom_daily
        if (not daily or not daily.get("needs_catchup") or daily["paused"] or
                self._chesscom_poll_stop.is_set() or self._stop.is_set() or self._error):
            return
        try:
            state = self._load_checkpoint()
        except (OSError, ValueError) as exc:
            daily["paused"] = True
            daily["status"] = "Daily following paused because the checkpoint is unavailable."
            self._error = str(exc)
            return
        if (self._daily_active(state) and state.pending is None and state.reset is None and
                state.source_kind is None and len(state.moves) < len(self._chesscom_replay["game"]["moves"])):
            self.runner.state = state
            self._start_worker(self._chesscom_daily_catchup, mode="daily_sync")

    def chesscom_daily_check(self, body):
        self._chesscom_fields(body, set())
        with self._guard:
            state = self._load_checkpoint()
            if not self._daily_active(state) or self._chesscom_checking:
                raise ApiError("no active daily game is ready for a check")
            daily = self._chesscom_daily
            self._chesscom_checking = True
            self._schedule_daily_check(daily)
        try:
            result = self._chesscom_client.daily(daily["username"], refresh=True)
            game = next((game for game in result["games"] if game["id"] == daily["game_id"]), None)
            with self._guard:
                if self._chesscom_daily is not daily or self._chesscom_poll_stop.is_set():
                    return self.chesscom_selection()
                daily["last_checked_at"] = datetime.now(timezone.utc).isoformat()
                state = self._load_checkpoint()
                if not self._daily_active(state):
                    return self.chesscom_selection()
                old = self._chesscom_replay["game"]
                if (not game or not game.get("replay_supported") or
                        game["moves"][:len(old["moves"])] != old["moves"] or len(game["moves"]) < len(old["moves"])):
                    daily["paused"] = True
                    daily["status"] = "Current game is missing, unsupported, or its history changed; inspect and reselect explicitly."
                    return self.chesscom_selection()
                game = deepcopy({**game, "kind": "daily", "username": daily["username"]})
                self._chesscom_replay["game"] = game
                if len(state.moves) < len(game["moves"]):
                    daily["needs_catchup"] = True
                if self._chesscom_token == daily["selection_token"]:
                    self._chesscom_selection = deepcopy(game)
                if daily["paused"]:
                    daily["status"] = "Daily updates checked. Following is paused; use Set table to resume explicitly."
                elif self._worker is not None:
                    daily["status"] = "Daily updates checked; table worker is active."
                elif self._error or state.pending is not None or state.reset is not None or self._stop.is_set():
                    daily["paused"] = True
                    daily["status"] = "Daily updates available; inspect recovery before following."
                elif len(state.moves) < len(game["moves"]):
                    self.runner.state = state
                    daily["status"] = "Applying newly recorded daily moves."
                    self._start_worker(self._chesscom_daily_catchup, mode="daily_sync")
                else:
                    daily["status"] = "Daily position is current. Checking Chess.com every 30 minutes."
                return self.chesscom_selection()
        except (OSError, ValueError, GameStateError) as exc:
            with self._guard:
                if self._chesscom_daily is daily:
                    daily["status"] = f"Daily update unavailable: {exc}. Next check in 30 minutes."
            raise
        finally:
            with self._guard:
                self._chesscom_checking = False

"""Interruptible playback of recorded games through the existing GUI worker."""

from __future__ import annotations

from http import HTTPStatus
import math
import time

from .api_errors import ApiError
from .game_errors import GameStateError


class ChessComPlaybackMixin:
    """Timed playback owns the same worker, runner and checkpoint as Next."""

    def chesscom_replay_autoplay(self, body: dict) -> dict:
        self._chesscom_fields(body, {
            "selection_token", "host_game_id", "expected_ply", "expected_fen", "wait_seconds",
        })
        delay = body.get("wait_seconds")
        if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or
                not 0.1 <= delay <= 3600 or not math.isfinite(delay)):
            raise ApiError("wait_seconds must be a number between 0.1 and 3600", HTTPStatus.BAD_REQUEST)
        with self._guard:
            view = self.chesscom_selection()["replay"]
            if (type(body.get("expected_ply")) is not int or
                    any(body.get(key) != view[key] for key in (
                        "selection_token", "host_game_id", "expected_fen",
                    )) or body["expected_ply"] != view["index"]):
                raise ApiError("replay position or selection changed; inspect the latest checkpoint")
            replay = self._chesscom_replay
            if not replay or replay["game"].get("kind") not in {"historical", "finished"}:
                raise ApiError("autoplay is available for historical and finished games")
            if not view["can_next"]:
                raise ApiError("replay is not ready for autoplay")
            self.runner.state = self._load_checkpoint()
            self._chesscom_wait_seconds = float(delay)
            self._chesscom_pause.clear()
            self._chesscom_autoplay = True
            try:
                self._start_worker(self._chesscom_autoplay_loop, mode="recorded_replay")
            except BaseException:
                self._chesscom_autoplay = False
                raise
            return self.chesscom_selection()

    def chesscom_replay_pause(self, body: dict) -> dict:
        """Finish an active chess move, then release playback without STOP."""
        self._chesscom_fields(body, set())
        with self._guard:
            if self._chesscom_autoplay:
                self._chesscom_pause.set()
                self._status = "Pausing autoplay after the current recorded move..."
            return self.chesscom_selection()

    def _chesscom_autoplay_loop(self) -> None:
        """Commit one move at a time, waiting only after successful commits.

        A pause is a move boundary request. STOP retains its existing immediate
        motion semantics. A reset request leaves the worker's ordinary reset
        handoff in charge after this loop returns.
        """
        try:
            while True:
                with self._guard:
                    if (self._stop.is_set() or self._chesscom_pause.is_set() or
                            self._reset_requested.is_set()):
                        break
                    state = self._load_checkpoint()
                    replay = self._chesscom_replay
                    if (not replay or not self._chesscom_bound(state) or
                            state.host_game_id != replay["host_game_id"] or
                            self._chesscom_token != replay["selection_token"] or
                            state.source_kind is not None or state.reset is not None):
                        raise GameStateError("replay selection or board changed; autoplay paused")
                    game = replay["game"]
                    index = len(state.moves)
                    if (state.moves != game["moves"][:index] or index >= len(game["positions"]) or
                            state.board_fen != game["positions"][index]):
                        raise GameStateError("replay checkpoint no longer matches the recording; autoplay paused")
                    if index == len(game["moves"]):
                        break
                    if state.pending is not None and (
                            state.pending.phase != "ready" or state.pending.uci != game["moves"][index]):
                        raise GameStateError("inspect the pending move before continuing autoplay")
                    self.runner.state = state
                self._chesscom_step()
                with self._guard:
                    if (self._stop.is_set() or self._chesscom_pause.is_set() or
                            self._reset_requested.is_set()):
                        break
                    committed = self._load_checkpoint()
                    if len(committed.moves) != index + 1 or committed.pending is not None:
                        raise GameStateError("recorded move did not commit; autoplay paused")
                    if len(committed.moves) == len(game["moves"]):
                        break
                    delay = self._chesscom_wait_seconds
                    self._set_activity("replay_wait", f"Recorded move committed; next move in {delay:g} seconds.")
                deadline = time.monotonic() + delay
                while not (self._stop.is_set() or self._chesscom_pause.is_set() or
                           self._reset_requested.is_set()):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    # Pause wakes immediately; STOP and reset wake within 50 ms.
                    self._chesscom_pause.wait(min(remaining, 0.05))
            if self._chesscom_pause.is_set() and not self._stop.is_set():
                self._set_activity("idle", "Autoplay paused after the current recorded move.")
        finally:
            with self._guard:
                self._chesscom_autoplay = False

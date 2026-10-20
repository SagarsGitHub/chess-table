"""Chess.com browsing, preview selection and fenced recorded-move replay.

Selection is a preview. Confirmed daily switching, position-fenced Next, and
timed playback share the controller's existing worker and physical runner.
The ordinary game checkpoint remains the authority for physical progress.
"""

from __future__ import annotations

from copy import deepcopy
from http import HTTPStatus
import json
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import chess

from .api_errors import ApiError
from .chesscom import ChessComClient, parse_pgn
from .replay_guard import read_replay_marker


class ChessComMixin:
    """Methods on GameController; no additional runner or serial connection."""

    def _init_chesscom(self) -> None:
        self._chesscom_client = ChessComClient()
        self._chesscom_selection: dict[str, Any] | None = None
        self._chesscom_token: str | None = None
        self._chesscom_replay: dict[str, Any] | None = None
        self._chesscom_marker = read_replay_marker(self.store.path)
        self._chesscom_autoplay = False
        self._chesscom_wait_seconds = 2.0
        self._chesscom_pause = threading.Event()
        self._init_chesscom_daily()

    @staticmethod
    def _chesscom_fields(body: dict, allowed: set[str]) -> None:
        if set(body) - allowed:
            raise ApiError("unknown Chess.com request fields", HTTPStatus.BAD_REQUEST)

    def chesscom_list(self, kind: str, query: dict[str, str]) -> dict:
        """Fetch account data outside the motion guard; network never moves pieces."""
        allowed = {
            "daily": {"username"}, "archives": {"username"},
            "finished": {"username", "year", "month", "q"}, "historical": {"q"},
        }
        if kind not in allowed:
            raise ApiError("unknown Chess.com collection", HTTPStatus.NOT_FOUND)
        self._chesscom_fields(query, allowed[kind])
        username = query.get("username", "ofroni")
        if kind == "daily":
            return self._chesscom_client.daily(username)
        if kind == "archives":
            return self._chesscom_client.archives(username)
        if kind == "finished":
            return self._chesscom_client.finished(
                username, query.get("year"), query.get("month"), query.get("q", ""),
            )
        search = query.get("q", "")
        if not isinstance(search, str) or len(search) > 200:
            raise ApiError("search must be at most 200 characters", HTTPStatus.BAD_REQUEST)
        library = json.loads(Path(__file__).with_name("historical_games.json").read_text(encoding="utf-8"))
        games = []
        for entry in library:
            if search.casefold() not in " ".join(str(entry.get(key, "")) for key in (
                "title", "white", "black", "date", "description",
            )).casefold():
                continue
            parsed = parse_pgn(entry["pgn"], require_finished=True)
            games.append({**entry, **parsed, "fen": parsed["positions"][-1],
                          "replay_supported": True, "unsupported_reason": None, "kind": "historical"})
        return {"games": games}

    def chesscom_select(self, body: dict) -> dict:
        self._chesscom_fields(body, {"kind", "id", "username", "year", "month"})
        kind, game_id = body.get("kind"), body.get("id")
        if not isinstance(kind, str) or kind not in {"daily", "finished", "historical"} or not isinstance(game_id, str):
            raise ApiError("choose a collection and game id", HTTPStatus.BAD_REQUEST)
        query = {key: value for key, value in body.items() if key not in {"kind", "id"}}
        collection = self.chesscom_list(kind, query)
        games = collection["games"]
        game = next((game for game in games if game["id"] == game_id), None)
        if game is None:
            raise ApiError("game is absent from this collection; refresh the list", HTTPStatus.NOT_FOUND)
        with self._guard:
            same = self._chesscom_selection is not None and all(
                self._chesscom_selection.get(key) == {**game, "kind": kind}.get(key)
                for key in ("kind", "id", "pgn")
            )
            self._chesscom_selection = deepcopy({**game, "kind": kind, "username": collection.get("username")})
            if not same:
                self._chesscom_token = str(uuid4())
            return self.chesscom_selection()

    def _chesscom_bound(self, state) -> bool:
        """A completed physical reset creates a new host identity and unbinds replay."""
        # Pure predicate: a delayed browser snapshot must not clear a newer binding.
        return bool(self._chesscom_marker and self._chesscom_marker["host_game_id"] == state.host_game_id)

    def chesscom_selection(self) -> dict:
        with self._guard:
            state, cached = self.store.read_snapshot()
            bound = self._chesscom_bound(state)
            replay = self._chesscom_replay if bound else None
            selected = self._chesscom_selection
            busy = self._worker is not None
            ready = not cached and not busy and state.source_kind is None and state.reset is None
            fresh = (ready and not state.moves and state.pending is None and
                     state.initial_fen == chess.STARTING_FEN and state.board_fen == chess.STARTING_FEN)
            index = len(state.moves) if bound else 0
            game = replay["game"] if replay else selected
            total = len(game.get("moves", [])) if game else 0
            matching = bool(replay and replay["selection_token"] == self._chesscom_token)
            prefix = bool(replay and state.moves == game["moves"][:index] and
                          index < len(game["positions"]) and state.board_fen == game["positions"][index])
            pending_ready = (state.pending is None or (
                state.pending.phase == "ready" and index < total and state.pending.uci == game["moves"][index]
            )) if game else False
            can_next = bool(ready and not self._error and matching and prefix and pending_ready and index < total)
            if self._daily_active(state):
                can_next = False
            can_start = bool(fresh and not bound and selected and selected.get("replay_supported") and total)
            if bound and replay is None:
                status = "Saved replay: inspect recovery and reset the physical board before a new game."
            elif bound and not matching:
                status = "Another game is on the table. Reset the board before switching replay."
            elif bound and index == total and prefix:
                status = "Recorded moves complete. Reset the board to select another replay."
            elif bound and (not prefix or not pending_ready):
                status = "Replay paused. Inspect the checkpoint and use local recovery controls."
            elif bound and self._error:
                status = f"Replay paused: {self._error}. Inspect recovery or reset before continuing."
            elif bound and self._chesscom_autoplay and self._activity == "replay_wait":
                status = f"Autoplay waits {self._chesscom_wait_seconds:g} seconds between plies."
            elif bound:
                status = "Executing recorded move." if busy else "Ready for the next recorded move."
            elif selected and not selected.get("replay_supported"):
                status = selected.get("unsupported_reason") or "This game is available for preview only."
            else:
                status = "Confirm standard setup to begin." if can_start else "A fresh standard board is required for replay."
            return {
                "selection": deepcopy(selected), "selection_token": self._chesscom_token,
                "daily": self._daily_view(state, cached),
                "replay": {"active": bound, "index": index, "total": total,
                           "autoplay": self._chesscom_autoplay, "wait_seconds": self._chesscom_wait_seconds,
                           "can_pause": self._chesscom_autoplay,
                           "can_start": can_start, "can_next": can_next, "status": status,
                           "host_game_id": state.host_game_id, "expected_fen": state.board_fen,
                           "selection_token": replay["selection_token"] if replay else self._chesscom_token},
            }

    def chesscom_replay_start(self, body: dict) -> dict:
        self._chesscom_fields(body, {"confirmed", "selection_token"})
        if body.get("confirmed") is not True:
            raise ApiError("confirm all pieces are at standard setup first", HTTPStatus.BAD_REQUEST)
        with self._guard:
            if not self._chesscom_token or body.get("selection_token") != self._chesscom_token:
                raise ApiError("selection changed; refresh the preview")
            view = self.chesscom_selection()
            if (view["replay"]["active"] and self._chesscom_replay and
                    self._chesscom_replay["selection_token"] == self._chesscom_token):
                return view
            if not view["replay"]["can_start"]:
                raise ApiError("replay needs a fresh, unbound standard board and a supported game")
            state = self._load_checkpoint()
            self._bind_recording(self._chesscom_selection, self._chesscom_token, state)
            return self.chesscom_selection()

    def chesscom_replay_next(self, body: dict) -> dict:
        self._chesscom_fields(body, {"selection_token", "host_game_id", "expected_ply", "expected_fen"})
        with self._guard:
            view = self.chesscom_selection()["replay"]
            if (type(body.get("expected_ply")) is not int or
                    any(body.get(key) != view[key] for key in ("selection_token", "host_game_id", "expected_fen")) or
                    body["expected_ply"] != view["index"]):
                raise ApiError("replay position or selection changed; inspect the latest checkpoint")
            if not view["can_next"]:
                raise ApiError("replay is not ready for another recorded move")
            state = self._load_checkpoint()
            self.runner.state = state
            self._start_worker(self._chesscom_step, mode="recorded_replay")
            return self.chesscom_selection()

    def _chesscom_step(self) -> None:
        """Run exactly one recorded move through the existing durable runner."""
        link, geometry = self._connection()
        if self._stop.is_set():
            return
        game = self._chesscom_replay["game"]
        uci = game["moves"][len(self.runner.state.moves)]
        self._set_activity("moving", f"Replaying recorded move {uci}...")
        if self.runner.state.pending is None:
            self.runner.prepare_move(uci, geometry)
        self.runner.execute_pending(
            self._motion_link(link), geometry, self._confirm_action, self._approve_route,
            confirmation_source="simulation" if self.simulate else "controller_done",
        )
        self._set_activity("idle", f"Recorded move {uci} committed.")

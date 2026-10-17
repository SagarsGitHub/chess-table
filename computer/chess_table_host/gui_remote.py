"""Remote requests, position fences and durable receipt views.

Methods run on the owning GameController and use its existing guard, move queue
and worker. Remote requests never create a second runner or serial owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
import threading
from typing import Any
import chess
from .api_errors import ApiError, ResetNotAccepted
from .gui_snapshot import _active_action
from .game import GameState, GameStateError
from .checkpoint import RemoteMoveReceipt
from .remote_contract import (
    MoveRequest, PocketMageResetRequest, ResolveMoveRequest, SessionResetRequest, SessionStartRequest,
    request_identifier,
)
from .remote_session import RemoteSession
from .host_runtime import _resolve_engine_path
from .reset_receipt import RESET_TERMINAL, RemoteResetReceipt, settle_reset_receipts


@dataclass
class RemoteMoveTicket:
    receipt: RemoteMoveReceipt
    ready: threading.Event = field(default_factory=threading.Event)
    error: Exception | None = None
    claimed: bool = False
    cancelled: bool = False


class RemoteControlMixin:
    """Internal methods of GameController; owns no separate session state."""

    @staticmethod
    def _receipt_view(receipt: RemoteMoveReceipt, state: GameState, busy: bool) -> dict[str, Any]:
        value = {"schema_version": 1, **receipt.to_json()}
        # The checkpoint keeps the last physical phase. If the worker is gone
        # while that phase is uncertain, expose reconciliation_required even
        # after a process restart; no separate volatile flag is needed.
        if (state.pending is not None and
                state.pending.remote_request_id == receipt.request_id and
                state.pending.phase != "ready" and not busy):
            value["state"] = "reconciliation_required"
        return value

    def remote_status(self) -> dict[str, Any]:
        state, cached = self.store.read_snapshot()
        board = state.board()
        game_over = board.is_game_over(claim_draw=True)
        with self._guard:
            busy = self._worker is not None
            mode = self._mode
            waiting = self._waiting
            stopped = self._stop.is_set()
            reset_requested = self._reset_requested.is_set()
            error = self._error
        pending = _active_action(state)
        if pending is not None:
            pending = {key: pending[key] for key in (
                "kind", "uci", "phase", "next_action", "action_number",
                "total_actions", "action",
            )}
        if pending is not None and pending["phase"] != "ready" and not busy:
            phase = "reconciliation_required"
        elif busy and (reset_requested or mode is None):
            phase = "resetting"
        elif state.reset is not None:
            phase = "resetting" if busy else "paused"
        elif state.pending is not None:
            phase = "moving" if busy else "paused"
        elif busy and waiting and mode in {"external", "human_engine"}:
            phase = "waiting_for_move"
        elif busy and self._activity == "engine_thinking":
            phase = "engine_thinking"
        elif busy:
            phase = "moving"
        elif game_over:
            phase = "game_over"
        elif error:
            phase = "error"
        else:
            phase = "idle"
        return {
            "schema_version": 1,
            "host_game_id": state.host_game_id,
            "source_kind": state.source_kind,
            "source_game_id": state.source_game_id,
            "mode": state.remote_mode if state.source_kind else mode,
            "human": state.remote_human if state.source_kind else self._human,
            "ply": len(state.moves),
            "last_move_uci": state.moves[-1] if state.moves else None,
            "fen": state.board_fen,
            "turn": "white" if board.turn else "black",
            "check": board.is_check(),
            "result": board.result(claim_draw=True) if game_over else None,
            # These describe the committed position, including an idle board.
            # can_move is the separate authority to submit an actual move.
            "legal_moves": ([move.uci() for move in board.legal_moves]
                            if not game_over and state.pending is None and state.reset is None else []),
            "phase": phase,
            "can_move": bool(not cached and busy and waiting and mode in {"external", "human_engine"} and
                             state.pending is None and state.reset is None and
                             not stopped and not reset_requested),
            "pending": pending,
            "busy": busy,
            "checkpoint_cached": cached,
            # Worker errors can contain private filesystem or serial paths.
            "error": "host operation failed" if error is not None else None,
        }

    def remote_start(self, body: dict[str, Any], *, source_kind: str = "aina") -> dict[str, Any]:
        request = SessionStartRequest.from_body(body, source_kind=source_kind)
        worker_mode = "self_play" if request.mode == "engine_engine" else request.mode
        if request.mode in {"human_engine", "engine_engine"}:
            try:
                _resolve_engine_path(self.stockfish_path)
            except GameStateError as exc:
                raise ApiError("Stockfish unavailable; check host engine configuration",
                               HTTPStatus.SERVICE_UNAVAILABLE) from exc
        with self._guard:
            state = self._load_checkpoint()
            if source_kind == "pocketmage" and state.reset is not None:
                raise ApiError("inspect and continue the saved board reset using local host controls")
            if any(receipt.state not in RESET_TERMINAL for receipt in state.remote_reset_receipts.values()):
                raise ApiError("a physical board reset is already requested")
            if RemoteSession.bind(state, request, busy=self._worker is not None):
                self.store.save(state)
                self.runner.state = state
            if self._worker is not None:
                if self._mode != worker_mode or self._human != request.human:
                    raise ApiError("another game worker is already running")
            elif (state.reset is None and
                  (state.pending is None or state.pending.phase == "ready") and
                  not state.board().is_game_over(claim_draw=True)):
                self.runner.state = state
                self._start_worker(self._game_loop, mode=worker_mode, human=request.human)
        return self.remote_status()

    def remote_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        """End the fenced game at a move boundary, then restore standard setup."""
        request = SessionResetRequest.from_body(body)
        with self._guard:
            state = self._load_checkpoint()
            if state.source_kind not in {None, "aina"}:
                raise ApiError("checkpoint is not bound to this remote source")
            if state.host_game_id != request.host_game_id:
                raise ApiError("host_game_id does not match the current game")
            # A matching retry observes the existing reset worker. Once reset
            # completes, the fresh UUID rejects retries for the previous game.
            if self._worker is None or self._mode is not None:
                self.end_game_and_reset()
            return self.remote_status()

    @staticmethod
    def _reset_receipt_view(receipt: RemoteResetReceipt, state: GameState, busy: bool) -> dict[str, Any]:
        active = _active_action(state) if receipt.host_game_id == state.host_game_id else None
        phase = receipt.state
        if receipt.state not in RESET_TERMINAL:
            if active is not None and active["phase"] != "ready" and not busy:
                phase = "reconciliation_required"
            elif active is not None:
                phase = "resetting" if active["kind"] == "reset" else "finishing_move"
        return {
            "schema_version": 1, **receipt.to_json(), "phase": phase,
            "action_number": active["action_number"] if active else 0,
            "total_actions": active["total_actions"] if active else 0,
        }

    def pocketmage_reset_receipt(self, request_id: str) -> dict[str, Any]:
        request_id = request_identifier(request_id, "request_id")
        with self._guard:
            state = self._load_checkpoint()
            receipt = state.remote_reset_receipts.get(request_id)
            if receipt is None:
                raise ApiError("reset request was never prepared", HTTPStatus.NOT_FOUND)
            return self._reset_receipt_view(receipt, state, self._worker is not None)

    def pocketmage_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        request = PocketMageResetRequest.from_body(body)
        receipt = request.receipt()
        with self._guard:
            state = self._load_checkpoint()
            existing = state.remote_reset_receipts.get(request.request_id)
            if existing is not None:
                if not existing.same_intent(receipt):
                    raise ApiError("request_id was already used for another reset")
                # A duplicate observes exactly the saved outcome. In particular,
                # it cannot resume a STOPped or rebooted physical reset.
                return self._reset_receipt_view(existing, state, self._worker is not None)

            def validate(current: GameState) -> None:
                def reject(reason: str) -> None:
                    raise ResetNotAccepted(reason, dict(vars(request)))

                if (current.source_kind != "pocketmage" or
                        current.source_game_id != request.source_game_id):
                    reject("checkpoint is not bound to this PocketMage session")
                if (current.host_game_id != request.host_game_id or
                        len(current.moves) != request.expected_ply or
                        current.board_fen != request.expected_fen):
                    reject("reset position fence does not match the current game")
                if any(item.state not in RESET_TERMINAL for item in current.remote_reset_receipts.values()):
                    reject("another physical reset is already requested")
                active = current.reset or current.pending
                if active is not None and self._worker is None:
                    reject("inspect and continue the saved action using local host controls")
                if self._stop.is_set() and self._worker is not None:
                    reject("wait for the STOPped worker to settle")
                if current.reset is not None or (self._worker is not None and self._mode is None):
                    reject("board reset is already running")
                if current.initial_fen != chess.Board().fen():
                    reject("board reset requires a standard-start game")

            self.store.register_reset_receipt(receipt, validate)
            try:
                self.end_game_and_reset()
            except BaseException:
                saved = self._load_checkpoint()
                if settle_reset_receipts(saved):
                    self.store.save(saved)
                raise
            return self.pocketmage_reset_receipt(request.request_id)

    def remote_resolve_move(self, body: dict[str, Any]) -> dict[str, Any]:
        """Persist legal interpretation under the same guard as move submission."""
        request = ResolveMoveRequest.from_body(body)
        with self._guard:
            state = self._load_checkpoint()
            if state.source_kind != "aina":
                raise ApiError("checkpoint is not bound to this remote source")
            resolution, changed = RemoteSession.resolve(
                state, request, can_move=self.remote_status()["can_move"],
            )
            if changed:
                self.store.save(state)
                self.runner.state = state
            return resolution.payload

    def remote_receipt(self, request_id: str, *, source_kind: str = "aina") -> dict[str, Any]:
        request_id = request_identifier(request_id, "request_id")
        state = self._load_checkpoint()
        if state.source_kind not in {None, source_kind}:
            raise ApiError("checkpoint is not bound to this remote source")
        receipt = state.remote_receipts.get(request_id)
        if receipt is None:
            raise ApiError("move request was never prepared", HTTPStatus.NOT_FOUND)
        with self._guard:
            busy = self._worker is not None
        return self._receipt_view(receipt, state, busy)

    def remote_submit_move(self, body: dict[str, Any], *, source_kind: str = "aina") -> dict[str, Any]:
        request = MoveRequest.from_body(body)
        request_id = request.request_id
        receipt = request.receipt()
        with self._guard:
            state = self._load_checkpoint()
            # Enforce listener ownership even for duplicate receipts. Source
            # IDs coordinate sessions; they are not device authentication.
            if state.source_kind != source_kind:
                raise ApiError("checkpoint is not bound to this remote source")
            existing = RemoteSession.existing_receipt(state, request)
            if existing is not None:
                return self._receipt_view(existing, state, self._worker is not None)
            ticket = self._remote_ticket
            if ticket is not None and ticket.receipt.request_id == request_id:
                if not request.matches(ticket.receipt):
                    raise ApiError("request_id was already used for another move")
            else:
                if (self._mode not in {"external", "human_engine"} or
                        self._worker is None or not self._waiting or self._stop.is_set() or
                        self._reset_requested.is_set() or state.pending is not None or
                        state.reset is not None or self._remote_ticket is not None):
                    raise ApiError("remote game is not waiting for a move")
                RemoteSession.check_position(state, request)
                ticket = RemoteMoveTicket(receipt)
                self._remote_ticket = ticket
                self._moves.put_nowait(ticket)
                self._waiting = False
                self._activity = "moving"
                self._status = f"Planning {request.uci}..."
        assert ticket is not None
        if not ticket.ready.wait(timeout=60):
            try:
                return self.remote_receipt(request_id, source_kind=source_kind)
            except ApiError as exc:
                if exc.status != HTTPStatus.NOT_FOUND:
                    raise
            raise ApiError("preparation still pending; retry the same request_id",
                           HTTPStatus.GATEWAY_TIMEOUT)
        try:
            return self.remote_receipt(request_id, source_kind=source_kind)
        except ApiError as exc:
            if exc.status != HTTPStatus.NOT_FOUND or ticket.error is None:
                raise
            raise ticket.error

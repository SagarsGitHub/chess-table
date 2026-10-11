"""Single-worker game/reset loops and cancellation-aware motion.

Methods run on the owning GameController. The registered worker retains the
runner and serial link across game-to-reset handoff; STOP uses the same guard.
"""

from __future__ import annotations

from queue import Empty, Queue
import sys
import threading
import traceback

import chess
import chess.engine

from .api_errors import ApiError
from .game import GameStateError, MoveDeferred, ReconciliationRequired
from .planner import PlanError
from .routing import RouteError, RouteGeometry
from .serial_link import LinkError
from .host_runtime import _archive_checkpoint, _feasible_engine_moves, _resolve_engine_path
from .gui_remote import RemoteMoveTicket


class GameWorkerMixin:
    """Internal methods of GameController; owns no separate session state."""

    def _start_worker(self, task, *, mode: str | None = None, human: str = "white",
                      reset_after: bool = False) -> None:
        """Give a single background worker ownership of the game and link."""
        with self._guard:
            if self._worker is not None:
                raise ApiError("a game or board reset is already running")
            self._stop.clear()
            self._reset_requested.clear()
            if reset_after:
                self._reset_requested.set()
            self._moves = Queue(maxsize=1)
            self._remote_ticket = None
            self._mode = mode
            self._human = human
            self._activity = "starting"
            self._waiting = False
            self._status = "Connecting to the table..." if not self.simulate else "Starting simulation..."
            self._error = None
            worker = threading.Thread(target=self._work, args=(task,), daemon=True)
            self._worker = worker
            worker.start()

    def _work(self, task) -> None:
        """Contain worker errors and hand off a completed game to board reset.

        The handoff keeps one worker registered throughout the transition so
        HTTP controls cannot start another game between the move and reset.
        """
        completed = False
        try:
            task()
            completed = True
        except MoveDeferred:
            self._set_activity("paused", "Paused at a completed action boundary; checkpoint saved.")
        except BaseException as exc:
            with self._guard:
                self._error = str(exc)
                self._status = str(exc)
                self._activity = "error"
            if isinstance(exc, (GameStateError, PlanError, RouteError, LinkError, OSError,
                                ValueError, chess.engine.EngineError)):
                print(f"Game paused: {exc}", file=sys.stderr)
            else:
                traceback.print_exc()
            if isinstance(exc, LinkError) and self.link is not None:
                interrupt = self._interrupt_thread
                if interrupt is not None and interrupt is not threading.current_thread():
                    interrupt.join(timeout=5)
                self.link.close()
                self.link = None
                self.geometry = None
        finally:
            if self._stop.is_set():
                # STOP can arrive while route preflight is still running. A
                # successful preflight saves a pending move before returning;
                # remove that unstarted move so a later session resume cannot
                # turn a stopped request into a physical carry.
                try:
                    self.runner.cancel_unstarted_remote_move()
                except OSError as exc:
                    with self._guard:
                        self._error = str(exc)
                        self._status = "Could not save the stopped move; inspect the checkpoint."
                        self._activity = "error"
            with self._guard:
                if self._remote_ticket is not None and not self._remote_ticket.ready.is_set():
                    self._remote_ticket.error = ApiError("worker ended before move preparation")
                    self._remote_ticket.ready.set()
                    self._remote_ticket = None
                self._waiting = False
                if completed and task == self._game_loop and self._reset_requested.is_set():
                    self._reset_requested.clear()
                    self._mode = None
                    self._activity = "starting"
                    self._status = "Starting board reset..."
                    next_worker = threading.Thread(
                        target=self._work, args=(self._reset_loop,), daemon=True,
                    )
                    self._worker = next_worker
                    next_worker.start()
                else:
                    self._reset_requested.clear()
                    self._worker = None

    def _approve_route(self, *_args) -> bool:
        return not self._stop.is_set()

    def _motion_link(self, link):
        """Do not send motion if STOP landed after route approval."""
        owner = self

        class CancellableLink:
            def set_map(self, occupancy_hex):
                return link.set_map(occupancy_hex)

            def carry(self, source_id, destination_id, point_chars):
                if owner._stop.is_set():
                    raise ReconciliationRequired(
                        "STOP arrived after the carry was checkpointed; inspect its source"
                    )
                return link.carry(source_id, destination_id, point_chars)

            def carry_offset(self, source_id, corner, destination_id, point_chars):
                if owner._stop.is_set():
                    raise ReconciliationRequired(
                        "STOP arrived after the offset carry was checkpointed; inspect the rook"
                    )
                return link.carry_offset(source_id, corner, destination_id, point_chars)

            def offset(self, square_id, corner, direction):
                if owner._stop.is_set():
                    raise ReconciliationRequired(
                        "STOP arrived after the piece nudge was checkpointed; inspect its square"
                    )
                return link.offset(square_id, corner, direction)

        return CancellableLink()

    @staticmethod
    def _confirm_action(_action) -> bool:
        # A matched DONE is controller completion; there is no physical sensor.
        return True

    def _next_move(self, engine: chess.engine.SimpleEngine | None,
                   geometry: RouteGeometry) -> str | None:
        """Resume a saved move or select and preflight the next legal move.

        Return ``None`` when play should stop at a move boundary. A reset
        request must finish a pending chess move, including all capture legs,
        before the board-reset worker takes ownership.
        """
        pending = self.runner.state.pending
        if self._reset_requested.is_set() and pending is None:
            return None
        if pending is not None:
            if pending.phase != "ready":
                raise ReconciliationRequired("inspect and reconcile the uncertain carry")
            return pending.uci

        board = self.runner.state.board()
        if board.is_game_over(claim_draw=True):
            self._set_activity("game_over", f"Game complete: {board.result(claim_draw=True)}")
            return None
        engine_turn = self._mode == "self_play" or (
            self._mode == "human_engine" and board.turn != (self._human == "white")
        )
        if engine_turn:
            assert engine is not None
            self._set_activity("engine_thinking", "Stockfish is choosing a move...")
            feasible = _feasible_engine_moves(self.runner, geometry)
            if not feasible:
                raise GameStateError("no legal engine move passes route preflight")
            result = engine.play(board, chess.engine.Limit(time=self.engine_time), root_moves=feasible)
            uci = result.move.uci()
            selected = uci
        else:
            self._set_activity("human_turn", "Enter a move; leave the physical pieces in place.")
            while not self._stop.is_set() and not self._reset_requested.is_set():
                try:
                    selected = self._moves.get(timeout=0.2)
                    break
                except Empty:
                    continue
            else:
                return None

            if isinstance(selected, RemoteMoveTicket):
                with self._guard:
                    selected.claimed = True
                if selected.cancelled:
                    return None
                uci = selected.receipt.uci
            else:
                uci = selected

        if self._stop.is_set() or self._reset_requested.is_set():
            if isinstance(selected, RemoteMoveTicket):
                selected.error = ApiError("move was stopped before preparation")
                selected.ready.set()
                with self._guard:
                    if self._remote_ticket is selected:
                        self._remote_ticket = None
            return None
        self._set_activity("moving", f"Preflighting {uci}...")
        try:
            self.runner.prepare_move(
                uci, geometry,
                remote_receipt=selected.receipt if isinstance(selected, RemoteMoveTicket) else None,
            )
        except BaseException as exc:
            if isinstance(selected, RemoteMoveTicket):
                selected.error = exc
                selected.ready.set()
                with self._guard:
                    if self._remote_ticket is selected:
                        self._remote_ticket = None
            raise
        if isinstance(selected, RemoteMoveTicket):
            selected.ready.set()
            with self._guard:
                if self._remote_ticket is selected:
                    self._remote_ticket = None
        return uci

    def _game_loop(self) -> None:
        """Execute chosen moves until stopped, finished, or ready to reset."""
        link, geometry = self._connection()
        self._set_activity("initializing", "Preparing the game...")
        engine = None
        if self._mode in {"human_engine", "self_play"}:
            path = _resolve_engine_path(self.stockfish_path)
            engine = chess.engine.SimpleEngine.popen_uci(path)
        try:
            while not self._stop.is_set():
                uci = self._next_move(engine, geometry)
                if uci is None or self._stop.is_set():
                    break
                pending = self.runner.state.pending
                status = f"Moving {uci}; waiting for controller completion..."
                if pending is not None and pending.castle_clearance_override:
                    status = f"Moving {uci} via last-resort castling route; watch nearby pieces."
                self._set_activity("moving", status)
                self.runner.execute_pending(
                    self._motion_link(link), geometry, self._confirm_action, self._approve_route,
                    confirmation_source="simulation" if self.simulate else "controller_done",
                )
                self._set_activity("moving", f"Committed {uci} at ply {len(self.runner.state.moves)}.")
            if self._stop.is_set():
                self._set_activity("paused", "Game paused; use Start to resume from the checkpoint.")
        finally:
            if engine is not None:
                engine.quit()

    def _reset_loop(self) -> None:
        """Archive the prior game, then execute or resume the saved reset plan."""
        link, geometry = self._connection()
        self._set_activity("moving", "Planning the board reset...")
        if self.runner.state.reset is None:
            actions = self.runner.preflight_reset(geometry)
            if actions or self.runner.state.moves:
                _archive_checkpoint(self.store.path)
            actions = self.runner.prepare_reset(geometry)
            if not actions:
                self._set_activity("idle", "Board already in standard setup; fresh game ready.")
                return
        self._set_activity("moving", "Returning pieces to their starting squares...")
        self.runner.execute_reset(
            self._motion_link(link), geometry, self._confirm_action, self._approve_route,
            confirmation_source="simulation" if self.simulate else "controller_done",
        )
        self._set_activity("idle", "Board reset complete; all 32 pieces are home.")

"""GUI session ownership, local controls and browser state views."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from queue import Full, Queue
import threading
import time
from typing import Any

from .api_errors import ApiError
from .gui_snapshot import SynchronizedGameStore, _san_history, _active_action
from .game import GameRunner, GameState, GameStateError
from .checkpoint import STATE_VERSION
from .link_adapters import SimulatedLink
from .routing import RouteGeometry
from .serial_link import LinkError
from .host_runtime import (
    connect_physical as _connect_physical, _geometry_from_file, _route_geometry,
)
from .session_lock import SessionLock
from .gui_remote import RemoteControlMixin, RemoteMoveTicket
from .gui_worker import GameWorkerMixin
from .reset_receipt import settle_reset_receipts


MODES = frozenset({"human_engine", "self_play", "manual_both", "external"})


class GameController(RemoteControlMixin, GameWorkerMixin):
    """One game/checkpoint owner and one possible motion worker.

    The method groups share this instance; only this constructor creates locks,
    queues, events, a runner or a store. HTTP threads read checkpoint snapshots.
    """

    def __init__(
        self, *, state_path: Path, new: bool, simulate: bool,
        geometry_file: Path | None, serial_port: str, serial_log: Path,
        stockfish_path: str, engine_time: float,
        moving_diameter: float, standing_diameter: float, margin: float,
        allow_tight_routes: bool, full_home: bool = False,
    ) -> None:
        self.lock = SessionLock(state_path, None if simulate else serial_port)
        self.lock.acquire()
        try:
            self.store = SynchronizedGameStore(state_path)
            if new:
                if state_path.exists():
                    raise GameStateError("state file already exists; use --resume or choose another path")
                state = GameState.new()
                self.store.save(state)
            else:
                state = self.store.load()
                if settle_reset_receipts(state):
                    self.store.save(state)
                # Persist migrated metadata before any remote client can read it;
                # physical inventory and plans remain unchanged.
                if state_path.exists():
                    import json
                    with state_path.open("r", encoding="utf-8") as handle:
                        stored_version = json.load(handle).get("version", 0)
                    if stored_version < STATE_VERSION:
                        self.store.save(state)
            self.runner = GameRunner(state, self.store, allow_tight_routes=allow_tight_routes)
        except BaseException:
            self.lock.release()
            raise
        self.simulate = simulate
        self.geometry_file = geometry_file
        self.serial_port = serial_port
        self.serial_log = serial_log
        self.stockfish_path = stockfish_path
        self.engine_time = engine_time
        self.moving_diameter = moving_diameter
        self.standing_diameter = standing_diameter
        self.margin = margin
        self.allow_tight_routes = allow_tight_routes
        self.full_home = full_home
        self.link = None
        self.geometry: RouteGeometry | None = None
        self._guard = threading.RLock()
        self._worker: threading.Thread | None = None
        self._interrupt_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reset_requested = threading.Event()
        self._moves: Queue[str | RemoteMoveTicket] = Queue(maxsize=1)
        self._remote_ticket: RemoteMoveTicket | None = None
        self._mode: str | None = None
        self._human = "white"
        self._activity: str | None = None
        self._waiting = False
        if state.reset is not None:
            self._status = (
                "Inspect and reconcile the saved reset action."
                if state.reset.phase != "ready" else
                "Board reset paused; continue from the saved action."
            )
        elif state.pending is not None:
            self._status = (
                "Inspect and reconcile the saved chess move."
                if state.pending.phase != "ready" else
                f"Move {state.pending.uci} paused; continue from the saved action."
            )
        elif state.board().is_game_over(claim_draw=True):
            self._status = f"Game complete: {state.board().result(claim_draw=True)}. Reset the board for a new game."
        else:
            self._status = "Ready. Start or resume a game."
        self._error: str | None = None

    def close(self) -> None:
        with self._guard:
            if self._worker is not None:
                raise RuntimeError("cannot close the GUI while a carry or game worker is active")
            interrupt = self._interrupt_thread
        if interrupt is not None:
            interrupt.join(timeout=5)
        try:
            if self.link is not None:
                self.link.close()
                self.link = None
        finally:
            self.lock.release()

    def _set_activity(self, activity: str, status: str) -> None:
        with self._guard:
            self._activity = activity
            self._status = status
            self._waiting = activity == "human_turn"

    def _load_checkpoint(self) -> GameState:
        """Tolerate a short DrvFS visibility gap around atomic replacement."""
        for attempt in range(10):
            try:
                return self.store.load()
            except FileNotFoundError:
                if attempt == 9:
                    raise
                time.sleep(0.01)
        raise AssertionError("checkpoint retry loop did not return")

    def _connection(self) -> tuple[Any, RouteGeometry]:
        """Open the controller lazily and retain its measured route geometry."""
        if self.link is None:
            if self.simulate:
                self.link = SimulatedLink()
                controller = _geometry_from_file(self.geometry_file)
            else:
                try:
                    _link, controller = _connect_physical(
                        self.serial_port, full_home=self.full_home, serial_log=self.serial_log,
                        on_open=lambda opened: setattr(self, "link", opened),
                        cancelled=self._stop.is_set,
                    )
                except BaseException:
                    self.link = None
                    self.geometry = None
                    raise
            self.geometry = _route_geometry(
                controller, self.moving_diameter, self.standing_diameter, self.margin,
            )
        assert self.geometry is not None
        return self.link, self.geometry

    def config(self) -> dict[str, Any]:
        return {
            "state_path": str(self.store.path),
            "simulate": self.simulate,
            "serial_port": None if self.simulate else self.serial_port,
            "serial_log": None if self.simulate else str(self.serial_log),
            "geometry_file": str(self.geometry_file) if self.geometry_file else None,
            "stockfish_path": self.stockfish_path,
            "engine_time": self.engine_time,
            "allow_tight_routes": self.allow_tight_routes,
            "moving_diameter_mm": self.moving_diameter,
            "standing_diameter_mm": self.standing_diameter,
            "clearance_margin_mm": self.margin,
        }

    def state(self) -> dict[str, Any]:
        """Build the browser view from an atomic checkpoint and worker metadata."""
        # JsonGameStore.load reads the atomically replaced file. Never inspect
        # the runner's mutable state from an HTTP thread during a CARRY.
        state, cached = self.store.read_snapshot()
        board = state.board()
        action = _active_action(state)
        with self._guard:
            busy = self._worker is not None
            mode = self._mode
            waiting = self._waiting
            activity = self._activity
            status = self._status
            error = self._error
            human = self._human
            reset_requested = self._reset_requested.is_set()
        # Derive controls from both the saved transfer and live worker. An
        # in-flight checkpoint is normal while its worker still owns the carry;
        # once that worker ends, the same checkpoint requires inspection.
        uncertain = not busy and action is not None and action["phase"] != "ready"
        game_over = board.is_game_over(claim_draw=True)
        # Reconciliation outranks every other phase; a graceful reset request
        # outranks the worker's current activity so the operator sees the plan.
        if uncertain:
            phase = "reconciliation_required"
        elif busy and reset_requested:
            phase = "ending_game"
        elif busy and (state.pending is not None or state.reset is not None or activity == "moving"):
            phase = "moving"
        elif busy and activity == "engine_thinking":
            phase = "engine_thinking"
        elif busy and activity == "initializing":
            phase = "engine_thinking" if mode in {"human_engine", "self_play"} else "moving"
        elif busy and waiting:
            phase = "human_turn"
        elif game_over:
            phase = "game_over"
        elif not busy and error:
            phase = "error"
        else:
            phase = "idle"
        can_move = (not cached and phase == "human_turn" and state.pending is None and
                    state.reset is None and state.source_kind is None)
        can_start = (not cached and not busy and not uncertain and state.reset is None and
                     not game_over and state.source_kind is None)
        if cached:
            status = "Checkpoint update in progress; waiting for the saved file."
        return {
            "board_fen": state.board_fen,
            "physical_squares": state.physical_squares,
            "physical_parking": state.physical_parking,
            "moves": state.moves,
            "moves_san": _san_history(state),
            "turn": "white" if board.turn else "black",
            "result": board.result(claim_draw=True) if game_over else None,
            "legal_moves": [move.uci() for move in board.legal_moves] if not game_over and state.pending is None and state.reset is None else [],
            "pending": action if state.pending is not None else None,
            "reset": action if state.reset is not None else None,
            "mode": mode,
            "human": human,
            "phase": phase,
            "status": status,
            "error": error,
            "busy": busy,
            "can_move": can_move,
            "can_start": can_start,
            "can_reset": not cached and not busy and state.pending is None and not uncertain,
            "can_end_and_reset": not cached and not uncertain and not reset_requested and (
                (busy and mode is not None) or
                (not busy and state.reset is None and
                 (state.pending is None or state.pending.phase == "ready"))
            ),
            "reset_requested": reset_requested,
            "can_stop": busy,
            "simulated": self.simulate,
            "ply": len(state.moves),
            "checkpoint_cached": cached,
        }

    def start(self, mode: str, human: str = "white") -> None:
        if not isinstance(mode, str) or mode not in MODES:
            raise ApiError("mode must be human_engine, self_play, or manual_both", HTTPStatus.BAD_REQUEST)
        if mode == "external":
            raise ApiError("external games start through the remote session API", HTTPStatus.BAD_REQUEST)
        if not isinstance(human, str) or human not in {"white", "black"}:
            raise ApiError("human must be white or black", HTTPStatus.BAD_REQUEST)
        state = self._load_checkpoint()
        if state.source_kind is not None:
            raise ApiError("remote-bound games resume through the remote session API")
        if state.reset is not None:
            raise ApiError("finish or reconcile the board reset first")
        if state.pending is not None and state.pending.phase != "ready":
            raise ApiError("inspect and reconcile the uncertain move before resuming")
        if state.board().is_game_over(claim_draw=True):
            raise ApiError("game is complete; reset the board before starting a new one")
        self._start_worker(self._game_loop, mode=mode, human=human)

    def submit_move(self, uci: str) -> None:
        if not isinstance(uci, str):
            raise ApiError("uci must be a string", HTTPStatus.BAD_REQUEST)
        uci = uci.strip().lower()
        with self._guard:
            state = self._load_checkpoint()
            if state.source_kind is not None:
                raise ApiError("remote-bound games accept moves through the remote session API")
            if self._worker is None or not self._waiting or self._mode in {"self_play", "external"}:
                raise ApiError("the game is not waiting for a human move")
            try:
                state.board().parse_uci(uci)
            except ValueError as exc:
                raise ApiError(f"illegal move: {uci}", HTTPStatus.UNPROCESSABLE_ENTITY) from exc
            try:
                self._moves.put_nowait(uci)
            except Full as exc:
                raise ApiError("a move is already queued") from exc
            self._waiting = False
            self._activity = "moving"
            self._status = f"Planning {uci}..."

    def stop(self) -> None:
        """Request a safe pause, issuing serial STOP when motion may be active."""
        with self._guard:
            if self._worker is None:
                return
            already_stopping = self._stop.is_set()
            self._stop.set()
            self._reset_requested.clear()
            if self._remote_ticket is not None and not self._remote_ticket.claimed:
                self._remote_ticket.cancelled = True
                self._remote_ticket.error = ApiError("move was stopped before preparation")
                self._remote_ticket.ready.set()
                self._remote_ticket = None
            self._status = "Pausing after the current transfer..."
            self._waiting = False
            if not already_stopping and not self.simulate and self.link is not None:
                # STOP is the serial protocol's permitted concurrent command.
                # It also preempts a HOME/REF during initial connection.
                # STOP must remain available even if DrvFS briefly hides the
                # just-replaced checkpoint path. The cached copy is sufficient
                # to decide whether a CARRY was last saved as in_flight.
                try:
                    snapshot, _cached = self.store.read_snapshot()
                    active = snapshot.pending or snapshot.reset
                    in_flight = active is not None and active.phase == "in_flight"
                except OSError:
                    # A missing checkpoint must never prevent the operator
                    # from asking the controller to halt apparent motion.
                    in_flight = self._activity == "moving"
                if in_flight or self._activity == "starting":
                    self._status = "Stopping motion..."
                    interrupt = threading.Thread(
                        target=self._stop_motion, args=(self.link,), daemon=True,
                    )
                    self._interrupt_thread = interrupt
                    interrupt.start()

    def _stop_motion(self, link) -> None:
        try:
            link.stop()
        except LinkError as exc:
            with self._guard:
                self._error = f"STOP result uncertain: {exc}"
                self._status = self._error

    def reconcile(self, outcome: str, confirmed: bool) -> None:
        """Record the inspected piece location after an uncertain action."""
        if not isinstance(outcome, str) or outcome not in {"source", "destination", "abort"}:
            raise ApiError("outcome must be source, destination, or abort", HTTPStatus.BAD_REQUEST)
        if confirmed is not True:
            raise ApiError("confirm inspection and the selected physical position first", HTTPStatus.BAD_REQUEST)
        with self._guard:
            if self._worker is not None:
                raise ApiError("pause and wait for the current action before reconciling")
            current = self._load_checkpoint()
            if current.reset is not None:
                if outcome == "abort":
                    self.runner.abort_reset_after_restore()
                elif current.reset.phase != "ready":
                    self.runner.resolve_uncertain_reset(outcome)
                else:
                    raise ApiError("no uncertain reset action to resolve")
            elif current.pending is not None:
                if outcome == "abort":
                    self.runner.abort_pending_after_restore()
                elif current.pending.phase != "ready":
                    self.runner.resolve_uncertain_action(outcome)
                else:
                    raise ApiError("no uncertain move action to resolve")
            else:
                raise ApiError("there is no pending move or reset to reconcile")
            if self.simulate and self.link is not None:
                # Reconciliation changes the expected occupancy outside of a
                # simulated CARRY. The next MAP establishes the new baseline.
                self.link.bitmap = None
            self._error = None
            self._status = "Checkpoint reconciled. Resume the game or board reset."

    def reset_board(self) -> None:
        state = self._load_checkpoint()
        if state.pending is not None:
            raise ApiError("finish or reconcile the pending chess move before resetting")
        if state.reset is not None and state.reset.phase != "ready":
            raise ApiError("inspect and reconcile the uncertain reset action first")
        self._start_worker(self._reset_loop)

    def end_game_and_reset(self) -> None:
        """Finish the current chess move, then return every piece to setup.

        This is a graceful game-ending request, not an emergency motor STOP.
        A capture can have multiple transfers; resetting from its midpoint
        would leave the checkpoint between legal chess positions.
        """
        with self._guard:
            if self._worker is not None:
                if self._mode is None:
                    raise ApiError("board reset is already running")
                if self._stop.is_set():
                    raise ApiError("wait for the paused move to settle before resetting")
                if self._reset_requested.is_set():
                    return
                self._reset_requested.set()
                self._waiting = False
                self._status = "Ending after the current chess move, then resetting the board..."
                return
        state = self._load_checkpoint()
        if state.pending is not None:
            if state.pending.phase != "ready":
                raise ApiError("inspect and reconcile the uncertain move before resetting")
            self._start_worker(self._game_loop, mode="manual_both", reset_after=True)
        else:
            self.reset_board()

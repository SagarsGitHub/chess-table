"""Local browser control for a checkpointed chess-table game.

Run ``python -m chess_table_host.gui --help``. The HTTP listener binds only to
loopback. A single background worker owns the game runner, Stockfish, and the
serial link; HTTP reads use the last atomic checkpoint so they remain responsive
while a physical CARRY is waiting for controller completion.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import time
import traceback
from typing import Any
from urllib.parse import urlsplit

import chess
import chess.engine

from .game import GameRunner, GameState, GameStateError, JsonGameStore, MoveDeferred, ReconciliationRequired
from .planner import PlanError
from .routing import RouteError, RouteGeometry
from .serial_link import LinkError
from .play import (
    DEFAULT_CONFIG, SimulatedLink, _archive_checkpoint, _connect_physical,
    _feasible_engine_moves, _geometry_from_file, _load_config,
    _resolve_engine_path, _route_geometry,
)
from .session_lock import SessionLock


STATIC_DIR = Path(__file__).with_name("gui_static")
MODES = frozenset({"human_engine", "self_play", "manual_both"})


class ApiError(ValueError):
    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.CONFLICT):
        super().__init__(message)
        self.status = status


class SynchronizedGameStore(JsonGameStore):
    """Serialize GUI reads with checkpoint writes and keep a brief safe view.

    DrvFS may report the target path absent for a short time after os.replace.
    A cached *copy of the last successfully saved* state keeps read-only GUI
    polling available while the path reappears. The controller disables action
    buttons whenever it serves this cached view.
    """

    def __init__(self, path: str | Path):
        super().__init__(path)
        self._io_lock = threading.RLock()
        self._last_state: GameState | None = None
        self._last_save_at = 0.0

    def save(self, state: GameState) -> None:
        with self._io_lock:
            super().save(state)
            self._last_state = deepcopy(state)
            self._last_save_at = time.monotonic()

    def load(self) -> GameState:
        with self._io_lock:
            state = super().load()
            self._last_state = deepcopy(state)
            return state

    def read_snapshot(self) -> tuple[GameState, bool]:
        with self._io_lock:
            try:
                state = super().load()
            except FileNotFoundError:
                if (self._last_state is None or
                        time.monotonic() - self._last_save_at > 2.0):
                    raise
                return deepcopy(self._last_state), True
            self._last_state = deepcopy(state)
            return state, False


def _san_history(state: GameState) -> list[str]:
    board = chess.Board(state.initial_fen)
    history = []
    for uci in state.moves:
        move = board.parse_uci(uci)
        history.append(board.san(move))
        board.push(move)
    return history


def _active_action(state: GameState) -> dict[str, Any] | None:
    active = state.reset or state.pending
    if active is None:
        return None
    value = active.to_json()
    index = active.next_action
    return {
        "kind": "reset" if state.reset else "move",
        "uci": state.pending.uci if state.pending else None,
        "phase": active.phase,
        "next_action": index,
        "action_number": index + 1,
        "total_actions": len(active.actions),
        "action": value["actions"][index],
        "active_route": list(active.active_route) if active.active_route else None,
    }


class GameController:
    """One game/checkpoint owner and one possible motion worker."""

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
        self._guard = threading.Lock()
        self._worker: threading.Thread | None = None
        self._interrupt_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._moves: Queue[str] = Queue(maxsize=1)
        self._mode: str | None = None
        self._human = "white"
        self._activity: str | None = None
        self._waiting = False
        if state.reset is not None:
            self._status = (
                "Inspect and reconcile the saved reset transfer."
                if state.reset.phase != "ready" else
                "Board reset paused; continue from the saved transfer."
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
        # in_flight and awaiting_confirmation are normal checkpoints while the
        # worker still owns a carry. Only an ended worker leaves uncertainty.
        uncertain = not busy and action is not None and action["phase"] != "ready"
        game_over = board.is_game_over(claim_draw=True)
        if uncertain:
            phase = "reconciliation_required"
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
        can_move = not cached and phase == "human_turn" and state.pending is None and state.reset is None
        can_start = not cached and not busy and not uncertain and state.reset is None and not game_over
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
            "can_stop": busy,
            "simulated": self.simulate,
            "ply": len(state.moves),
            "checkpoint_cached": cached,
        }

    def _start_worker(self, task, *, mode: str | None = None, human: str = "white") -> None:
        with self._guard:
            if self._worker is not None:
                raise ApiError("a game or board reset is already running")
            self._stop.clear()
            self._moves = Queue(maxsize=1)
            self._mode = mode
            self._human = human
            self._activity = "starting"
            self._waiting = False
            self._status = "Connecting to the table..." if not self.simulate else "Starting simulation..."
            self._error = None
            worker = threading.Thread(target=self._work, args=(task,), daemon=True)
            self._worker = worker
            worker.start()

    def start(self, mode: str, human: str = "white") -> None:
        if not isinstance(mode, str) or mode not in MODES:
            raise ApiError("mode must be human_engine, self_play, or manual_both", HTTPStatus.BAD_REQUEST)
        if not isinstance(human, str) or human not in {"white", "black"}:
            raise ApiError("human must be white or black", HTTPStatus.BAD_REQUEST)
        state = self._load_checkpoint()
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
            if self._worker is None or not self._waiting or self._mode == "self_play":
                raise ApiError("the game is not waiting for a human move")
            try:
                self._load_checkpoint().board().parse_uci(uci)
            except ValueError as exc:
                raise ApiError(f"illegal move: {uci}", HTTPStatus.UNPROCESSABLE_ENTITY) from exc
            try:
                self._moves.put_nowait(uci)
            except Exception as exc:
                raise ApiError("a move is already queued") from exc
            self._waiting = False
            self._activity = "moving"
            self._status = f"Planning {uci}..."

    def stop(self) -> None:
        with self._guard:
            if self._worker is None:
                return
            already_stopping = self._stop.is_set()
            self._stop.set()
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
        if not isinstance(outcome, str) or outcome not in {"source", "destination", "abort"}:
            raise ApiError("outcome must be source, destination, or abort", HTTPStatus.BAD_REQUEST)
        if confirmed is not True:
            raise ApiError("confirm inspection and the selected physical position first", HTTPStatus.BAD_REQUEST)
        with self._guard:
            if self._worker is not None:
                raise ApiError("pause and wait for the current transfer before reconciling")
            current = self._load_checkpoint()
            if current.reset is not None:
                if outcome == "abort":
                    self.runner.abort_reset_after_restore()
                elif current.reset.phase != "ready":
                    self.runner.resolve_uncertain_reset(outcome)
                else:
                    raise ApiError("no uncertain reset transfer to resolve")
            elif current.pending is not None:
                if outcome == "abort":
                    self.runner.abort_pending_after_restore()
                elif current.pending.phase != "ready":
                    self.runner.resolve_uncertain_action(outcome)
                else:
                    raise ApiError("no uncertain move transfer to resolve")
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
            raise ApiError("inspect and reconcile the uncertain reset transfer first")
        self._start_worker(self._reset_loop)

    def _work(self, task) -> None:
        try:
            task()
        except MoveDeferred:
            self._set_activity("paused", "Paused at a completed transfer boundary; checkpoint saved.")
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
            with self._guard:
                self._worker = None
                self._waiting = False

    def _approve_route(self, *_args) -> bool:
        return not self._stop.is_set()

    def _motion_link(self, link):
        """Do not send CARRY if STOP landed after route approval."""
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

        return CancellableLink()

    @staticmethod
    def _confirm_action(_action) -> bool:
        # A matched DONE is controller completion; there is no physical sensor.
        return True

    def _game_loop(self) -> None:
        link, geometry = self._connection()
        self._set_activity("initializing", "Preparing the game...")
        engine = None
        if self._mode in {"human_engine", "self_play"}:
            path = _resolve_engine_path(self.stockfish_path)
            engine = chess.engine.SimpleEngine.popen_uci(path)
        try:
            while not self._stop.is_set():
                pending = self.runner.state.pending
                if pending is not None:
                    if pending.phase != "ready":
                        raise ReconciliationRequired("inspect and reconcile the uncertain carry")
                    uci = pending.uci
                else:
                    board = self.runner.state.board()
                    if board.is_game_over(claim_draw=True):
                        self._set_activity("game_over", f"Game complete: {board.result(claim_draw=True)}")
                        break
                    engine_turn = self._mode == "self_play" or (
                        self._mode == "human_engine" and
                        board.turn != (self._human == "white")
                    )
                    if engine_turn:
                        assert engine is not None
                        self._set_activity("engine_thinking", "Stockfish is choosing a move...")
                        feasible = _feasible_engine_moves(self.runner, geometry)
                        if not feasible:
                            raise GameStateError("no legal engine move passes route preflight")
                        result = engine.play(
                            board, chess.engine.Limit(time=self.engine_time), root_moves=feasible,
                        )
                        uci = result.move.uci()
                    else:
                        self._set_activity("human_turn", "Enter a move; leave the physical pieces in place.")
                        while not self._stop.is_set():
                            try:
                                uci = self._moves.get(timeout=0.2)
                                break
                            except Empty:
                                continue
                        else:
                            break
                    if self._stop.is_set():
                        break
                    self._set_activity("moving", f"Preflighting {uci}...")
                    self.runner.prepare_move(uci, geometry)
                if self._stop.is_set():
                    break
                self._set_activity("moving", f"Moving {uci}; waiting for controller completion...")
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


class GuiHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: GameController):
        self.controller = controller
        super().__init__(address, GuiHandler)


class GuiHandler(BaseHTTPRequestHandler):
    server: GuiHTTPServer

    def _local_host(self) -> bool:
        return self.headers.get("Host") in {
            f"localhost:{self.server.server_port}",
            f"127.0.0.1:{self.server.server_port}",
        }

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._local_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                self._send_json(self.server.controller.state())
            except (OSError, GameStateError) as exc:
                self._send_json({"error": f"checkpoint temporarily unavailable: {exc}"},
                                HTTPStatus.SERVICE_UNAVAILABLE)
        elif path == "/api/config":
            self._send_json(self.server.controller.config())
        elif path in {"/", "/index.html"}:
            self._static("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
        elif path == "/style.css":
            self._static("style.css", "text/css; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._local_host():
            self._send_json({"error": "local Host required"}, HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        if path not in {"/api/start", "/api/move", "/api/stop", "/api/reconcile", "/api/reset-board"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # JSON and same-origin requests prevent an unrelated website from
        # making blind form posts to this loopback-only motion controller.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._send_json({"error": "cross-origin control is forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                raise ApiError("request body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ApiError("JSON body must be an object", HTTPStatus.BAD_REQUEST)
            controller = self.server.controller
            if path == "/api/start":
                controller.start(body.get("mode"), body.get("human", "white"))
            elif path == "/api/move":
                controller.submit_move(body.get("uci"))
            elif path == "/api/stop":
                controller.stop()
            elif path == "/api/reconcile":
                controller.reconcile(body.get("outcome"), body.get("confirmed"))
            elif path == "/api/reset-board":
                controller.reset_board()
            self._send_json(controller.state(), HTTPStatus.ACCEPTED)
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except OSError as exc:
            self._send_json({"error": f"checkpoint temporarily unavailable: {exc}"},
                            HTTPStatus.SERVICE_UNAVAILABLE)
        except (GameStateError, RouteError, PlanError, LinkError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, required=True, help="checkpoint JSON file")
    session = parser.add_mutually_exclusive_group(required=True)
    session.add_argument("--new", action="store_true", help="create a new standard-position checkpoint")
    session.add_argument("--resume", action="store_true", help="load an existing checkpoint")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--simulate", action="store_true")
    connection.add_argument("--port", help="Uno serial port (override config; e.g. /dev/ttyACM0 in WSL)")
    parser.add_argument("--geometry-file", type=Path, help="required for simulation")
    parser.add_argument("--engine", help="Stockfish executable or directory")
    parser.add_argument("--engine-time", type=float, default=0.5)
    parser.add_argument("--moving-diameter", type=float)
    parser.add_argument("--standing-diameter", type=float)
    parser.add_argument("--margin", type=float)
    clearance = parser.add_mutually_exclusive_group()
    clearance.add_argument("--allow-tight-routes", dest="allow_tight_routes", action="store_true")
    clearance.add_argument("--strict-clearance", dest="allow_tight_routes", action="store_false")
    parser.set_defaults(allow_tight_routes=None)
    parser.add_argument("--home", action="store_true", help="full HOME instead of saved-geometry REF")
    parser.add_argument("--serial-log", type=Path)
    parser.add_argument("--http-port", type=int, default=8765, help="localhost browser port")
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
    except GameStateError as exc:
        parser.error(str(exc))
    if args.simulate and args.geometry_file is None:
        parser.error("--simulate requires --geometry-file")
    if not args.simulate and args.geometry_file is not None:
        parser.error("physical play reads live GEOM; omit --geometry-file")
    if not 0 < args.http_port < 65536:
        parser.error("--http-port must be in 1..65535")
    if args.engine_time <= 0:
        parser.error("--engine-time must be positive")
    moving = args.moving_diameter if args.moving_diameter is not None else config["moving_diameter_mm"]
    standing = args.standing_diameter if args.standing_diameter is not None else config["standing_diameter_mm"]
    margin = args.margin if args.margin is not None else config["clearance_margin_mm"]
    if moving <= 0 or standing <= 0 or margin < 0:
        parser.error("diameters must be positive and margin cannot be negative")
    allow_tight = args.allow_tight_routes if args.allow_tight_routes is not None else config["allow_tight_routes"]
    serial_port = args.port or config["serial_port"]
    serial_log = args.serial_log or args.state.with_name(args.state.stem + ".serial.jsonl")
    try:
        controller = GameController(
            state_path=args.state, new=args.new, simulate=args.simulate,
            geometry_file=args.geometry_file, serial_port=serial_port, serial_log=serial_log,
            stockfish_path=args.engine or config["stockfish_path"], engine_time=args.engine_time,
            moving_diameter=moving, standing_diameter=standing, margin=margin,
            allow_tight_routes=allow_tight, full_home=args.home,
        )
    except (GameStateError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    try:
        with GuiHTTPServer(("127.0.0.1", args.http_port), controller) as server:
            print(f"Chess table GUI: http://localhost:{server.server_port}/")
            print("Simulation only; no hardware will move." if args.simulate else
                  f"Physical Uno port: {serial_port}; keep the power cutoff reachable.")
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        controller.stop()
        worker = controller._worker
        if worker is not None:
            print("Waiting for the current carry to finish before closing...")
            worker.join()
        return 0
    finally:
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

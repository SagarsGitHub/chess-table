"""Local game runner for the chess table.

Run ``python -m chess_table_host.play --help`` for physical, simulated, and
serial-port discovery options. Simulation checks host orchestration only; it
does not certify the motor, magnet, or piece placement.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import threading

import chess
import chess.engine

from .game import (
    GameRunner, GameState, GameStateError, JsonGameStore, MoveDeferred,
    ReconciliationRequired,
)
from .planner import Action, BoardSquare, ManualReplacement, ParkingSlot, PlanError, Transfer
from .routing import RouteError, RouteGeometry
from .serial_link import ControllerGeometry, LinkError, open_serial
from .serial_link import Completion


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def _load_config(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GameStateError(f"cannot read game config {path}: {exc}") from exc
    required = {
        "serial_port", "stockfish_path", "moving_diameter_mm",
        "standing_diameter_mm", "clearance_margin_mm",
    }
    if not isinstance(config, dict) or not required.issubset(config):
        raise GameStateError(f"game config must contain {', '.join(sorted(required))}")
    if any(not isinstance(config[key], str) or not config[key].strip()
           for key in ("serial_port", "stockfish_path")):
        raise GameStateError("serial_port and stockfish_path must be nonempty strings")
    for key in ("moving_diameter_mm", "standing_diameter_mm", "clearance_margin_mm"):
        if not isinstance(config[key], (int, float)) or isinstance(config[key], bool):
            raise GameStateError(f"{key} must be a number")
    if (config["moving_diameter_mm"] <= 0 or config["standing_diameter_mm"] <= 0 or
            config["clearance_margin_mm"] < 0):
        raise GameStateError("diameters must be positive and margin cannot be negative")
    if type(config.get("allow_tight_routes", False)) is not bool:
        raise GameStateError("allow_tight_routes must be a boolean")
    config["allow_tight_routes"] = config.get("allow_tight_routes", False)
    return config


class SimulatedLink:
    """Completes host commands without controlling physical hardware."""

    def __init__(self) -> None:
        self.command_id = 0
        self.bitmap: int | None = None

    def set_map(self, occupancy_hex: str) -> None:
        bitmap = int.from_bytes(bytes.fromhex(occupancy_hex), "little")
        if self.bitmap is not None and bitmap != self.bitmap:
            raise GameStateError("simulated controller map differs from confirmed host state")
        self.bitmap = bitmap
        print(f"SIM MAP {occupancy_hex}")

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        source, destination = int(source_id), int(destination_id)
        if (self.bitmap is None or not 0 <= source < 96 or not 0 <= destination < 96 or
                not self.bitmap & (1 << source) or self.bitmap & (1 << destination)):
            raise GameStateError("simulated controller rejected inconsistent CARRY occupancy")
        self.bitmap = (self.bitmap & ~(1 << source)) | (1 << destination)
        self.command_id += 1
        print(f"SIM CARRY {source_id} {destination_id} {point_chars}")
        return Completion(self.command_id, "CARRY", ())

    def close(self) -> None:
        pass


class LoggedSerialStream:
    """Record complete TX/RX lines from one physical serial session."""

    def __init__(self, stream, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.log = path.open("a", encoding="utf-8", newline="\n")
        self.lock = threading.Lock()
        self.receive = bytearray()

    def _record(self, direction: str, raw: bytes) -> None:
        item = {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "line": raw.rstrip(b"\r\n").decode("ascii", errors="replace"),
        }
        with self.lock:
            self.log.write(json.dumps(item, sort_keys=True) + "\n")
            self.log.flush()
            os.fsync(self.log.fileno())

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        for byte in data:
            self.receive.append(byte)
            if byte == 10:
                self._record("rx", bytes(self.receive))
                self.receive.clear()
        return data

    def write(self, data: bytes) -> int | None:
        written = self.stream.write(data)
        if written is None or written == len(data):
            self._record("tx", data)
        return written

    def close(self) -> None:
        try:
            if self.receive:
                self._record("rx_partial", bytes(self.receive))
            self.log.close()
        finally:
            self.stream.close()


def _yes(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _describe_action(action: Action) -> str:
    def label(location: BoardSquare | ParkingSlot) -> str:
        return location.name if isinstance(location, BoardSquare) else f"P{location.slot_id:02d}"
    if isinstance(action, Transfer):
        return f"{action.piece} {label(action.source)} -> {label(action.destination)} ({action.reason})"
    return f"replace {action.pawn} with {action.replacement} at {action.square.name}"


def _show_physical_state(state: GameState) -> None:
    board = chess.Board(None)
    for square, symbol in state.physical_squares.items():
        board.set_piece_at(chess.parse_square(square), chess.Piece.from_symbol(symbol))
    print("Expected physical board (white at bottom):")
    print(board)
    parked = [f"P{i:02d}={piece}" for i, piece in enumerate(state.physical_parking) if piece]
    print("Parking:", ", ".join(parked) if parked else "empty")
    print("Committed FEN:", state.board_fen)
    logical = state.board()
    stand_ins = [
        f"{name}: physical {physical} represents logical {logical.piece_at(chess.parse_square(name)).symbol()}"
        for name, physical in sorted(state.physical_squares.items())
        if logical.piece_at(chess.parse_square(name)) is not None
        and logical.piece_at(chess.parse_square(name)).symbol() != physical
    ]
    if stand_ins:
        print("Promoted pawn stand-ins:", "; ".join(stand_ins))
    if state.pending:
        pending = state.pending
        print(f"Pending move: {pending.uci}, action {pending.next_action + 1}/"
              f"{len(pending.actions)}, phase={pending.phase}")
        print("Pending action:", _describe_action(pending.actions[pending.next_action]))
        if pending.active_route:
            print("Last CARRY arguments:", " ".join(pending.active_route))


def _reconcile_at_start(runner: GameRunner, *, automatic: bool = False) -> None:
    pending = runner.state.pending
    uncertain_at_start = pending is not None and pending.phase != "ready"
    if pending:
        _show_physical_state(runner.state)
    if pending and pending.phase == "ready":
        if automatic:
            print("Continuing the checkpointed move from its last completed action.")
        else:
            print("This move has confirmed actions remaining. You may continue from the checkpoint")
            print("or manually restore the entire committed pre-move position and abort it.")
            answer = input("Press Enter to continue, or type ABORT after restoring: ").strip().lower()
            if answer == "abort":
                if not _yes("Did you restore every board square and parking slot to the pre-move position?"):
                    raise ReconciliationRequired("pre-move position was not confirmed")
                runner.abort_pending_after_restore()
            elif answer:
                raise ReconciliationRequired("no valid recovery choice was selected")
    elif pending:
        print("Inspect the last action and every affected square and parking slot.")
        print("SOURCE: restore its piece to the action source. DESTINATION: confirm it arrived.")
        print("ABORT: manually restore the entire committed pre-move position.")
        answer = input("Type SOURCE, DESTINATION, or ABORT after physical inspection: ").strip().lower()
        if answer not in {"source", "destination", "abort"}:
            raise ReconciliationRequired("no recovery outcome was selected")
        if not _yes("Did you inspect and restore/verify the selected physical state?"):
            raise ReconciliationRequired("physical state was not confirmed")
        if answer == "abort":
            runner.abort_pending_after_restore()
        else:
            runner.resolve_uncertain_action(answer)
    _show_physical_state(runner.state)
    if (not automatic or uncertain_at_start) and not _yes(
        "Does the entire physical board and parking inventory match this checkpoint?"
    ):
        raise ReconciliationRequired("physical board does not match the saved checkpoint")


def _geometry_from_file(path: Path) -> ControllerGeometry:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return ControllerGeometry(
        x_travel_mm=float(value["x_travel_mm"]),
        y_travel_mm=float(value["y_travel_mm"]),
        board_origin_x_mm=float(value["board_origin_x_mm"]),
        board_origin_y_mm=float(value["board_origin_y_mm"]),
        square_pitch_mm=float(value["square_pitch_mm"]),
        end_inset_mm=float(value["end_inset_mm"]),
        pulses_per_mm=float(value["pulses_per_mm"]),
    )


def _route_geometry(
    controller: ControllerGeometry, moving: float, standing: float, margin: float
) -> RouteGeometry:
    return RouteGeometry(
        controller.square_pitch_mm, moving, standing, margin,
        x_travel_mm=controller.x_travel_mm,
        y_travel_mm=controller.y_travel_mm,
        end_inset_mm=controller.end_inset_mm,
        pulses_per_mm=controller.pulses_per_mm,
        board_origin_x_mm=controller.board_origin_x_mm,
        board_origin_y_mm=controller.board_origin_y_mm,
    )


def _connect_physical(port: str, *, full_home: bool, serial_log: Path):
    link = open_serial(port)
    try:
        link.stream = LoggedSerialStream(link.stream, serial_log)
        print("Serial transcript:", serial_log)
        hello = link.hello()
        required = {"STATUS", "HOME", "REF", "MAP", "CARRY", "GEOM"}
        if not required.issubset(hello.capabilities):
            raise GameStateError(f"controller lacks {sorted(required - hello.capabilities)}")
        status = link.status()
        print(f"Controller: V={hello.version}, REF={int(status.referenced)}, "
              f"SAVED={int(status.saved_geometry)}")
        if full_home or not status.saved_geometry:
            print("Running full HOME; watch both axes and keep the power cutoff reachable.")
            link.home()
        else:
            print("Running saved-geometry REF; watch both axes toward MIN.")
            link.reference()
        controller = link.geometry()
        print("Live GEOM:", controller)
        return link, controller
    except BaseException:
        link.close()
        raise


def _feasible_engine_moves(runner: GameRunner, geometry: RouteGeometry) -> list[chess.Move]:
    board = runner.state.board()
    feasible: list[chess.Move] = []
    for move in board.legal_moves:
        try:
            runner.preflight_move(move.uci(), geometry)
        except (GameStateError, PlanError, RouteError):
            continue
        feasible.append(move)
    return feasible


def _resolve_engine_path(configured: str | None) -> str:
    """Find a downloaded Stockfish binary without assuming an OS-specific name."""
    default = Path.home() / "Documents" / "stockfish"
    choice = Path(configured).expanduser() if configured else default
    if configured and not choice.exists() and len(choice.parts) == 1:
        on_path = shutil.which(configured)
        if on_path:
            return on_path
    if choice.is_file():
        return str(choice)
    if choice.is_dir():
        for name in ("stockfish", "stockfish.exe", "stockfish-windows-x86-64-universal.exe"):
            candidate = choice / name
            if candidate.is_file():
                return str(candidate)
        candidates = sorted(
            path for path in choice.rglob("*")
            if path.is_file() and path.name.lower().startswith("stockfish")
            and path.suffix.lower() in {"", ".exe"}
        )
        if len(candidates) == 1:
            return str(candidates[0])
        if len(candidates) > 1:
            raise GameStateError(
                f"several Stockfish executables found in {choice}; select one with --engine"
            )
    raise GameStateError(f"Stockfish executable not found at {choice}; use --engine PATH")


def _print_plan(runner: GameRunner, uci: str, geometry: RouteGeometry) -> None:
    board = runner.state.board()
    move = board.parse_uci(uci)
    plan = runner.preflight_move(uci, geometry)
    print(f"Move {len(runner.state.moves) + 1}: {board.san(move)} ({plan.uci})")
    for index, action in enumerate(plan.actions, 1):
        description = _describe_action(action)
        if isinstance(action, Transfer) and isinstance(action.source, BoardSquare):
            logical = board.piece_at(chess.parse_square(action.source.name))
            if logical is not None and logical.symbol() != action.piece:
                description += f"; physical {action.piece} represents logical {logical.symbol()}"
        print(f"  {index}. {description}")
    if move.promotion:
        promoted = chess.Piece(move.promotion, board.turn).symbol()
        pawn = "P" if board.turn == chess.WHITE else "p"
        print(f"  Promotion: the physical {pawn} remains at {chess.square_name(move.to_square)}; "
              f"chess rules use {promoted}.")


def _validate_script(runner: GameRunner, scripted: list[str]) -> None:
    if scripted[:len(runner.state.moves)] != runner.state.moves and scripted:
        raise GameStateError("saved move history is not a prefix of the supplied script")
    if (runner.state.pending is not None and len(scripted) > len(runner.state.moves) and
            scripted[len(runner.state.moves)] != runner.state.pending.uci):
        raise GameStateError("scripted next move differs from saved pending physical move")


def _play_moves(args: argparse.Namespace, runner: GameRunner, link, geometry: RouteGeometry) -> None:
    scripted = args.moves or []
    _validate_script(runner, scripted)
    engine = None
    if args.engine or getattr(args, "self_play", False) or (not scripted and not args.manual_both):
        engine_path = _resolve_engine_path(args.engine or args.engine_config_path)
        print("Stockfish:", engine_path)
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    try:
        while True:
            if runner.state.pending:
                print("Resuming pending move", runner.state.pending.uci)
            else:
                board = runner.state.board()
                if board.is_game_over(claim_draw=True):
                    print("Game complete:", board.result(claim_draw=True))
                    print(board.outcome(claim_draw=True))
                    break
                script_index = len(runner.state.moves)
                if script_index < len(scripted):
                    uci = scripted[script_index]
                elif engine is not None and (
                    getattr(args, "self_play", False) or board.turn != (args.human == "white")
                ):
                    feasible = _feasible_engine_moves(runner, geometry)
                    if not feasible:
                        raise GameStateError("no legal engine move passes route preflight")
                    result = engine.play(
                        board, chess.engine.Limit(time=args.engine_time), root_moves=feasible,
                    )
                    uci = result.move.uci()
                    print(
                        "Stockfish", "White" if board.turn else "Black", "chose", uci,
                        "from", len(feasible), "routable legal moves",
                    )
                elif engine is None and script_index >= len(scripted) and scripted:
                    print("Script finished; checkpoint saved for a later game session.")
                    break
                else:
                    print(board)
                    print("Leave the pieces in place; the table will carry the move you enter.")
                    uci = input("Enter your intended UCI move, or quit: ").strip().lower()
                    if uci in {"quit", "exit", "q"}:
                        break
                try:
                    _print_plan(runner, uci, geometry)
                    if getattr(args, "supervised", False) and not args.simulate and not _yes(
                        "Start this physical move?"
                    ):
                        print("Move left unstarted.")
                        break
                    runner.prepare_move(uci, geometry)
                except (GameStateError, RouteError, ValueError) as exc:
                    if script_index < len(scripted) or engine is not None:
                        raise GameStateError(f"move {uci} cannot start: {exc}") from exc
                    print(f"Move cannot start: {exc}")
                    continue

            def approve_route(action, route, wire) -> bool:
                points = " -> ".join(f"({point.x},{point.y})" for point in route.points)
                print("Route for", _describe_action(action), ":", points)
                print("CARRY arguments:", " ".join(wire))
                if route.clearance_ignored:
                    print("CLEARANCE OVERRIDE: circular piece-footprint clearance was bypassed."
                          " Motion will continue on the occupied-center-safe route.")
                return args.simulate or not getattr(args, "supervised", False) or _yes(
                    "Start this transfer?"
                )

            def confirm(action: Action) -> bool:
                if args.simulate:
                    print("SIM CONFIRMED", _describe_action(action))
                    return True
                if isinstance(action, ManualReplacement):
                    raise GameStateError("manual promotion exchange is not allowed by this board design")
                if getattr(args, "supervised", False):
                    return _yes(f"Confirm physical action: {_describe_action(action)}?")
                print("Controller DONE; advancing", _describe_action(action))
                return True

            try:
                runner.execute_pending(
                    link, geometry, confirm, approve_route,
                    confirmation_source=(
                        "simulation" if args.simulate else
                        "operator" if getattr(args, "supervised", False) else
                        "controller_done"
                    ),
                )
            except MoveDeferred as exc:
                print(exc)
                print("Move checkpoint saved; resume when ready.")
                break
            print("Committed", runner.state.moves[-1], "at ply", len(runner.state.moves))
            _show_physical_state(runner.state)
    finally:
        if engine is not None:
            engine.quit()


def _list_ports(configured_port: str) -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("Install computer/requirements.txt to discover serial ports.", file=sys.stderr)
        return 2
    print("Configured serial port:", configured_port)
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports detected. Connect the Uno and run this again.")
    for port in ports:
        print(f"{port.device}\t{port.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="game settings JSON (default: computer/config.json)")
    parser.add_argument("--list-ports", action="store_true", help="show detected serial ports and exit")
    parser.add_argument("--state", type=Path, help="checkpoint JSON path")
    parser.add_argument("--serial-log", type=Path,
                        help="physical serial JSONL log (default: beside --state)")
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--new", action="store_true", help="create a new standard-position game")
    session.add_argument("--resume", action="store_true", help="load an existing checkpoint")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--port", help="override configured Uno port for this run")
    connection.add_argument("--simulate", action="store_true", help="no hardware; auto-confirm transfers")
    parser.add_argument("--geometry-file", type=Path, help="required for simulation")
    parser.add_argument("--moving-diameter", type=float, help="measured maximum moving footprint in mm")
    parser.add_argument("--standing-diameter", type=float, help="measured maximum standing footprint in mm")
    parser.add_argument("--margin", type=float, help="override configured clearance in mm")
    clearance = parser.add_mutually_exclusive_group()
    clearance.add_argument("--allow-tight-routes", dest="allow_tight_routes", action="store_true",
                           help="permit bounded routes that fail piece-footprint clearance")
    clearance.add_argument("--strict-clearance", dest="allow_tight_routes", action="store_false",
                           help="reject routes that fail piece-footprint clearance")
    parser.set_defaults(allow_tight_routes=None)
    parser.add_argument("--home", action="store_true", help="full HOME even if saved geometry exists")
    parser.add_argument("--supervised", action="store_true",
                        help="ask before moves/routes and after CARRY completion")
    parser.add_argument("--moves", nargs="+", help="scripted UCI moves, in order")
    parser.add_argument("--engine", help="override ~/Documents/stockfish with a Stockfish file or folder")
    players = parser.add_mutually_exclusive_group()
    players.add_argument("--manual-both", action="store_true", help="enter UCI moves for both sides")
    players.add_argument("--self-play", action="store_true",
                         help="have Stockfish play both colors without human UCI entry")
    parser.add_argument("--human", choices=("white", "black"), default="white")
    parser.add_argument("--engine-time", type=float, default=0.5, help="Stockfish seconds per move")
    args = parser.parse_args(argv)
    try:
        config = _load_config(args.config)
    except GameStateError as exc:
        parser.error(str(exc))
    args.engine_config_path = config["stockfish_path"]
    if not args.simulate and args.port is None:
        args.port = config["serial_port"]
    if args.moving_diameter is None:
        args.moving_diameter = config["moving_diameter_mm"]
    if args.standing_diameter is None:
        args.standing_diameter = config["standing_diameter_mm"]
    if args.margin is None:
        args.margin = config["clearance_margin_mm"]
    if args.allow_tight_routes is None:
        args.allow_tight_routes = config["allow_tight_routes"]
    if args.list_ports:
        return _list_ports(config["serial_port"])
    if (args.state is None or (not args.new and not args.resume) or
            (not args.port and not args.simulate)):
        parser.error("game requires --state and --new/--resume")
    if args.simulate and args.geometry_file is None:
        parser.error("--simulate requires --geometry-file")
    if args.port and args.geometry_file is not None:
        parser.error("physical play uses live GEOM; omit --geometry-file")
    if args.engine_time <= 0:
        parser.error("--engine-time must be positive")
    if args.engine and args.manual_both:
        parser.error("--engine and --manual-both cannot be combined")
    store = JsonGameStore(args.state)
    if args.new:
        if args.state.exists():
            parser.error("state file already exists; use --resume or choose another path")
        state = GameState.new()
        store.save(state)
    else:
        try:
            state = store.load()
        except (GameStateError, OSError, ValueError) as exc:
            parser.error(f"could not load state file {args.state}: {exc}")
    runner = GameRunner(state, store, allow_tight_routes=args.allow_tight_routes)
    link = None
    try:
        if not args.simulate:
            _reconcile_at_start(runner, automatic=not args.supervised)
            _validate_script(runner, args.moves or [])
            serial_log = args.serial_log or args.state.with_name(args.state.stem + ".serial.jsonl")
            link, controller = _connect_physical(
                args.port, full_home=args.home, serial_log=serial_log
            )
        else:
            _validate_script(runner, args.moves or [])
            link = SimulatedLink()
            controller = _geometry_from_file(args.geometry_file)
            print("SIMULATION ONLY; no board or magnet will move.")
        geometry = _route_geometry(
            controller, args.moving_diameter, args.standing_diameter, args.margin
        )
        print("Required center distance:", geometry.required_center_distance_mm, "mm")
        print("Tight-route fallback:", "enabled" if args.allow_tight_routes else "disabled")
        if not args.simulate:
            print("Physical advancement:", "supervised" if args.supervised else "automatic after DONE CARRY")
        _play_moves(args, runner, link, geometry)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Inspect the physical board and saved pending action before resuming.")
        if link is not None and not args.simulate and state.pending and state.pending.phase == "in_flight":
            try:
                link.stop()
            except LinkError as exc:
                print(f"STOP result uncertain: {exc}", file=sys.stderr)
        return 130
    except (GameStateError, RouteError, LinkError, OSError, ValueError, chess.engine.EngineError) as exc:
        print(f"Game paused: {exc}", file=sys.stderr)
        print("Check the saved state and physical board before resuming.", file=sys.stderr)
        return 1
    finally:
        if link is not None:
            link.close()


if __name__ == "__main__":
    raise SystemExit(main())

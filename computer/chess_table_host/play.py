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
from typing import Callable

import chess
import chess.engine

from .game import (
    GameRunner, GameState, GameStateError, JsonGameStore, MoveDeferred,
    ReconciliationRequired,
)
from .planner import (
    Action, BoardSquare, ManualReplacement, ParkingSlot, PieceOffset, PlanError,
    RookOffset, Transfer,
)
from .routing import RouteError, RouteGeometry
from .serial_link import ControllerGeometry, LinkError, open_serial
from .session_lock import SessionLock
from .host_config import DEFAULT_CONFIG, _load_config
from .link_adapters import LoggedSerialStream, SimulatedLink


def _yes(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


def _describe_action(action: Action) -> str:
    def label(location: BoardSquare | ParkingSlot) -> str:
        return location.name if isinstance(location, BoardSquare) else f"P{location.slot_id:02d}"
    if isinstance(action, Transfer):
        if action.reason == "castle_rook_from_offset":
            return (f"{action.piece} {label(action.source)} offset corner -> "
                    f"{label(action.destination)} ({action.reason})")
        return f"{action.piece} {label(action.source)} -> {label(action.destination)} ({action.reason})"
    if isinstance(action, (RookOffset, PieceOffset)):
        motion = "nudge toward" if action.direction == "OUT" else "recenter from"
        return f"{action.piece} {motion} {action.corner} corner within {action.square.name} ({action.reason})"
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
        if state.reset is None and logical.piece_at(chess.parse_square(name)) is not None
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
            print("Last motion arguments:", " ".join(pending.active_route))
    if state.reset:
        reset = state.reset
        print(f"Pending board reset: action {reset.next_action + 1}/"
              f"{len(reset.actions)}, phase={reset.phase}")
        print("Pending action:", _describe_action(reset.actions[reset.next_action]))
        if reset.active_route:
            print("Last motion arguments:", " ".join(reset.active_route))


def _reconcile_reset_at_start(runner: GameRunner, *, automatic: bool) -> bool:
    """Resolve an interrupted reset before its remaining actions are sent."""
    reset = runner.state.reset
    assert reset is not None
    uncertain = reset.phase != "ready"
    _show_physical_state(runner.state)
    if uncertain:
        action = reset.actions[reset.next_action]
        if isinstance(action, PieceOffset):
            source = "center" if action.direction == "OUT" else f"{action.corner} corner"
            destination = f"{action.corner} corner" if action.direction == "OUT" else "center"
            print(f"Inspect {action.piece} within {action.square.name} and nearby pieces.")
            print(f"SOURCE: verify it at the {source}. DESTINATION: verify it at the {destination}.")
        else:
            print("Inspect the last reset transfer and every affected square and parking slot.")
            print("SOURCE: restore its piece to the action source. DESTINATION: confirm it arrived.")
        print("ABORT: manually restore the entire pre-reset game position.")
        answer = input("Type SOURCE, DESTINATION, or ABORT after physical inspection: ").strip().lower()
        if answer not in {"source", "destination", "abort"}:
            raise ReconciliationRequired("no reset recovery outcome was selected")
        if not _yes("Did you inspect and restore/verify the selected physical state?"):
            raise ReconciliationRequired("physical state was not confirmed")
        if answer == "abort":
            runner.abort_reset_after_restore()
            print("Board reset aborted; the pre-reset game checkpoint is restored.")
            return False
        runner.resolve_uncertain_reset(answer)
    elif not automatic:
        print("Continuing the checkpointed board reset from its last completed action.")
    _show_physical_state(runner.state)
    if (uncertain or not automatic) and not _yes(
        "Does the entire physical board and parking inventory match this checkpoint?"
    ):
        raise ReconciliationRequired("physical board does not match the saved reset checkpoint")
    if runner.state.reset is None:
        print("Board reset completed during reconciliation; the checkpoint is a fresh game.")
        return False
    return True


def _reconcile_move_at_start(runner: GameRunner, *, automatic: bool) -> bool:
    """Resolve an interrupted move before its remaining actions are sent."""
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
        action = pending.actions[pending.next_action]
        if isinstance(action, (RookOffset, PieceOffset)):
            source = "square center" if action.direction == "OUT" else f"{action.corner} corner"
            destination = f"{action.corner} corner" if action.direction == "OUT" else "square center"
            print(f"SOURCE: verify {action.piece} at {action.square.name} {source}.")
            print(f"DESTINATION: verify {action.piece} at {action.square.name} {destination}.")
        elif isinstance(action, Transfer) and action.reason == "castle_rook_from_offset":
            corner = pending.active_route[1]
            print(f"SOURCE: restore the rook to the {corner} corner within {action.source.name}.")
            print(f"DESTINATION: verify the rook at {action.destination.name}.")
            print("Inspect the castled king and nearby pieces for contact or displacement.")
        else:
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
    return True


def _reconcile_at_start(runner: GameRunner, *, automatic: bool = False) -> bool:
    if runner.state.reset is not None:
        return _reconcile_reset_at_start(runner, automatic=automatic)
    return _reconcile_move_at_start(runner, automatic=automatic)


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


def _connect_physical(
    port: str, *, full_home: bool, serial_log: Path,
    on_open: Callable[[object], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
):
    link = open_serial(port)
    try:
        link.stream = LoggedSerialStream(link.stream, serial_log)
        if on_open is not None:
            on_open(link)
        print("Serial transcript:", serial_log)
        hello = link.hello()
        required = {
            "STATUS", "HOME", "REF", "MAP", "CARRY", "CARRY_OFFSET",
            "CARRY_DIAG", "GEOM", "OFFSET", "OVER20",
        }
        if not required.issubset(hello.capabilities):
            raise GameStateError(f"controller lacks {sorted(required - hello.capabilities)}")
        status = link.status()
        print(f"Controller: V={hello.version}, REF={int(status.referenced)}, "
              f"SAVED={int(status.saved_geometry)}")
        if cancelled is not None and cancelled():
            raise MoveDeferred("connection stopped before HOME or REF")
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
    default = Path.home() / "Documents" / "Stockfish"
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
    """Advance scripted, human, or engine moves through one checkpointed runner."""
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
                # A script takes precedence until exhausted; otherwise choose
                # the engine for its side (or both sides in self-play mode).
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
                """Show the exact controller motion before optional supervision."""
                points = " -> ".join(f"({point.x},{point.y})" for point in route.points)
                print("Route for", _describe_action(action), ":", points)
                command = (
                    "OFFSET" if isinstance(action, (RookOffset, PieceOffset)) else
                    "CARRY_OFFSET" if action.reason == "castle_rook_from_offset" else
                    "CARRY"
                )
                print(f"{command} arguments:", " ".join(wire))
                if route.clearance_ignored:
                    print("CLEARANCE OVERRIDE: circular piece-footprint clearance was bypassed."
                          " Motion will continue on the occupied-center-safe route.")
                return args.simulate or not getattr(args, "supervised", False) or _yes(
                    "Start this piece nudge?" if isinstance(action, (RookOffset, PieceOffset)) else
                    "Start this transfer?"
                )

            def confirm(action: Action) -> bool:
                """Decide when the saved physical snapshot may advance."""
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


def _archive_checkpoint(path: Path) -> Path:
    """Preserve the exact played checkpoint bytes before reset modifies it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for attempt in range(1000):
        suffix = "" if attempt == 0 else f"-{attempt:03d}"
        archive = path.with_name(f"{path.stem}.before-reset-{stamp}{suffix}{path.suffix}")
        try:
            with path.open("rb") as original, archive.open("xb") as copy:
                shutil.copyfileobj(original, copy)
                copy.flush()
                os.fsync(copy.fileno())
            return archive
        except FileExistsError:
            continue
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
    raise GameStateError("could not find a free checkpoint archive filename")


def _run_reset(args: argparse.Namespace, runner: GameRunner, link, geometry: RouteGeometry) -> None:
    if runner.state.reset is None:
        preview = runner.preflight_reset(geometry)
        if preview or runner.state.moves:
            print("Played checkpoint archived:", _archive_checkpoint(runner.store.path))
        actions = runner.prepare_reset(geometry)
        if not actions:
            print("Board is already in the standard setup; checkpoint reset for a new game.")
            return
        print(f"Board reset prepared: {len(actions)} actions; full route preflight passed.")
    else:
        print(f"Resuming board reset at action {runner.state.reset.next_action + 1}/"
              f"{len(runner.state.reset.actions)}.")

    def approve_route(action: Transfer | PieceOffset, route, wire) -> bool:
        points = " -> ".join(f"({point.x},{point.y})" for point in route.points)
        print("Reset route for", _describe_action(action), ":", points)
        print("OFFSET arguments:" if isinstance(action, PieceOffset) else "CARRY arguments:",
              " ".join(wire))
        if route.clearance_ignored:
            print("CLEARANCE OVERRIDE: using the bounded occupied-center-safe route.")
        return args.simulate or not args.supervised or _yes("Start this reset action?")

    def confirm(action: Transfer | PieceOffset) -> bool:
        if args.simulate:
            print("SIM CONFIRMED", _describe_action(action))
            return True
        if args.supervised:
            return _yes(f"Confirm reset action: {_describe_action(action)}?")
        print("Controller DONE; advancing", _describe_action(action))
        return True

    try:
        runner.execute_reset(
            link, geometry, confirm, approve_route,
            confirmation_source=(
                "simulation" if args.simulate else
                "operator" if args.supervised else "controller_done"
            ),
        )
    except MoveDeferred as exc:
        print(exc)
        print("Reset checkpoint saved; resume with --reset-board when ready.")
        return
    print("Board reset complete. All 32 pieces are at their starting squares; parking is empty.")
    print("The checkpoint now contains a fresh standard-position game.")


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


def _main(argv: list[str] | None = None) -> int:
    """Parse game options, load the checkpoint, then run a hardware or simulated session."""
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
    parser.add_argument("--reset-board", action="store_true",
                        help="restore all pieces from a reconciled game checkpoint, then start fresh")
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
    if args.reset_board and (not args.resume or args.moves or args.engine or args.self_play):
        parser.error("--reset-board requires --resume and cannot be combined with moves or an engine")
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
    if args.reset_board and state.pending is not None:
        parser.error("resolve or finish the pending chess move before --reset-board")
    if not args.reset_board and state.reset is not None:
        parser.error("checkpoint contains a board reset; resume with --reset-board")
    runner = GameRunner(state, store, allow_tight_routes=args.allow_tight_routes)
    link = None
    try:
        # Startup reconciliation happens before opening a live serial port.
        # A saved uncertain carry cannot be silently replayed on reconnect.
        if not args.simulate:
            if not _reconcile_at_start(runner, automatic=not args.supervised):
                return 0
            if not args.reset_board:
                _validate_script(runner, args.moves or [])
            serial_log = args.serial_log or args.state.with_name(args.state.stem + ".serial.jsonl")
            link, controller = _connect_physical(
                args.port, full_home=args.home, serial_log=serial_log
            )
        else:
            if not args.reset_board:
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
            print("Physical advancement:", "supervised" if args.supervised else "automatic after matched DONE")
        if args.reset_board:
            _run_reset(args, runner, link, geometry)
        else:
            _play_moves(args, runner, link, geometry)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Inspect the physical board and saved pending action before resuming.")
        if (link is not None and not args.simulate and
                ((runner.state.pending and runner.state.pending.phase == "in_flight") or
                 (runner.state.reset and runner.state.reset.phase == "in_flight"))):
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


def main(argv: list[str] | None = None) -> int:
    """Hold the same checkpoint/port locks as the browser host for this run."""
    preliminary = argparse.ArgumentParser(add_help=False)
    preliminary.add_argument("--state", type=Path)
    preliminary.add_argument("--port")
    preliminary.add_argument("--simulate", action="store_true")
    preliminary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    preliminary.add_argument("--list-ports", action="store_true")
    options, _ = preliminary.parse_known_args(argv)
    if options.state is None or options.list_ports:
        return _main(argv)
    if options.simulate:
        port = None
    else:
        try:
            port = options.port or _load_config(options.config)["serial_port"]
        except GameStateError:
            # Let the full parser report its usual configuration error.
            return _main(argv)
    try:
        with SessionLock(options.state, port):
            return _main(argv)
    except RuntimeError as exc:
        print(f"Game session unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Local browser control for a checkpointed chess-table game.

Run ``python -m chess_table_host.gui --help``. The HTTP listener binds only to
loopback. A single background worker owns the game runner, Stockfish, and the
serial link; HTTP reads use the last atomic checkpoint so they remain responsive
while a physical CARRY is waiting for controller completion.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import threading

from .gui_http import GuiHTTPServer
from .game import GameStateError
from .host_config import DEFAULT_CONFIG, _load_config
from .remote_http import RemoteHTTPServer, read_remote_token
from .gui_controller import GameController, MODES
# Preserve public convenience imports exposed by this entry point.
from .gui_remote import RemoteMoveTicket
from .gui_snapshot import SynchronizedGameStore


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
    parser.add_argument("--remote-token-file", type=Path,
                        help="enable the authenticated loopback remote API with this token file")
    parser.add_argument("--remote-http-port", type=int, default=8766,
                        help="loopback remote API port when --remote-token-file is supplied")
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
    if not 0 < args.remote_http_port < 65536:
        parser.error("--remote-http-port must be in 1..65535")
    if args.remote_token_file and args.remote_http_port == args.http_port:
        parser.error("--remote-http-port must differ from --http-port")
    try:
        remote_token = read_remote_token(args.remote_token_file) if args.remote_token_file else None
    except ValueError as exc:
        parser.error(str(exc))
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
    remote_server = None
    remote_thread = None
    try:
        with GuiHTTPServer(("127.0.0.1", args.http_port), controller) as server:
            if remote_token is not None:
                remote_server = RemoteHTTPServer(
                    ("127.0.0.1", args.remote_http_port), controller, remote_token,
                )
                remote_thread = threading.Thread(target=remote_server.serve_forever, daemon=True)
                remote_thread.start()
                print(f"Chess table remote API: http://127.0.0.1:{args.remote_http_port}/v1/status")
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
        if remote_server is not None:
            remote_server.shutdown()
            remote_server.server_close()
        if remote_thread is not None:
            remote_thread.join(timeout=5)
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

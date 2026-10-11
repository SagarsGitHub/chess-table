"""Shared geometry, engine selection and checkpoint archival for host entry points.

Entry points retain link and worker ownership. Connection setup returns a
referenced link; it never starts game play or sends a piece transfer.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Callable

import chess

from .game import GameRunner, GameStateError, MoveDeferred
from .planner import PlanError
from .routing import RouteError, RouteGeometry
from .link_adapters import LoggedSerialStream
from .serial_link import ControllerGeometry, open_serial


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



def connect_physical(
    port: str, *, full_home: bool, serial_log: Path,
    on_open: Callable[[object], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    link_factory: Callable[[str], object] = open_serial,
):
    link = link_factory(port)
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

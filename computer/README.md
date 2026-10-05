# Computer-side move planner

`chess_table_host` uses `python-chess` to check UCI moves and converts each legal
move into ordered **logical** transfers between board squares and perimeter
parking slots. Slot IDs are `0..31`; the firmware's internal `PieceMap`
represents the same slots as IDs `64..95`. The host also plans bounded carry
routes and speaks the Uno's version 1 framed serial protocol.

Install `requirements.txt`, then run `python -m unittest discover -s tests -v`
from this directory.

```python
import chess
from chess_table_host import ParkingInventory, plan_move

board = chess.Board()
parking = ParkingInventory.empty()
plan = plan_move(board, "e2e4", parking)
for action in plan.actions:
    print(action)
# Commit board and parking state only after each physical action succeeds.
```

Capture plans park the victim before moving the attacker. En passant parks the
pawn from the square behind the destination. Castling moves the king and rook
in separate steps. On promotion, the pawn moves to the last rank and stays as
the physical stand-in for the selected queen, rook, bishop, or knight. Chess
rules and Stockfish use the selected type; later physical transfers still move
the pawn. If that promoted piece is captured, its physical pawn is parked.
There is no spare-piece exchange or retrieval from parking. A capture requires
an empty parking slot, so planning fails explicitly if storage is full.

## Carry routing

`plan_transfer_route()` takes one `Transfer` plus the occupied squares and
parking slots **at that action's start**. Its points use the firmware's
half-square coordinates: board centers are odd/odd (`a1=(1,1)`), parking is
on the even-coordinate perimeter, and the route passes through adjacent
even/even grid intersections. Pickup and delivery are included. Straight grid
runs are compressed to turn points. A vacant edge square beside a parking slot
may be used to approach it when adjacent slots are occupied. There can be at
most 84 points; the nominal empty-perimeter matrix of 64 board squares × 32
slots needs at most four. Set `max_waypoints` to a controller's capacity; over-cap routes raise
`RouteTooLongError` and are never truncated.

By default these are **nominal paths**: they avoid occupied piece centers but
do not establish physical clearance. `RouteGeometry` requires measured X/Y
travel spans plus the firmware's end inset and pulses-per-mm scale, along with
square pitch, moving and stationary piece diameters, and a safety margin.
Optional saved X/Y origins override the centered-calibration formula. Read
these machine values with `SerialLink.geometry()` after `home()` or
`reference()`. It mirrors the firmware's inward perimeter clamp and pulse
rounding before its circular-footprint clearance check. Allow a margin for
four-decimal reply rounding, piece shape, centering error, magnet behavior,
and path tracking. An impossible clearance raises
`NoRouteError`.

The default move planner selects the lowest empty slot. Pass a `slot_selector`
callback to choose a reachable one; the callback gets the source square,
logical piece symbol, and current immutable 32-slot snapshot. The game runner
converts each transfer to its physical pawn token before routing. For one transfer,
`find_reachable_parking_slot()` tries free slots in order using the supplied
occupancy and geometry. For a capturing move, create a fresh
`ReachableParkingSlotSelector(occupied_squares, geometry=...,
max_waypoints=...)` for that `plan_move()` call. The resulting plan still needs
physical execution and acknowledgement before board state is committed.
After a promotion, use the game runner for physical play: a standalone
`plan_move()` draft does not have the physical stand-in history needed to
identify a captured promoted pawn as a pawn in parking.

## Serial transport

`chess_table_host.serial_link.open_serial(port)` opens a 115200-baud link using
`pyserial`. Call `hello()` to check capabilities, then `home()` or `reference()`
and inspect `status()` and `geometry()`. `save_geometry()` stores a completed
calibration.
After inspecting and reconciling the actual board, `set_map(encode_map(board,
parking))` declares its 96-position occupancy. Plan each `Transfer` against
that action's current occupancy with `max_waypoints=12`, then send
`carry(*encode_carry(route))`. The transport matches command IDs and waits for
both `ACK` and terminal `DONE`/`ERR`; it treats reset, timeout, STOP, and
ambiguous motion faults as requiring physical reconciliation. See
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md) for exact frames.

## Game runner

`python -m chess_table_host.play` joins the legal-move planner, measured route
planner, and serial link. It preflights every transfer in a move before starting
the first one, chooses a reachable parking slot, and saves an atomic JSON
checkpoint after each completed physical action. Captures keep a separate
physical position while the victim is parked and the attacker has not moved.
The local browser GUI uses the same game runner and checkpoints; see the
[WSL GUI game runbook](../docs/GUI_GAME.md) for simulated and physical launch
commands, recovery, and board reset.
Physical sessions save timestamped TX/RX serial frames beside the checkpoint
as `<state-name>.serial.jsonl`; `--serial-log` changes that path.
An interrupted action is never replayed automatically; on resume the operator
must inspect the board and choose whether the piece is at the source or
destination (or restore the entire pre-move position). In ordinary play a
matched `DONE CARRY` advances expected occupancy without a confirmation prompt.
Use `--supervised` to request move, route, and placement prompts during a
deliberate test. The controller cannot sense whether the piece was delivered.
The checkpoint records the legal promoted type and the physical pawn stand-in
separately. For promotion acceptance and fault recovery, compare the legal
type with the physical pawn that was moved or parked.

The checked-in [`config.json`](config.json) defaults to serial port `COM3`,
Stockfish in `~/Documents/stockfish`, 18 mm moving and standing diameters, and
a 0 mm margin. It enables `allow_tight_routes`: the runner tries measured
piece clearance first, then permits a route closer to occupied pieces if it
still avoids their centers, stays within measured travel, and fits 12 waypoints.
The CLI marks these carries `CLEARANCE OVERRIDE`; `--strict-clearance` disables
the fallback for one session. Edit the config, pass another JSON file with
`--config`, or override individual values with `--port`, `--engine`, `--moving-diameter`,
`--standing-diameter`, and `--margin`. Use
`python -m chess_table_host.play --list-ports` to find a connected Uno. The
runner reads fresh `GEOM` from the controller for physical play. The 0 mm
margin is the current physical test setting, not a measured path-error
allowance. The circular clearance model may reject a tight route that works
in practice; the fallback
can route that transfer without relaxing firmware limits or the command format.

For Ubuntu WSL 2 in the Coding checkout, use `.venv/bin/python` and override
the Windows defaults with the attached Linux serial port (`--port /dev/ttyACM0`
or the port detected on your machine) and the WSL path to Stockfish
(`--engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe`).
The [stage 4 runbook](../docs/STAGE4_TEST_GAME.md) has the verified setup and
USB attachment steps. A completed scripted smoke checkpoint should be kept;
use a new state filename for another `--new` run.

For a scripted smoke test, use `--moves` with a fresh `--new --state` file; the
program stops when the script ends. To resume a game against Stockfish, omit
`--moves`. By default it finds a downloaded engine in the configured
`stockfish_path` (including the Windows universal binary). Override that
with `--engine /path/to/stockfish` or another directory on Linux. Human
moves are entered as UCI text; the table then carries both sides' pieces.
`--manual-both` is available for testing without an engine. The runner offers
Stockfish only legal moves that pass physical route preflight.

Use `--self-play` for one Stockfish process to choose both colors without human UCI entry.
From the WSL `computer` directory, run a simulated full game with the saved
geometry fixture first:

```bash
self_play_sim_state="runs/self-play-sim-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$self_play_sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --self-play --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe --engine-time 0.005
```

For a physical game, restore the standard starting position, empty parking,
attach the Uno to WSL, and replace the example port with the detected device:

```bash
serial_port=/dev/ttyACM0
self_play_state="runs/self-play-physical-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$self_play_state" --port "$serial_port" --self-play --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

An interrupted game can continue with `--resume --state` pointing to its
existing checkpoint, plus the same `--self-play`, engine, and connection or
simulation options. Normal physical play advances after a matched `DONE CARRY`.
The table cannot sense pieces, so an uncertain carry or fault requires
inspection and reconciliation before resuming. The runbook below has the USB
attachment and recovery steps.

See [the stage 4 physical test runbook](../docs/STAGE4_TEST_GAME.md) for exact
simulation, port discovery, physical smoke, and resume commands.

## Current limits

- General route execution has software and compiled-Uno simulator coverage,
  plus a 155-ply loaded physical self-play run that stopped during its next
  carry. A complete game, all parking slots, and direct-diagonal routing
  remain to be verified on the board.
- Game checkpoints and source/destination recovery rely on an operator's
  inspection. There is no camera or piece sensor to reconcile automatically.
- A promoted pawn looks like a pawn even though it moves under the selected
  promoted piece's rules. The operator must use the displayed legal game state
  when entering moves or reconciling a fault; there is no piece sensor to
  distinguish the stand-in by sight.
- The planner covers standard chess, not Chess960 castling. It records piece
  type and color per slot, not the identity of identical pieces.

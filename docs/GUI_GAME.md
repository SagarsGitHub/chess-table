# Full-game GUI from WSL

The GUI is a local browser front end to the checkpointed game runner. It shows
the legal position, expected physical board, parking slots, move history, and
current motion status. The table has no piece sensors: enter a human move in
the GUI and leave the pieces in place for the table to carry. A matched
controller `DONE CARRY` advances the expected position in normal play. An
uncertain transfer needs physical inspection and reconciliation.

## Start a simulated game

Use the Coding checkout in Ubuntu WSL 2. Install dependencies in its Linux
virtual environment if needed:

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The checked-in config points to a Windows serial port and Stockfish folder.
The command below gives the WSL path to the downloaded Stockfish executable.
Use a fresh checkpoint filename for each `--new` game:

```bash
sim_state="runs/gui-sim-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.gui --simulate --new --state "$sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

Open the printed `http://localhost:8765/` address in a browser on the same
computer. Choose **Play Stockfish · White**, **Play Stockfish · Black**,
**Stockfish vs Stockfish**, or **Manual · Both sides**, then select **Start
game**. On a human turn, select a piece and destination; for promotion, choose
the legal piece in the promotion dialog. The physical pawn remains a pawn
stand-in for that piece. Simulation sends no commands to the Uno and does not
move the physical board.

To stop the local server, press Ctrl+C in its WSL terminal. Resume the saved
simulation with the same state file and geometry fixture:

```bash
.venv/bin/python -m chess_table_host.gui --simulate --resume --state "$sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

The default browser port is 8765. Use `--http-port NUMBER` if another local
process already uses it, then open the address printed by the program. A
checkpoint belongs to one running GUI or CLI session at a time; close the
other session before opening it here.

## Start a physical game

Attach the Uno to WSL and identify its `/dev/ttyACM*` or `/dev/ttyUSB*` path
using the [stage 4 WSL USB instructions](STAGE4_TEST_GAME.md#connect-the-uno-to-wsl-for-physical-play).
Close Arduino Serial Monitor. Start with the standard physical position and
all parking slots empty. Keep the independent power cutoff available and
watch the table during every carry.

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
serial_port=/dev/ttyACM0  # replace with the detected WSL serial device
physical_state="runs/gui-physical-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.gui --new --state "$physical_state" --port "$serial_port" --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

Open the printed local URL, choose the play mode, and select **Start game**.
The host contacts the Uno, checks protocol capabilities, runs saved-geometry
`REF` or full `HOME` when needed, reads live `GEOM`, and sends `MAP` before
each `CARRY`. There is no separate Serial Monitor homing or `GEOM` step. Add
`--home` to force a full home. The default config's `COM3` is a Windows port;
always pass the detected `/dev/...` device with `--port` in WSL. A physical
session writes the serial transcript beside its checkpoint as
`<state-name>.serial.jsonl`.

Normal play does not ask for confirmation after every completed carry. The
configured tight-route fallback may route closer to occupied pieces than the
18 mm circular footprint model allows while preserving center avoidance,
travel limits, and the 12-waypoint bound. Watch neighboring pieces. Pass
`--strict-clearance` to disable this fallback for a session.

After stopping and closing the GUI server, resume the same physical checkpoint:

```bash
.venv/bin/python -m chess_table_host.gui --resume --state "$physical_state" --port "$serial_port" --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

The physical session observed on 2026-10-05 at `localhost:8765` uses
`runs/gui-physical-20261005-152550.json` on `/dev/ttyACM0`. Its completed
126-ply game ended in a draw, with 20 pieces parked. The older reset planner
reported that retrieving the remaining parked pieces would trap a slot. It
failed during full-plan preflight, before sending a reset `CARRY`; the saved
board and parking inventory remain the source for the next attempt. To load
the updated host code, stop that GUI server after it is idle, then restart it
from WSL with the **same** checkpoint:

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
.venv/bin/python -m chess_table_host.gui --resume --state runs/gui-physical-20261005-152550.json --port /dev/ttyACM0 --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

Compare the displayed expected board and all occupied parking slots with the
table before requesting a loaded reset. Do not use `--new` for this position.
The updated reset planner can temporarily stage a parked piece on an interior
square to break a cycle of mutually blocked slot exits; it still preflights
the complete return route before sending the first reset `CARRY`. A copy of
this 126-ply checkpoint completed the 32-transfer reset in simulation, but
the position still needs an observed physical reset.

## Pause, recover, and reset

**End game & reset board** is available before checkmate or another game
result. During play, it stops choosing new moves, finishes any chess move
already in progress, including all transfers in a capture, then archives the
played checkpoint and begins the board reset. If play is paused with a ready
pending move, the host finishes that move first. The button shows the pending
reset request while the current move settles. Once all 32 pieces are home and
parking is empty, the saved state becomes a fresh standard-position game.
Use this control for an orderly midgame reset.

**Stop motion** sends a controller `STOP` immediately during a physical
`CARRY`, `HOME`, or `REF`. During simulation, engine thinking, or between
transfers, it pauses at the next boundary. An interrupted `CARRY` stays
`in_flight` in the checkpoint. If a transfer stopped, timed out, or lost its
controller connection, inspect the piece, its source and destination, and
affected parking slots before further motion. The recovery panel offers
**Piece at source**, **Piece at destination**, or **Restored prior position**.
Select an outcome only after
putting the actual pieces in that state and marking the inspection checkbox.
Restoring the prior position means restoring the *entire* committed game
position, or the entire pre-reset position during a board reset. A resumed
session must resolve an uncertain checkpoint before **Start game** can
continue. The GUI cannot determine the outcome by sensing the board.

After a completed or paused game with no pending move, the same control reads
**Reset board for new game**. During an interrupted reset, it reads
**Continue board reset**. The host preflights the full return plan before
starting it and archives the played checkpoint before the first reset carry.
If a reset transfer is interrupted, reconcile it in the same way, then
continue the saved plan. An uncertain chess move must be physically inspected
and reconciled before a reset can begin.

## Acceptance record to collect

First run a simulated game through a result and test resume, captures,
parking, castling, en passant, promotion stand-in, fault recovery, and reset.
Then run and observe a physical GUI game and reset. Save the checkpoint,
physical serial transcript, and notes on piece slip, neighboring movement,
stops, and any reconciliation. Passing a simulated game does not complete
the physical stage 4 acceptance or the deferred electrical diagnosis.

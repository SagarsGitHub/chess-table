# Full-game GUI from WSL

The GUI is a local browser front end to the checkpointed game runner. It shows
the legal position, expected physical board, parking slots, move history, and
current motion status. The table has no piece sensors: enter a human move in
the GUI and leave the pieces in place for the table to carry. A matched
controller `DONE CARRY` advances the expected position in normal play. An
uncertain transfer needs physical inspection and reconciliation.

The parking view places all 32 labeled slots around an 8×8 board outline in a
9×9 perimeter map. Board and parked pieces use SVG shapes over contrasting
colors, and flipping the board also flips the parking view. These displays
show checkpointed expected locations; they do not detect actual pieces.

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

The updated Uno firmware carries at 50 mm/s and 400 mm/s², and slows a loaded
horizontal grid segment beside occupied squares to 30 mm/s and 250 mm/s². It
logs `tight piece mm/s=30.0` when that profile is selected. Flash the updated
firmware before evaluating these speeds on the table; refreshing the GUI alone
does not change motor timing. Stop the GUI server before flashing so it releases
the serial port, then resume the same checkpoint.

After stopping and closing the GUI server, resume the same physical checkpoint:

```bash
.venv/bin/python -m chess_table_host.gui --resume --state "$physical_state" --port "$serial_port" --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

The physical session observed on 2026-10-05 at `localhost:8765` uses
`runs/gui-physical-20261005-152550.json` on `/dev/ttyACM0`. Its 126-ply game
ended in a draw, with 20 pieces parked. The old planner rejected the first
reset request in full-plan preflight, before sending a reset `CARRY`. The
revised planner staged a parked piece to break the exit cycle. The user
reports that the physical reset worked; the saved checkpoint now records
`board_reset_completed` with `previous_plies: 126`, a fresh standard position,
32 pieces on the board, and empty parking.

The reset serial transcript records 32 `CARRY` requests and 31 `DONE CARRY`
responses. One `@35 ERR MOTION` occurred after its last waypoint, followed by
`!BOOT`, reconnection, reconciliation, and eventual reset completion. This is
evidence of a completed physical reset with recovery, not a fault-free loaded
run. The displayed state still needs comparison with the actual table because
the checkpoint cannot sense pieces.

To reopen this saved session after the current GUI server closes, use
`--resume` from WSL:

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
.venv/bin/python -m chess_table_host.gui --resume --state runs/gui-physical-20261005-152550.json --port /dev/ttyACM0 --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

Use `--resume` for this existing checkpoint; `--new` requires a different
filename. The completed 126-ply game is preserved in the pre-reset archive.
Before a new physical game, compare the standard-position display and empty
parking map with the actual pieces and slots. The reset planner preflights its
full route before sending the first reset `CARRY`.

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

The 126-ply physical game and recovered reset establish a completed
game-and-reset path. Continue testing resume, captures, castling, en passant,
promotion stand-in, midgame **End game & reset board**, and fault recovery.
Save each checkpoint, physical serial transcript, and notes on piece slip,
neighboring movement, stops, and reconciliation. A fault-free loaded run and
the deferred electrical diagnosis remain open.

# Stage 4 physical test game

This runbook is for the first supervised game-orchestration smoke test. It has
not been run on the physical table. A successful five-ply line is progress
toward stage 4, not acceptance of a complete game or all 32 parking slots.

## Board and clearance gate

The reported live calibration is:

```text
@4 DONE GEOM X=249.7350 Y=251.9850 OX=-0.1325 OY=0.9925 P=31.2500 I=2.0000 PPM=133.3333
```

The physical game command handles referencing and geometry automatically. On
startup it reads controller status, runs `REF` when saved calibration exists
or `HOME` when it does not, then requests a fresh `GEOM` and prints `Live GEOM`.
Use `--home` to force a full `HOME` even when calibration is saved. Record that
printed response and the serial transcript for each physical session; there
is no separate Serial Monitor step.

The displayed spans and origins are internally consistent with a 250 mm
printed board: `OX=(X-250)/2` and `OY=(Y-250)/2`. They establish reachable
square centers, not piece clearance. The outer parking lanes are clamped to
the 2 mm inset and run close to occupied edge squares. The user measured
18 mm moving and 18 mm standing footprints. The commands below use these
values with a **provisional 0 mm margin**; centering, wobble, and path error
still need to be measured and allowed for. The runner uses the live `GEOM`
values for physical play and rejects a move if any transfer has no route
within the Uno's 12-waypoint limit. Its circular-clearance model can also
reject an empirically viable tight route; the runner currently fails closed
instead of forcing motion. Record that case for route or hardware refinement.

The magnet-release reset is still tracked as [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1).
Do not rely on this game run to certify the unresolved electrical behavior.
Keep the independent power cutoff available and watch for `!BOOT` at every
release.

## Five-ply smoke line

Start with the physical standard chess position and all parking slots empty.
Confirm that the host's FEN and parking inventory match them. The runner
references the Uno before routing and sends `MAP` before each `CARRY`. Check
the serial transcript for a successful `HOME` or `REF`, a fresh `GEOM`, and
the initial `MAP` value `FFFF00000000FFFF00000000`. The runner preflights the
**entire** next move against current occupancy before sending its first
`CARRY`.

| Ply | UCI | Expected physical action |
| --- | --- | --- |
| White 1 | `e2e4` | Carry the white pawn from e2 to e4. |
| Black 1 | `a7a6` | Carry the black pawn from a7 to a6. |
| White 2 | `e4e5` | Carry the white pawn from e4 to e5. |
| Black 2 | `d7d5` | Carry the black pawn from d7 to d5. |
| White 3 | `e5d6` | En passant: park the black pawn from d5 first; then carry the white pawn from e5 to d6. |

With the reported geometry, 18/18 mm footprints, and provisional 0 mm margin,
the current planner chose P19 for the captured pawn. This is an illustrative
software result, not a fixed slot assignment or physical clearance guarantee.
The runner must show the planned route and chosen slot for operator review.
If preflight fails, stop that ply before either piece moves.

## Promotion rule for a longer game

There are no spare promoted pieces. For a legal promotion such as a UCI move
ending in `q`, `r`, `b`, or `n`, the table carries the pawn to the last rank and
leaves that same physical pawn there. The game position and Stockfish treat it
as the selected queen, rook, bishop, or knight. On later legal moves the table
carries the pawn stand-in; if the promoted piece is captured, it parks that
physical pawn. No replacement piece is taken from parking, and the operator
does not exchange pieces by hand. After promotion and each later action,
confirm both the legal piece shown in the game state and the actual pawn's
location. On resume, reconcile the pawn stand-in with its recorded legal type.

## PowerShell commands

Close the Arduino Serial Monitor before starting; only one program should use
the Uno's COM port. [`computer/config.json`](../computer/config.json) defaults
to `COM3`, 18/18 mm footprints, a 0 mm margin, and
`~/Documents/stockfish`. Edit that file to change these defaults, pass
`--config path/to/another.json`, or override a single run with `--port COMx`
and `--engine /path/to/stockfish`. From PowerShell, enter the computer
directory and check the connected ports:

```powershell
Set-Location 'C:\Users\sagar\Documents\ChatGPT\chess table\computer'
& .venv/Scripts/python.exe -m chess_table_host.play --list-ports
```

Run the complete line in simulation first. `--new` requires a fresh state
file; choose another path if this one already exists:

```powershell
& .venv/Scripts/python.exe -m chess_table_host.play --simulate --new --state runs/smoke.json --geometry-file fixtures/board_geometry_2026-10-05.json --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

After the physical standard setup is verified, use a separate fresh state
file. The command uses configured `COM3`; if the Uno appears on another port,
change `config.json` or add `--port COMx`:

```powershell
& .venv/Scripts/python.exe -m chess_table_host.play --new --state runs/physical-smoke.json --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

To continue that checkpoint against Stockfish, use `--resume` and the same
physical state file and configured port. The runner finds Stockfish in
`~/Documents/stockfish` by default; White remains the human side. Enter the
intended White UCI move without moving the piece by hand; the table carries
both White and Black pieces:

```powershell
& .venv/Scripts/python.exe -m chess_table_host.play --resume --state runs/physical-smoke.json --human white
```

On Linux or another installation layout, add `--engine /path/to/stockfish`;
the override accepts an executable or a folder containing one.

The runner restricts Stockfish to legal moves that pass route preflight. If
none pass the current clearance model, it pauses the game. Check the saved
state and actual board before continuing; do not bypass the route check for a
loaded move.

The physical runner writes a timestamped serial transcript beside the state
file as `runs/physical-smoke.serial.jsonl`. It records request frames,
`ACK`/`DONE`/`ERR`, and `!BOOT`; `--serial-log path` changes its location.

For each transfer, require `ACK` and terminal `DONE CARRY`, then have the
operator confirm pickup, final placement, and that nearby pieces did not
shift. `DONE CARRY` reports commanded motion and relay timing; it cannot sense
the pieces. Update the host's physical occupancy only after that transfer is
confirmed. After the victim is parked, plan and execute the attacker against
the updated occupancy. At the end, confirm a white pawn on d6, e2/e4/e5
empty, d5 empty, the black d-pawn in the selected parking slot, the black
a-pawn on a6, and all other pieces unchanged.

## Interruption and recovery

On an unexpected position, missed pickup or release, route fault, limit stop,
timeout, disconnect, or `!BOOT`, stop the game. Record the request, ACK/terminal
response, and actual piece locations. Do not retry the uncertain `CARRY` or
advance the logical move automatically. Reconcile every board square and
parking slot by inspection, restore the carriage reference with `HOME` or
`REF`, read `GEOM`, send a new truthful `MAP`, and resume only from the
reconciled game state. For a fault between victim parking and attacker motion,
the victim remains parked in the physical inventory; the chess move is still
unfinished. On resume, choose SOURCE or DESTINATION only after restoring or
verifying that action's result. A move paused between confirmed actions can
continue from its checkpoint or be aborted after manually restoring the entire
pre-move board and parking inventory.

## Evidence to keep

Save the firmware/host commit, complete serial log (including resets), live
`GEOM`, measured footprints and margin, FEN and parking inventory before and
after each ply, chosen routes/slots, operator confirmations, and any observed
reset or displacement. For a promotion, record the chosen legal type and the
physical pawn's location through later movement or capture. Stage 4 acceptance
still requires a complete local game, tested special moves, and mid-capture
recovery as described in [ROADMAP.md](ROADMAP.md). Loaded parking coverage remains tracked in
[issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

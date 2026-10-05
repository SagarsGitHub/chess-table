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

## Run from the Coding checkout in WSL

Open Ubuntu (WSL 2). This Windows checkout appears at
`/mnt/c/Users/sagar/Documents/Coding/chess-table`. Create a Linux virtual
environment in its `computer` directory; the `.venv` directory is ignored by
Git. The checked-in `config.json` still has Windows defaults (`COM3` and
`~/Documents/stockfish`), so the physical WSL commands below override them.

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
```

Run the complete five-ply line in simulation first. Every `--new` run needs
a new checkpoint name; `--resume` continues a saved game and does not replay
its completed moves. The timestamp keeps earlier runs available:

```bash
sim_state="runs/smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

### Connect the Uno to WSL for physical play

At the 2026-10-05 setup check, the Uno appeared as `COM3` in Windows but was
not attached to WSL. WSL 2 requires USB passthrough; a Windows COM number is
not a WSL serial path.
Follow [Microsoft's USB connection guide](https://learn.microsoft.com/en-us/windows/wsl/connect-usb)
to install `usbipd-win` if needed. Close Arduino Serial Monitor first. In an
**administrator PowerShell**, list devices, identify the Uno's bus ID, and
share it once:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <BUSID>
```

If `usbipd` is not found immediately after installation, open a new
administrator PowerShell before running `usbipd list` and `bind`.

Then, in a regular PowerShell, attach that bus ID to WSL. Repeat attachment
after a reboot or USB disconnect. Windows cannot use the Uno while it is
attached to WSL.

```powershell
usbipd attach --wsl --busid <BUSID>
```

Back in WSL, discover the actual Linux port. It may be `/dev/ttyACM0` or
`/dev/ttyUSB0`. The `Configured serial port: COM3` line from `--list-ports`
is only the Windows default; use the detected `/dev/...` port with `--port`.
If access is denied, check the device permissions and add your WSL user to
`dialout` with `sudo usermod -aG dialout "$USER"`, then start a new WSL login
and attach the Uno again.

```bash
.venv/bin/python -m chess_table_host.play --list-ports
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

After verifying the standard starting position and empty parking, set the
detected port below and start a fresh physical checkpoint. The runner performs
`HOME` or `REF`, reads live `GEOM`, and sends `MAP` itself. Add `--home` only if
you want to force a full calibration.

```bash
serial_port=/dev/ttyACM0  # replace with the port reported on your WSL machine
physical_state="runs/physical-smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$physical_state" --port "$serial_port" --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

To continue that checkpoint against Stockfish, keep the same state file and
port. The existing Windows Stockfish executable was verified from this WSL
installation; pass its WSL path because WSL's `~/Documents` is different from
Windows Documents. White remains the human side. Enter the intended White
UCI move without moving the piece by hand; the table carries both sides.

```bash
.venv/bin/python -m chess_table_host.play --resume --state "$physical_state" --port "$serial_port" --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe --human white
```

If you open a new WSL shell, set `physical_state` and `serial_port` again to
the saved file and attached port before using `--resume`.

The runner restricts Stockfish to legal moves that pass route preflight. If
none pass the current clearance model, it pauses the game. Check the saved
state and actual board before continuing; do not bypass the route check for a
loaded move.

The physical runner writes a serial transcript beside the chosen state file
with the suffix `.serial.jsonl`. It records request frames, `ACK`/`DONE`/`ERR`,
and `!BOOT`; `--serial-log path` changes its location.

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
parking slot by inspection, then resume only from the reconciled game state.
The runner restores the carriage reference with `HOME` or `REF`, reads `GEOM`,
and sends a truthful `MAP` before the next `CARRY`. For a fault between victim
parking and attacker motion, the victim remains parked in the physical
inventory; the chess move is still unfinished. On resume, choose SOURCE or
DESTINATION only after restoring or verifying that action's result. A move
paused between confirmed actions can continue from its checkpoint or be
aborted after manually restoring the entire pre-move board and parking
inventory.

## Evidence to keep

Save the firmware/host commit, complete serial log (including resets), live
`GEOM`, measured footprints and margin, FEN and parking inventory before and
after each ply, chosen routes/slots, operator confirmations, and any observed
reset or displacement. For a promotion, record the chosen legal type and the
physical pawn's location through later movement or capture. Stage 4 acceptance
still requires a complete local game, tested special moves, and mid-capture
recovery as described in [ROADMAP.md](ROADMAP.md). Loaded parking coverage remains tracked in
[issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

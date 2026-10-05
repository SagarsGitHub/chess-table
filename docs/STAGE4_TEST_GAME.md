# Stage 4 physical test game

This runbook covers the smoke line and continuation toward a
complete physical game against Stockfish. The saved 2026-10-05 physical run
reached 16 committed plies and four occupied parking slots. A complete game,
promotion, all 32 parking slots, and fault recovery still need physical
acceptance evidence. This table has no piece sensors: the human enters UCI
moves, successful `DONE CARRY` responses advance expected occupancy during
ordinary play, and uncertain outcomes require physical reconciliation.

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
still need to be measured and allowed for. The runner uses live `GEOM` for
physical play. It first searches for a route with the configured circular
piece clearance. If that fails and `allow_tight_routes` is enabled, it may
use a bounded route that avoids occupied centers but can pass closer than
the measured diameters permit. The CLI labels this `CLEARANCE OVERRIDE` in
its output. Normal play continues without a route prompt; `--supervised`
adds deliberate prompts for a test session. Watch nearby pieces and keep the
independent power cutoff ready. The override does not relax travel bounds,
firmware limit handling, or the Uno's 12-waypoint limit. Use
`--strict-clearance` to disable fallback for a session; `--allow-tight-routes`
enables it even if the config disables it. An override is a configured
physical tradeoff, not measured clearance certification.

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
the earlier planner chose P19 for the captured pawn. This is an illustrative
result, not a fixed slot assignment or physical clearance guarantee. The
runner prints the planned route and chosen slot. If preflight fails, stop
that ply before either piece moves.

The ignored `computer/runs/physical-smoke-20261005-013125.json` checkpoint
records 16 committed plies through `h7h5`, no pending action, and parked
pieces in P19–P22. Its paired serial transcript records 20 `CARRY` requests
and 20 matching `DONE CARRY` responses without a controller `ERR`. It also
records four `!BOOT` announcements across connection sessions, so it does not
prove a reset-free loaded run. The user reports a later knight route rejected
by the clearance model; the failed move's exact UCI is not verified in this
checkpoint. From a copy of that checkpoint, strict preflight rejected all
four legal white knight moves, while the fallback found bounded routes. A
simulated `g1f3` continuation committed as ply 17 and displayed `CLEARANCE
OVERRIDE`; the saved physical checkpoint was not changed. These records do
not establish loaded knight-route or full-game acceptance.

## Promotion rule for a longer game

There are no spare promoted pieces. For a legal promotion such as a UCI move
ending in `q`, `r`, `b`, or `n`, the table carries the pawn to the last rank and
leaves that same physical pawn there. The game position and Stockfish treat it
as the selected queen, rook, bishop, or knight. On later legal moves the table
carries the pawn stand-in; if the promoted piece is captured, it parks that
physical pawn. No replacement piece is taken from parking, and the operator
does not exchange pieces by hand. For promotion acceptance, inspect the legal
piece shown in the game state and the actual pawn's location after promotion,
a later move, and capture. On resume after an uncertain outcome, reconcile
the pawn stand-in with its recorded legal type.

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

The 2026-10-05 physical run confirms that this checkout can operate the Uno
through WSL. Reattach the device after a reboot or USB disconnect. WSL 2 uses
USB passthrough; a Windows COM number is not a WSL serial path.
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
you want to force a full calibration. Add `--supervised` if you deliberately
want move, route, and placement prompts during a hardware test; normal play
advances after matched `DONE CARRY` responses.

```bash
serial_port=/dev/ttyACM0  # replace with the port reported on your WSL machine
physical_state="runs/physical-smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$physical_state" --port "$serial_port" --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

To continue a checkpoint against Stockfish, keep the same state file and
port. The existing Windows Stockfish executable was verified from this WSL
installation; pass its WSL path because WSL's `~/Documents` is different from
Windows Documents. White remains the human side. Enter the intended White
UCI move without moving the piece by hand; the table carries both sides.
The checked-in config enables `allow_tight_routes`. The runner first tries
measured clearance and only falls back when needed. It prints `CLEARANCE
OVERRIDE` when a transfer uses that fallback. Keep sight of the table during
motion and stop if a piece shifts. To refuse every tight route for a session, add
`--strict-clearance`; to enable them with a stricter custom config, add
`--allow-tight-routes`.

```bash
.venv/bin/python -m chess_table_host.play --resume --state "$physical_state" --port "$serial_port" --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe --human white
```

### Stockfish against Stockfish

Use `--self-play` to let one Stockfish process choose moves for both colors. No human UCI
entry is needed. Start with a fresh checkpoint and, for physical play, the
standard starting position and empty parking. First run a complete simulated
game in WSL using the saved geometry fixture:

```bash
self_play_sim_state="runs/self-play-sim-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$self_play_sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --self-play --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe --engine-time 0.005
```

Once the Uno is attached and `serial_port` is set to the detected `/dev/...`
device, run the physical game from a separate fresh checkpoint:

```bash
self_play_state="runs/self-play-physical-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$self_play_state" --port "$serial_port" --self-play --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

For an interrupted game, use `--resume` with that same checkpoint and the
same mode (`--simulate` with the geometry fixture, or `--port` with the
attached Uno), `--self-play`, and the engine path. Do not start `--new` on an
existing checkpoint. For a physical resume, inspect the board and all parking
slots against the saved state first; an uncertain carry or fault still needs
the reconciliation steps below. In ordinary play the host advances after each
matched `DONE CARRY`, with no move or placement prompts. It has no piece
sensors, so watch the board and stop if a piece moves incorrectly.

For example, after a checked physical interruption, resume the same saved
game with:

```bash
.venv/bin/python -m chess_table_host.play --resume --state "$self_play_state" --port "$serial_port" --self-play --engine /mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe
```

If you open a new WSL shell, set `physical_state` and `serial_port` again to
the saved file and attached port before using `--resume`. To continue the
recorded 16-ply test, use
`physical_state=runs/physical-smoke-20261005-013125.json` only if the actual
board and all parked pieces still match that checkpoint. Otherwise restore a
standard starting position and start a new game with a fresh `--new` state
filename. Do not use `--resume` to replay completed plies.

The runner restricts Stockfish to legal moves that pass route preflight under
the selected clearance setting. If none pass, it pauses the game. In normal
mode, it sends the transfers and advances expected occupancy after matched
`DONE CARRY` responses without further prompts. A completed command does not
measure where the physical piece ended up. If a piece shifts or the board
differs from the checkpoint, stop and reconcile before continuing.

The physical runner writes a serial transcript beside the chosen state file
with the suffix `.serial.jsonl`. It records request frames, `ACK`/`DONE`/`ERR`,
and `!BOOT`; `--serial-log path` changes its location.

For each transfer, the host requires `ACK` and terminal `DONE CARRY`, then
advances its expected physical occupancy. `DONE CARRY` reports commanded
motion and relay timing; it cannot sense the pieces. After the victim is
parked, the host routes and executes the attacker against the updated
occupancy. For acceptance of the five-ply line, inspect a white pawn on d6,
e2/e4/e5 empty, d5 empty, the black d-pawn in the selected parking slot, the
black a-pawn on a6, and all other pieces unchanged. During a longer game,
watch the board and stop if a carry goes wrong, particularly on a tight route.
Manual UCI entry remains required for human moves; there is no piece sensing
to infer a move or detect a missed pickup.

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
paused between completed actions can continue from its checkpoint or be
aborted after manually restoring the entire pre-move board and parking
inventory.

## Evidence to keep

Save the firmware/host commit, complete serial log (including resets), live
`GEOM`, measured footprints and margin, FEN and parking inventory before and
after each ply, chosen routes/slots, any supervision notes, and any observed
reset or displacement. For a promotion, record the chosen legal type and the
physical pawn's location through later movement or capture. Stage 4 acceptance
still requires a complete local game, tested special moves, and mid-capture
recovery as described in [ROADMAP.md](ROADMAP.md). Loaded parking coverage remains tracked in
[issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

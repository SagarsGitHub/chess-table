# CLI physical game runbook

Use this runbook for WSL USB setup, a short en passant smoke line, Stockfish self-play, board reset, and recovery. The [GUI runbook](GUI_GAME.md) uses the same checkpointed host and Uno. The board has no piece sensors: enter human moves in software and leave the pieces in place for the table to carry. A matched `DONE CARRY` advances expected occupancy automatically; inspect any uncertain transfer before continuing.

## Calibration and clearance

The reported physical calibration was:

```text
@4 DONE GEOM X=249.7350 Y=251.9850 OX=-0.1325 OY=0.9925 P=31.2500 I=2.0000 PPM=133.3333
```

The host runs `REF` with saved calibration or `HOME` without it, then reads fresh `GEOM` and sends `MAP`. Use `--home` to force full calibration. No separate Serial Monitor command is needed. Record the live `GEOM` and serial transcript for each physical session.

The measured board pitch is 31.25 mm; moving and standing pieces have 18 mm footprints. The configured 0 mm margin is provisional because centering, wobble, and path error have not been measured. Two standing pieces leave a 13.25 mm horizontal gap, less than an 18 mm moving piece. The host tries circular footprint clearance first. With `allow_tight_routes`, it may fall back to a route that avoids occupied centers but passes closer to pieces; the CLI prints `CLEARANCE OVERRIDE`. This never relaxes measured travel bounds, firmware limits, or the 12-waypoint cap. Use `--strict-clearance` to disable fallback for a session. Watch nearby pieces and keep the independent power cutoff available.

The reported magnet-release reset remains tracked in [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1); electrical diagnosis is deferred. A successful game command does not certify its root cause.

## Run from the Coding checkout in WSL

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -q
```

The checked-in config has Windows defaults (`COM3` and `~/Documents/stockfish`). In WSL, pass the detected Linux port and set `engine_path` in the Stockfish examples to the installed executable. Each `--new` game needs a fresh state filename; `--resume` continues that file without replaying completed carries. Checkpoints and serial logs under `computer/runs/` are ignored by Git but should be retained as physical evidence.

### Connect the Uno to WSL for physical play

Follow [Microsoft's USB connection guide](https://learn.microsoft.com/en-us/windows/wsl/connect-usb) if `usbipd-win` is not installed. Close Arduino Serial Monitor and any running GUI/CLI session. In **administrator PowerShell**, identify the Uno's bus ID and share it once:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <BUSID>
```

Open a new administrator PowerShell if `usbipd` is not found immediately after installation. In regular PowerShell, attach the Uno to WSL; repeat after a reboot or USB disconnect. Windows cannot use it while attached:

```powershell
usbipd attach --wsl --busid <BUSID>
```

Back in WSL, find its actual device. `COM3` is a Windows default, not a WSL port:

```bash
.venv/bin/python -m chess_table_host.play --list-ports
ls -l /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

If access is denied, check permissions, run `sudo usermod -aG dialout "$USER"`, then start a new WSL login and reattach the Uno.

## Five-ply smoke line

Start from the standard physical position with empty parking. The line `e2e4 a7a6 e4e5 d7d5 e5d6` ends in en passant: the black pawn goes from d5 to a selected parking slot before the white pawn goes from e5 to d6. First run it in simulation:

```bash
sim_state="runs/smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

Simulation sends no Uno commands. For the physical run, compare the board and slots with the starting checkpoint, keep the cutoff reachable, and use the detected port:

```bash
serial_port=/dev/ttyACM0  # replace with the detected WSL device
physical_state="runs/physical-smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$physical_state" --port "$serial_port" --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

The runner preflights the whole next chess move before its first `CARRY`. Its startup transcript should include `HOME` or `REF`, live `GEOM`, and the starting `MAP` bitmap `FFFF00000000FFFF00000000`. After the smoke line, inspect the white pawn on d6, black pawn in the reported slot, a-pawn on a6, and all other pieces. Earlier planning selected P19, but slot choice can change with occupancy and geometry. Use `--supervised` only when deliberately testing with prompts; ordinary play does not ask after each successful carry.

## Continue against Stockfish or run self-play

To continue the smoke checkpoint against Stockfish with human White, enter UCI moves without moving pieces by hand:

```bash
engine_path=/mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe  # change for another machine
.venv/bin/python -m chess_table_host.play --resume --state "$physical_state" --port "$serial_port" --engine "$engine_path" --human white
```

For Stockfish against Stockfish, first use a fresh simulated checkpoint, then a separate physical one starting with all 32 pieces home and parking empty:

```bash
engine_path=/mnt/c/Users/sagar/Documents/stockfish/stockfish-windows-x86-64-universal.exe  # change for another machine
serial_port=/dev/ttyACM0  # replace with the detected WSL device
self_play_sim_state="runs/self-play-sim-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$self_play_sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --self-play --engine "$engine_path" --engine-time 0.005

self_play_state="runs/self-play-physical-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$self_play_state" --port "$serial_port" --self-play --engine "$engine_path"
```

After a checked interruption, resume with the same `--state`, `--self-play`, engine, and connection or simulation arguments. In a new WSL shell, set `engine_path`, `serial_port`, and the state variable again. Do not resume the older 155-ply physical checkpoint until its stopped `h5h4` carry has been physically reconciled; see the [roadmap evidence](ROADMAP.md#physical-progress-recorded-2026-10-05).

Promotion uses the same physical pawn as a stand-in for the selected legal queen, rook, bishop, or knight. Later moves still carry that pawn; its capture parks it as a pawn. No replacement piece is taken from parking.

## Test motion and board reset

Flash the current Uno firmware before judging its motion profile or diagonal routing. Stop the GUI/CLI first so it releases the serial port. The host requires the firmware's `CARRY_DIAG` capability. In WSL, if PlatformIO is not installed:

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/firmware
python3 -m venv .venv
.venv/bin/python -m pip install platformio
serial_port=/dev/ttyACM0  # replace with the detected WSL device
.venv/bin/pio run
.venv/bin/pio run -t upload --upload-port "$serial_port"
cd ../computer
```

Use `pio` instead of `.venv/bin/pio` if PlatformIO is already installed in WSL. The loaded carry profile is 50 mm/s at 400 mm/s²; magnet-on horizontal grid segments beside occupied squares use 30 mm/s at 250 mm/s². Unloaded travel and homing have separate profiles. Longer physical runs work overall, but crowded horizontal lanes can disturb off-center pieces. The host now favors detours; record contact, displacement, slip, and delivery during a loaded trial.

A short diagonal and reset trial clears the bishop's path to h6:

```bash
diag_sim_state="runs/diagonal-sim-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$diag_sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json --moves e2e4 a7a6 d2d3 a6a5 c1h6
.venv/bin/python -m chess_table_host.play --simulate --resume --reset-board --state "$diag_sim_state" --geometry-file fixtures/board_geometry_2026-10-05.json

diag_state="runs/diagonal-physical-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$diag_state" --port "$serial_port" --moves e2e4 a7a6 d2d3 a6a5 c1h6
.venv/bin/python -m chess_table_host.play --resume --reset-board --state "$diag_state" --port "$serial_port"
```

After confirming all 32 pieces are home again, use a fresh checkpoint to test retrieval from parking:

```bash
capture_state="runs/reset-capture-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --new --state "$capture_state" --port "$serial_port" --moves e2e4 d7d5 e4d5
.venv/bin/python -m chess_table_host.play --resume --reset-board --state "$capture_state" --port "$serial_port"
```

Reset preflights the full return, archives the played checkpoint as `.before-reset-<UTC timestamp>.json`, and checkpoints each transfer. It clears the game only when all pieces are home. The planner favors direct bishop and knight returns, then other back-rank pieces, then pawns, but may stage a blocker when slot exits would otherwise become trapped. Compare the proposed order, final board, and empty parking with the actual table.

## Interruption, recovery, and evidence

On a missed pickup, shifted piece, fault, timeout, disconnect, `STOP`, or unexpected `!BOOT`, stop. Record the request and replies and inspect the source, destination, nearby squares, and parking. Never replay an uncertain `CARRY` automatically. On resume, choose SOURCE or DESTINATION only after putting the piece there or verifying it; restoring the prior position means restoring the entire committed board and parking inventory. A capture stopped after victim parking still has the victim parked while the chess move remains unfinished. The host then references the carriage, reads `GEOM`, and sends a truthful `MAP` before another carry.

Physical sessions write `<state-name>.serial.jsonl` beside the checkpoint; `--serial-log path` changes it. Keep the host/firmware commit, checkpoint, serial log, live `GEOM`, measured footprints and margin, routes, parking slots, and observations about slip, neighboring pieces, relay releases, and stops. `DONE CARRY` confirms commanded steps and magnet timing, not delivery. The [roadmap](ROADMAP.md) records the 16-ply smoke run, 155-ply stopped self-play, 126-ply GUI draw and recovered reset, and remaining acceptance work.

# Chess table quick start

The host runs on native Linux or Ubuntu WSL 2; PowerShell wraps WSL. Run the launchers from your local repository root.

## Once per machine

Install the host dependencies in Linux:

```bash
cd /path/to/chess-table/computer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Install Stockfish for engine games. Launchers find `Documents/Stockfish` automatically; Bash also checks lowercase `stockfish` and the Windows Documents folder in WSL. Override with `-Engine` / `--engine`.

For Aina, configure the [companion credential and host token](REMOTE_AINA.md#authentication-and-first-connection). Run one chess-capable companion beside the host in the same Linux/WSL environment. Launchers enable the API but do not start the companion.

## USB access

Close Arduino Serial Monitor and any previous GUI/CLI host before connecting. Native Linux may need serial-group access; run `sudo usermod -aG dialout "$USER"`, then start a new login.

For WSL, install `usbipd-win` and share the Uno once from **administrator PowerShell**:

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
usbipd bind --busid <BUSID>
```

Launchers attach the shared Uno on later runs. To attach manually after a reboot or disconnect, use regular PowerShell:

```powershell
usbipd attach --wsl --busid <BUSID>
```

Windows cannot use the device while attached to WSL. From `computer/` in Linux, list ports with `.venv/bin/python -m chess_table_host.play --list-ports`. Use the detected `/dev/serial/by-id/...`, `/dev/ttyACM*`, or `/dev/ttyUSB*` path; the config's `COM3` is a Windows default.

## Launch

For a fresh physical game, place all 32 pieces on starting squares, empty parking, and keep the independent power cutoff within reach. Run from the repository root:

```powershell
.\Play-ChessTable.ps1                     # Physical, Aina enabled
.\Start-ChessTable.ps1 -EnableAina        # Simulation, Aina enabled
```

On native Linux or inside WSL:

```bash
./Play-ChessTable.sh                     # Physical, Aina enabled
./Start-ChessTable.sh --enable-aina      # Simulation, Aina enabled
```

Each launcher prints a fresh checkpoint path. Keep its terminal open; Ctrl+C stops the host. Use `-UsbBusId <BUSID>` for several matching WSL boards, or `-Port` / `--port` for an attached device. `-DryRun` / `--dry-run` checks settings before starting.

For local browser play, use `Start-ChessTable` without `-EnableAina` / `--enable-aina`; add `-Mode Physical` / `--mode physical` for hardware. Simulation sends no commands to the Uno. See `./Start-ChessTable.sh --help` or PowerShell `Get-Help .\Start-ChessTable.ps1 -Detailed` for options.

## Play from the browser

Open the printed local address, normally `http://localhost:8765/`. Choose **Play Stockfish · White**, **Play Stockfish · Black**, **Stockfish vs Stockfish**, or **Manual · Both sides**, then **Start game**. On a human turn, select a piece and destination. Choose the legal promotion piece in the dialog; the same physical pawn remains its stand-in.

**The board has no piece sensors.** Enter moves in software and leave the pieces for the table to carry. The board and 32-slot parking view show expected positions. Starting physical play can reference or home the carriage; the host reads live `GEOM` and sends `MAP` automatically. Normal matched `DONE CARRY`, `DONE CARRY_OFFSET`, and `DONE OFFSET` need no confirmation, but cannot prove physical delivery.

For Aina play, leave the GUI game unstarted and ask Aina to start manual play or Stockfish with your chosen color. Aina then owns move entry; local STOP and recovery remain available. See [Aina commands and API](REMOTE_AINA.md).

## Resume the same game

Use one running GUI or CLI process per checkpoint. Stop the old host, then reopen the **exact file in its original physical/simulation mode**. Relative paths resolve from `computer/`:

```powershell
.\Play-ChessTable.ps1 -Resume -State 'runs\chess-physical-<saved-name>.json'
.\Start-ChessTable.ps1 -Mode Simulation -EnableAina -Resume -State 'runs\chess-sim-<saved-name>.json'
```

```bash
./Play-ChessTable.sh --resume 'runs/chess-physical-<saved-name>.json'
./Start-ChessTable.sh --mode simulation --enable-aina --resume 'runs/chess-sim-<saved-name>.json'
```

For Aina, send a matching session start with the saved mode and human color. Restarting the process alone does not resume play. A browser refresh only reloads the interface. After firmware changes, stop the host, [flash the Uno](../firmware/README.md#build-and-verification), then resume the checkpoint.

## Pause, recover, and reset

**Stop motion** sends `STOP` immediately during a physical carry, offset, home, or reference. During engine thinking, simulation, or between actions, it pauses at the next boundary. An interrupted action stays `in_flight` in the checkpoint.

**Never replay an uncertain carry.** After STOP, fault, timeout, disconnect, reboot, missed pickup, or shifted piece, inspect the magnet, transfer endpoints, neighbors, and parking before more motion. In the local recovery panel:

- For a carry, choose **Piece at source**, **Piece at destination**, or **Restored prior position** only after verifying or physically restoring that state.
- For an offset, verify **Piece at center** or **Piece at corner**, or restore the prior position.
- Restoring the prior position means the **entire committed board and parking inventory**, or the entire pre-reset position during reset. A capture interrupted after victim parking can leave the victim parked while the chess move is unfinished.

Mark the inspection checkbox only after checking the actual pieces. Resolve uncertainty before **Start game** or reset. The host then restores carriage reference and matching expected occupancy; it cannot inspect the board for you.

**End game & reset board** requests an orderly midgame reset: finish the current chess move, including every capture transfer, archive its checkpoint, then return pieces. With no pending move, the control reads **Reset board for new game**. Reset preflights the full plan and saves each action; a fresh standard-position game begins only after all 32 pieces are home and parking is empty. After an interrupted reset, inspect/reconcile its action and choose **Continue board reset**.

Preserve `computer/runs/` checkpoints, `.before-reset-<UTC timestamp>.json` archives, adjacent `.serial.jsonl` transcripts, live geometry, and notes about stops, contact, slip, and delivery. Keep machine-specific run evidence in your private project notes. See [routing limits](ROUTING.md) and [accepted/pending physical work](ROADMAP.md).

## Optional CLI smoke test

From `computer/`, first simulate the five-ply en passant line using a fresh state:

```bash
state="runs/smoke-$(date +%Y%m%d-%H%M%S).json"
.venv/bin/python -m chess_table_host.play --simulate --new --state "$state" --geometry-file fixtures/board_geometry_2026-10-05.json --moves e2e4 a7a6 e4e5 d7d5 e5d6
```

For an attended physical trial from standard setup, use a **different fresh state**, replace `--simulate --geometry-file ...` with `--port /dev/serial/by-id/...`, and inspect the white pawn on d6, black pawn in its selected parking slot, a-pawn on a6, and other pieces. Add `--supervised` only when deliberately testing per-transfer prompts.

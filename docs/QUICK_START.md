# Chess table quick start

On this machine, use `C:\Users\sagar\Documents\Coding\chess-table`; elsewhere, use the local repository root. The host runs in native Linux or Ubuntu WSL 2. PowerShell wraps the WSL launcher.

## Once per machine

1. Install host dependencies in Linux:

   ```bash
   cd /path/to/chess-table/computer
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

2. Connect the Uno. Native Linux needs serial access, commonly membership in `dialout` followed by a new login. For WSL, install `usbipd-win`, find the Uno with `usbipd list`, and run `usbipd bind --busid <BUSID>` once in administrator PowerShell. Launchers attach the shared Uno on later runs. See the [USB setup](STAGE4_TEST_GAME.md#connect-the-uno-to-wsl-for-physical-play).
3. Install Stockfish for engine games. Launchers find `Documents/Stockfish` automatically (Bash also checks lowercase `stockfish` and the Windows Documents folder in WSL). Override with `-Engine` or `--engine` if needed.
4. For Aina, configure its [companion credential and host token](REMOTE_AINA.md#authentication-and-first-connection). Run one chess-capable companion beside the host, in the same Linux/WSL environment. Launchers enable the local API but do not start the companion.

## Each game from PowerShell and WSL

Stop the previous host with Ctrl+C in its terminal. For a fresh physical game, place all 32 pieces on starting squares, empty parking, and keep the independent power cutoff within reach.

From the repository root:

```powershell
.\Play-ChessTable.ps1                     # Physical
.\Start-ChessTable.ps1 -EnableAina        # Simulation
```

Each command creates and prints a fresh checkpoint path. The physical launcher discovers and attaches the Uno. Use `-UsbBusId <BUSID>` if several boards match, or `-Port /dev/serial/by-id/...` for an attached device. `-DryRun` checks settings without attaching USB or starting the host.

## Each game on native Linux

These commands also work inside WSL:

```bash
./Play-ChessTable.sh                     # Physical
./Start-ChessTable.sh --enable-aina      # Simulation
```

Use `--port /dev/serial/by-id/...` for an ambiguous device, `--dry-run` to check settings, or `./Start-ChessTable.sh --help` for options. Native Linux accesses USB directly.

For local-only play, use the full `Start` launcher without `-EnableAina` / `--enable-aina`; add `-Mode Physical` / `--mode physical` for hardware.

## Start play and check Aina

Keep the launcher terminal open. For local play, open `http://localhost:8765/`, choose a mode, and press **Start game**. For Aina control, leave the GUI game unstarted and use:

| Request | Effect |
| --- | --- |
| “start chess table” | Launch a fresh idle physical host. |
| “start chess table in simulation mode” | Launch a fresh idle simulated host. |
| “start a chess game against Stockfish with me as white” | Bind and start play as White. Use “black” for Black. |
| “start a chess game in manual move mode” | Bind and start play with both sides entered manually. |
| “chess table status” | Read expected position, turn, game ID, and phase. |
| “knight to c3” or “D2, D4” | Resolve and submit a legal move in the current position. |

Launching a host and starting a game are separate operations. Starting physical play can reference or home the carriage. Status describes expected occupancy; it cannot verify the actual pieces. See [Aina troubleshooting](REMOTE_AINA.md#credential-changes-and-recovery) for connection or authentication failures.

## Resume the same game

Keep the printed checkpoint and its adjacent serial log. Stop the old host, then reopen the **exact file in its original physical/simulation mode**. Relative paths are resolved from `computer/`:

```powershell
.\Play-ChessTable.ps1 -Resume -State 'runs\chess-physical-<saved-name>.json'
.\Start-ChessTable.ps1 -Mode Simulation -EnableAina -Resume -State 'runs\chess-sim-<saved-name>.json'
```

```bash
./Play-ChessTable.sh --resume 'runs/chess-physical-<saved-name>.json'
./Start-ChessTable.sh --mode simulation --enable-aina --resume 'runs/chess-sim-<saved-name>.json'
```

For Aina, resume the matching game binding with its saved mode and human color. Restarting the process alone does not resume play.

**Never replay an uncertain carry.** Inspect the actual piece, endpoints, neighbors, and parking, then reconcile through the local GUI before another move. Choose a recovery outcome only after verifying or restoring the corresponding physical state. `DONE CARRY` advances expected occupancy but cannot prove delivery. Follow the [GUI recovery controls](GUI_GAME.md#pause-recover-and-reset) or [physical recovery runbook](STAGE4_TEST_GAME.md#interruption-recovery-and-evidence).

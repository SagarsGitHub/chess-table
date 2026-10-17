# Chess Table

Python host and Arduino Uno firmware for an automated chessboard with 32 perimeter parking slots. The host owns chess rules, Stockfish, routing, and checkpoints; the Uno controls motors and the magnet.

**The board has no piece sensors.** Enter moves in software and let the table carry the pieces. Inspect and reconcile any interrupted or uncertain carry before another move. Preserve checkpoints and serial logs in `computer/runs/`.

## Start from PowerShell

Complete the [one-time setup](docs/QUICK_START.md#once-per-machine), then run from the repository root:

```powershell
.\Play-ChessTable.ps1                     # Physical host with Aina enabled
.\Start-ChessTable.ps1 -EnableAina        # Simulation with Aina enabled
```

## Start on Linux or in WSL

```bash
./Play-ChessTable.sh                     # Physical host with Aina enabled
./Start-ChessTable.sh --enable-aina      # Simulation with Aina enabled
```

Keep the terminal open. Follow the [quick start](docs/QUICK_START.md) to start GUI or Aina play and resume the exact saved checkpoint.

## Status

See the [roadmap](docs/ROADMAP.md) for accepted physical tests and remaining work.

For Linux startup and authenticated Aina control, use `Start-ChessTable.sh`
and the [Aina setup guide](docs/REMOTE_AINA.md). The launcher starts the host;
game play remains stopped until started through the GUI or Aina.

## Repository map

| Guide | Purpose |
| --- | --- |
| [Quick start](docs/QUICK_START.md) | Setup, launch, game commands, and recovery. |
| [GUI runbook](docs/GUI_GAME.md) | Local play, inspection, and board reset. |
| [Aina integration](docs/REMOTE_AINA.md) | Credentials, API contract, and troubleshooting. |
| [PocketMage roadmap](docs/POCKETMAGE_ROADMAP.md) / [control guide](docs/POCKETMAGE_CONTROL.md) | Standalone chess app, trusted-LAN API and device-agent contract. |
| [Host](computer/README.md) / [firmware](firmware/README.md) | Development entry points and tests. |
| [Architecture](docs/ARCHITECTURE.md) / [protocol](docs/PROTOCOL.md) | Ownership and wire contract. |
| [Routing](docs/ROUTING.md) | Geometry and clearance limits. |
| [Physical test runbook](docs/STAGE4_TEST_GAME.md) | USB, CLI tests, and interrupted-motion recovery. |
| [Provenance](docs/PROVENANCE.md) | Original project and repository history. |

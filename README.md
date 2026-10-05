# Chess Table

Python host and Arduino Uno firmware for an automated chessboard with 32 perimeter parking slots for captured pieces. The host owns chess rules, Stockfish, routes, and checkpoints; the Uno executes bounded motor and magnet commands. The board has no piece sensors. Human moves are entered in the GUI or CLI, and an interrupted carry requires physical inspection before play resumes.

## Status

A physical GUI game reached a 126-ply draw with 20 parked pieces. Its board reset returned the saved state to 32 starting pieces and empty parking after a motion fault, reboot, and reconciliation. The user reports that the subsequent slower carry profiles work on the board. A repeatable fault-free loaded game, special-move coverage, and the reported magnet-release reset remain open acceptance work. See the [roadmap and evidence](docs/ROADMAP.md).

## Start in WSL

Use the Coding checkout in Ubuntu WSL 2:

```bash
cd /mnt/c/Users/sagar/Documents/Coding/chess-table/computer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For the browser game, follow the [GUI runbook](docs/GUI_GAME.md). For CLI smoke tests, Stockfish self-play, USB attachment, and recovery, use the [physical test runbook](docs/STAGE4_TEST_GAME.md). A new game needs a fresh checkpoint filename; `--resume` uses an existing one. The host performs `HOME` or `REF`, reads live `GEOM`, and sends `MAP`; it does not need a separate Serial Monitor setup step.

## Repository map

| Path | Purpose |
| --- | --- |
| [`computer/`](computer/README.md) | Game state, routing, Stockfish, GUI, serial transport, and host tests. |
| [`firmware/`](firmware/README.md) | PlatformIO Uno code, pinout, calibration, and motor simulation. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Host and Uno responsibilities and recovery boundary. |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | Framed serial commands and occupancy encoding. |
| [`docs/ROUTING.md`](docs/ROUTING.md) | Route geometry and clearance behavior. |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | Milestones, physical evidence, and remaining acceptance work. |
| [`legacy/chess_automated/`](legacy/chess_automated/) | Original reference project; see [provenance](docs/PROVENANCE.md). |

The repository retains the history of [SagarsGitHub/chess_automated](https://github.com/SagarsGitHub/chess_automated) and the selected workspace firmware snapshot. The original client's serial protocol differs from the integrated host and Uno protocol; new work belongs in `computer/` and `firmware/`.

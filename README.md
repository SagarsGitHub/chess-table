# Chess Table

This private repository brings together a Python chess controller and Arduino Uno motion firmware for an automated physical chessboard with perimeter parking for captured pieces. It starts from [SagarsGitHub/chess_automated](https://github.com/SagarsGitHub/chess_automated) and the latest firmware snapshot in the local chess-table workspace. The reference project's Git history is retained.

## Current state

The repository is an integration foundation, **not yet a functioning full chess game**. The computer side now checks legal moves, assigns captured pieces to 32 perimeter slots, plans general carry routes, encodes the compact occupancy map, and uses a request-ID serial link. The Uno firmware accepts bounded square-to-square and square-to-parking carries with map and route checks. Software tests and a compiled-Uno simulator exercise that path. The board has no physical piece sensing, and a reported controller reset when the electromagnet releases remains unresolved. Loaded end-to-end testing and game orchestration are still pending.

The reference Python project contains game clients and uses `python-chess`, but its hardware implementation is incomplete and its 9600-baud serial command does not match the new Uno's 115200-baud framed protocol. The original software is kept in `legacy/` for traceability; new integration work belongs in `computer/` and `firmware/`.

## Repository map

| Path | Purpose |
| --- | --- |
| `computer/` | Chess state and parking plans, measured-geometry routing, wire encoding, and serial transport. |
| `firmware/` | PlatformIO Uno firmware and compiled-device simulator tests. |
| `docs/ARCHITECTURE.md` | Responsibility and state boundaries. |
| `docs/ROADMAP.md` | Milestones, acceptance criteria, and hardware gates. |
| `docs/PROTOCOL.md` | Version 1 serial frames, occupancy bitmap, and carry command. |
| `docs/ROUTING.md` | Route topology, measured clearance inputs, and test scope. |
| `docs/PROVENANCE.md` | Source revisions, license, and known gaps. |
| `legacy/chess_automated/` | Unmodified reference Python application and its MIT license, moved intact from the source repository. |

See [architecture](docs/ARCHITECTURE.md) and [roadmap](docs/ROADMAP.md) before extending either side. The [firmware README](firmware/README.md) contains its command set, pinout, and calibration procedure. Software simulations do not confirm that the physical magnet picked up or delivered a piece.

## Development starting points

The computer-side modules can be tested without moving hardware: from `computer/`, install `requirements.txt` and run `python -m unittest discover -s tests -v`. The firmware is a PlatformIO project targeting Uno; from `firmware/`, run `pio run`, then follow the [simulator notes](firmware/tests/README.md). Stage 1 electrical diagnosis is deferred at the user's request. Loaded game tests wait for that reset investigation and the physical acceptance gates in the roadmap.

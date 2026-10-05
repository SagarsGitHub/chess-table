# Chess Table

This private repository brings together a Python chess controller and Arduino Uno motion firmware for an automated physical chessboard with perimeter parking for captured pieces. It starts from [SagarsGitHub/chess_automated](https://github.com/SagarsGitHub/chess_automated) and the latest firmware snapshot in the local chess-table workspace. The reference project's Git history is retained.

## Current state

The repository is an integration foundation, **not yet a functioning full chess game**. The imported Uno firmware homes the XY carriage, stores measured geometry, carries knights, and parks or retrieves a piece at an adjacent edge square. It has 32 perimeter parking coordinates and a commanded occupancy bitmap. It cannot route an arbitrary capture from the board to a parking slot. It has no physical piece sensing. A reported controller reset when the electromagnet releases remains unresolved and is a prerequisite for loaded end-to-end testing.

The reference Python project contains game clients and uses `python-chess`, but its hardware implementation is incomplete and its 9600-baud serial command does not match the imported Uno firmware's 115200-baud manual command set. The original software is kept in `legacy/` for traceability; new integration work belongs in `computer/` and `firmware/`.

## Repository map

| Path | Purpose |
| --- | --- |
| `computer/` | New computer-side chess state, parking planning, and, later, device transport and game UI. |
| `firmware/` | Imported PlatformIO Uno firmware and its existing tests. |
| `docs/ARCHITECTURE.md` | Responsibility and state boundaries. |
| `docs/ROADMAP.md` | Milestones, acceptance criteria, and hardware gates. |
| `docs/PROTOCOL.md` | Proposed serial protocol and migration from the current manual commands. |
| `docs/PROVENANCE.md` | Source revisions, license, and known gaps. |
| `legacy/chess_automated/` | Unmodified reference Python application and its MIT license, moved intact from the source repository. |

See [architecture](docs/ARCHITECTURE.md) and [roadmap](docs/ROADMAP.md) before extending either side. The [firmware README](firmware/README.md) contains its current command set, pinout, calibration procedure, and fixture tests. Software simulations do not confirm that the physical magnet picked up or delivered a piece.

## Development starting points

The computer-side planner can be developed and tested without moving hardware. See `computer/README.md` for its commands and limitations. The firmware is a PlatformIO project (`cd firmware; pio run`) targeting Uno; its [test notes](firmware/tests/README.md) describe the existing simulator. Do not start a loaded game test until the electrical reset is resolved and the physical acceptance gates in the roadmap have passed.

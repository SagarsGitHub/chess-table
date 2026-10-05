# Roadmap

The target is a chess table that can complete a full game, park captured pieces around the perimeter, and recover predictably from interrupted motion. A stage is complete only when its acceptance evidence is recorded in this repository.

**Current sequence:** Stage 1 electrical diagnosis is deferred at the user's request. Stage 2 and the software/simulator portion of stage 3 are implemented. The user reports that the empty-board physical test passed and supplied `GEOM X=249.7350 Y=251.9850 OX=-0.1325 OY=0.9925 P=31.2500 I=2.0000 PPM=133.3333`. Stage 4 now has a checkpointed supervised game runner, Stockfish integration, and a five-ply software smoke test; its physical test game and full-game acceptance remain open. The host test suite passes under Ubuntu WSL 2 from the Coding checkout. During actual board movement, a limit closure must persist for 3 ms to stop motion. Switch activity at idle and between movement segments is ignored. Homing uses the first raw edge and confirms it after stopping.

| Stage | Work | Acceptance evidence |
| --- | --- | --- |
| 0. Repository foundation | Retain reference history and MIT notice; import the chosen firmware snapshot; document ownership; build a pure computer-side move/parking planner. | New private repository contains both sources, provenance, architecture, roadmap, and planner unit tests. |
| 1. Hardware reliability | Identify and fix the magnet-release reset; verify relay supply, magnet suppression, grounding, and limits with the actual parts. Capture serial logs and wiring changes. | Homing and repeat loaded carries through the historical fixture positions finish without a boot/reset, unexpected limit, or lost piece, with logs recorded. |
| 2. Reliable serial link | Add a version/capability handshake, request IDs, ACK/completion/fault messages, bounded timeouts, stop behavior, and restart detection. Keep one-byte bench stop. | Host and simulated Uno pass duplicate, malformed, busy, disconnect, reset, and stop cases. A timeout never silently commits a move. |
| 3. General motion and parking | Implement square-to-square and arbitrary square-to-perimeter carry using route segments and known occupancy on the Uno. Model measured clearances and motion limits; reject a route that exceeds the compact 12-waypoint format. | Software route and simulator matrices pass. Later physical route runs cover all board regions and all 32 parking slots with repeatable pickup, delivery, and limit safety. Captures remove the victim first. |
| 4. Game orchestration | Integrate legal-move plans with transport. Handle capture, en passant, castling, and promotion using the original pawn as the physical stand-in for the chosen legal piece. Persist game and parking inventory. Start with manual UCI input for human moves. | A complete local game can be played against a computer engine without running out of parking or losing board/slot agreement. A promoted pawn is tested through promotion, a later move, capture into parking, and checkpoint recovery. Fault recovery is exercised mid-capture. |
| 5. Sensing and polish | Add or integrate physical piece detection if desired, then UI/clock/online features after the local game is stable. | Detected moves and mismatches are verified against legal state; regression and physical game logs are saved. |

Stage 1 is tracked in [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1). Stage 3 loaded hardware acceptance is tracked in [issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

## Execution workspace

Run the computer program from the checkout at `C:\Users\sagar\Documents\Coding\chess-table`, which Ubuntu WSL 2 sees as `/mnt/c/Users/sagar/Documents/Coding/chess-table`. Use its Linux `computer/.venv/bin/python` environment and the commands in [STAGE4_TEST_GAME.md](STAGE4_TEST_GAME.md). Each `--new` simulation or physical game needs a fresh checkpoint filename; `--resume` continues that checkpoint.

Before physical play from WSL, attach the Uno USB device to WSL and select its detected `/dev/ttyACM*` or `/dev/ttyUSB*` port. The current Windows `COM3` config value cannot be passed as a WSL serial device. The Windows Stockfish download can be run from WSL using its `/mnt/c/Users/sagar/Documents/stockfish/...exe` path. The runner itself performs `HOME` or `REF`, reads live `GEOM`, and sends `MAP`. As of 2026-10-05, USB passthrough is not configured on this machine, so the physical stage 4 run remains open.

## Immediate next actions

1. Run the supervised five-ply simulation from the WSL Coding checkout, attach the Uno to WSL, find its Linux serial port, then run the five-ply physical test in [STAGE4_TEST_GAME.md](STAGE4_TEST_GAME.md) and record each confirmation. The user reports 18 mm moving and standing diameters; path/centering error remains to be measured, so 0 mm margin is provisional.
2. When stage 1 resumes, record the relay module, electromagnet supply/current, wiring, and a serial log around the reported release reset. Resolve it before claiming loaded reliability.
3. Expand loaded routes across all board regions and 32 parking slots, then complete a local game against Stockfish and exercise mid-capture fault recovery.

## Fixed promotion rule

There are no spare queens, rooks, bishops, or knights for promotion. The pawn remains the physical piece on the board while the rules engine and Stockfish use the selected promoted type. Later moves carry that same pawn; capture parks it as a pawn. The game runner does not swap in a parked piece or request a manual replacement. Record both the legal and physical identities in game checkpoints and operator-facing state.

## Open design decisions

- Confirm the actual electrical fix. The compact framed implementation now fits the user-selected Uno by retiring older diagnostics.
- Decide whether the first usable version relies on manual move entry/confirmation or new sensors. The imported hardware has no piece detection.
- Confirm accessible slot spacing and collision clearance with real pieces, including occupied neighbors and parked pieces.

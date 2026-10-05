# Roadmap

The target is a chess table that can complete a full game, park captured pieces around the perimeter, and recover predictably from interrupted motion. A stage is complete only when its acceptance evidence is recorded in this repository.

**Current sequence:** Stage 1 electrical diagnosis is deferred at the user's request. Stage 2 and the software/simulator portion of stage 3 are implemented. The user reports that the empty-board physical test passed and supplied `GEOM X=249.7350 Y=251.9850 OX=-0.1325 OY=0.9925 P=31.2500 I=2.0000 PPM=133.3333`. Stage 4 has a checkpointed game runner and Stockfish integration. A physical game reached 16 committed plies and four occupied parking slots on 2026-10-05; full-game acceptance remains open. A tight-route fallback and automatic advancement after `DONE CARRY` are implemented in host software; the next work is a loaded knight trial and completion of a physical game. The host test suite passes under Ubuntu WSL 2 from the Coding checkout. During actual board movement, a limit closure must persist for 3 ms to stop motion. Switch activity at idle and between movement segments is ignored. Homing uses the first raw edge and confirms it after stopping. This table will use manual UCI entry and checkpointed expected occupancy throughout its life; physical piece sensing is out of scope. Faults and uncertain outcomes require physical reconciliation.

| Stage | Work | Acceptance evidence |
| --- | --- | --- |
| 0. Repository foundation | Retain reference history and MIT notice; import the chosen firmware snapshot; document ownership; build a pure computer-side move/parking planner. | New private repository contains both sources, provenance, architecture, roadmap, and planner unit tests. |
| 1. Hardware reliability | Identify and fix the magnet-release reset; verify relay supply, magnet suppression, grounding, and limits with the actual parts. Capture serial logs and wiring changes. | Homing and repeat loaded carries through the historical fixture positions finish without a boot/reset, unexpected limit, or lost piece, with logs recorded. |
| 2. Reliable serial link | Add a version/capability handshake, request IDs, ACK/completion/fault messages, bounded timeouts, stop behavior, and restart detection. Keep one-byte bench stop. | Host and simulated Uno pass duplicate, malformed, busy, disconnect, reset, and stop cases. A timeout never silently commits a move. |
| 3. General motion and parking | Implement square-to-square and arbitrary square-to-perimeter carry using route segments and known occupancy on the Uno. Model measured clearances and motion limits; reject a route that exceeds the compact 12-waypoint format. | Software route and simulator matrices pass. Later physical route runs cover all board regions and all 32 parking slots with repeatable pickup, delivery, and limit safety. Captures remove the victim first. |
| 4. Complete physical game | Integrate legal-move plans with transport. Handle capture, en passant, castling, and promotion using the original pawn as the physical stand-in for the chosen legal piece. Persist game and parking inventory. Keep manual UCI entry; advance transfers automatically after matched `DONE CARRY`, with optional `--supervised` prompts. Permit a center-safe bounded route when the footprint clearance model rejects a route that works on this table. Reconcile physical state after faults or uncertain motion. | A complete local game can be played against Stockfish without running out of parking or losing board/slot agreement. A promoted pawn is tested through promotion, a later move, capture into parking, and checkpoint recovery. Fault recovery is exercised mid-capture. Save the checkpoint, serial log, and operator observations. |

Stage 1 is tracked in [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1). Stage 3 loaded hardware acceptance is tracked in [issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

## Execution workspace

Run the computer program from the checkout at `C:\Users\sagar\Documents\Coding\chess-table`, which Ubuntu WSL 2 sees as `/mnt/c/Users/sagar/Documents/Coding/chess-table`. Use its Linux `computer/.venv/bin/python` environment and the commands in [STAGE4_TEST_GAME.md](STAGE4_TEST_GAME.md). Each `--new` simulation or physical game needs a fresh checkpoint filename; `--resume` continues that checkpoint.

Before physical play from WSL, attach the Uno USB device to WSL and select its detected `/dev/ttyACM*` or `/dev/ttyUSB*` port. The current Windows `COM3` config value cannot be passed as a WSL serial device. The Windows Stockfish download can be run from WSL using its `/mnt/c/Users/sagar/Documents/stockfish/...exe` path. The runner itself performs `HOME` or `REF`, reads live `GEOM`, and sends `MAP`. The 2026-10-05 physical run used this Coding checkout; use a fresh checkpoint for a new game or `--resume` for the saved one.

## Physical progress recorded 2026-10-05

The ignored checkpoint `computer/runs/physical-smoke-20261005-013125.json` records 16 committed plies through `h7h5`, no pending action, and four occupied parking slots. Its paired `.serial.jsonl` transcript records 20 `CARRY` requests with 20 `DONE CARRY` responses and no controller `ERR`. The transcript also contains four `!BOOT` announcements across connection sessions; the record alone does not establish a reset-free loaded run. The checkpoint has no knight move, so the subsequently reported knight clearance rejection is not assigned a specific UCI move. From a copy of this checkpoint, strict preflight rejected all four legal white knight moves, while the bounded tight-route fallback preflighted each. A simulated `g1f3` continuation committed as ply 17 with `CLEARANCE OVERRIDE`; the actual physical checkpoint was not changed. The user reports that the test game worked up to the route rejection. This is physical progress plus a host simulation, not complete-game or special-move acceptance.

A separate WSL Stockfish self-play simulation completed at 141 plies as a draw by insufficient material. It used the simulated controller and tight-route fallback, with 29 captures and 21 clearance overrides, and had no route or controller simulation failures. It did not include a promotion. This verifies that the software can reach a game ending with the current routing policy; loaded carries, physical clearance, promotion, and recovery remain open acceptance work.

## Immediate next actions

1. Try one loaded knight transfer from the saved position using the tight-route fallback, keeping travel bounds, occupied-center avoidance, the waypoint limit, active limits, and fault/recovery handling. Record the selected route, `ACK`/`DONE`, and observed piece positions. Normal play advances automatically after matched `DONE CARRY`; `--supervised` remains available for a deliberate test. The user reports 18 mm moving and standing diameters and practical success through some tight horizontal paths; path/centering error remains to be measured, so 0 mm margin is provisional.
2. When stage 1 resumes, record the relay module, electromagnet supply/current, wiring, and a serial log around the reported release reset. Resolve it before claiming loaded reliability.
3. Resume or start a physical game against Stockfish from the WSL Coding checkout. Record tight-route use and observed piece outcomes. Complete a game, exercise promotion and mid-capture fault recovery, and expand loaded routes across all board regions and 32 parking slots.

## Fixed promotion rule

There are no spare queens, rooks, bishops, or knights for promotion. The pawn remains the physical piece on the board while the rules engine and Stockfish use the selected promoted type. Later moves carry that same pawn; capture parks it as a pawn. The game runner does not swap in a parked piece or request a manual replacement. Record both the legal and physical identities in game checkpoints and operator-facing state.

## Open design decisions

- Confirm the actual electrical fix. The compact framed implementation now fits the user-selected Uno by retiring older diagnostics.
- Confirm accessible slot spacing and collision clearance with real pieces, including occupied neighbors and parked pieces.

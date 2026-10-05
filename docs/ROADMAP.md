# Roadmap

The target is a chess table that can complete a full game, park captured pieces around the perimeter, and recover predictably from interrupted motion. A stage is complete only when its acceptance evidence is recorded in this repository.

**Current sequence:** Stage 1 electrical diagnosis is deferred at the user's request. Stage 2 and the software/simulator portion of stage 3 are implemented: 59 host tests pass, the Uno builds at 28,698 flash and 1,005 static RAM bytes, and compiled-firmware motion/recovery cases pass. Loaded physical acceptance remains open. During actual board movement, a limit closure must persist for 3 ms to stop motion. Switch activity at idle and between movement segments is ignored. Homing uses the first raw edge and confirms it after stopping.

| Stage | Work | Acceptance evidence |
| --- | --- | --- |
| 0. Repository foundation | Retain reference history and MIT notice; import the chosen firmware snapshot; document ownership; build a pure computer-side move/parking planner. | New private repository contains both sources, provenance, architecture, roadmap, and planner unit tests. |
| 1. Hardware reliability | Identify and fix the magnet-release reset; verify relay supply, magnet suppression, grounding, and limits with the actual parts. Capture serial logs and wiring changes. | Homing and repeat loaded carries through the historical fixture positions finish without a boot/reset, unexpected limit, or lost piece, with logs recorded. |
| 2. Reliable serial link | Add a version/capability handshake, request IDs, ACK/completion/fault messages, bounded timeouts, stop behavior, and restart detection. Keep one-byte bench stop. | Host and simulated Uno pass duplicate, malformed, busy, disconnect, reset, and stop cases. A timeout never silently commits a move. |
| 3. General motion and parking | Implement square-to-square and arbitrary square-to-perimeter carry using route segments and known occupancy on the Uno. Model measured clearances and motion limits; reject a route that exceeds the compact 12-waypoint format. | Software route and simulator matrices pass. Later physical route runs cover all board regions and all 32 parking slots with repeatable pickup, delivery, and limit safety. Captures remove the victim first. |
| 4. Game orchestration | Integrate legal-move plans with transport. Handle capture, en passant, castling, and promotion through a documented physical exchange policy. Persist game and parking inventory. Start with manual UCI input for human moves. | A complete local game can be played against a computer engine without running out of parking or losing board/slot agreement. Fault recovery is exercised mid-capture. |
| 5. Sensing and polish | Add or integrate physical piece detection if desired, then UI/clock/online features after the local game is stable. | Detected moves and mismatches are verified against legal state; regression and physical game logs are saved. |

Stage 1 is tracked in [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1). Stage 3 loaded hardware acceptance is tracked in [issue #2](https://github.com/SagarsGitHub/chess-table/issues/2).

## Immediate next actions

1. Record the actual `GEOM` reply, moving/standing piece diameters, and magnet path error before calling a route physically clear.
2. When stage 1 resumes, record the relay module, electromagnet supply/current, wiring, and a serial log around the reported release reset. Resolve it before loaded route runs.
3. Test loaded routes across all board regions and 32 parking slots, then connect confirmed transfers to the full game orchestrator.

## Open design decisions

- Confirm the actual electrical fix. The compact framed implementation now fits the user-selected Uno by retiring older diagnostics.
- Choose the physical promotion inventory and human workflow: spare promoted pieces, manual exchange, or a separate rack. The logical planner must not imply that a pawn transforms physically.
- Decide whether the first usable version relies on manual move entry/confirmation or new sensors. The imported hardware has no piece detection.
- Confirm accessible slot spacing and collision clearance with real pieces, including occupied neighbors and parked pieces.

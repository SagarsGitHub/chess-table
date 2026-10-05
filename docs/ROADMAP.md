# Roadmap

The target is a chess table that can complete a full game, park captured pieces around the perimeter, and recover predictably from interrupted motion. Milestones are ordered by prerequisites. A stage is complete only when its acceptance evidence is recorded in this repository.

| Stage | Work | Acceptance evidence |
| --- | --- | --- |
| 0. Repository foundation | Retain reference history and MIT notice; import the chosen firmware snapshot; document ownership; build a pure computer-side move/parking planner. | New private repository contains both sources, provenance, architecture, roadmap, and planner unit tests. |
| 1. Hardware reliability | Identify and fix the magnet-release reset; verify relay supply, magnet suppression, grounding, and limits with the actual parts. Capture serial logs and wiring changes. | Homing and all 36 firmware fixture legs finish without a boot/reset, unexpected limit, or lost piece, with repeat runs recorded. |
| 2. Reliable serial link | Add a version/capability handshake, request IDs, ACK/completion/fault messages, bounded timeouts, stop behavior, and restart detection. Preserve a temporary manual diagnostic mode. | Host and simulated Uno pass duplicate, malformed, busy, disconnect, reset, and stop cases. A timeout never silently commits a move. |
| 3. General motion and parking | Implement square-to-square and arbitrary square-to-perimeter carry using route segments and known occupancy. Measure clearances and motion limits; assess Uno flash/RAM headroom before choosing the final route encoding or a controller upgrade. | Physical route matrix covers all board regions and all 32 parking slots with repeatable pickup, delivery, and limit safety. Captures remove the victim first. |
| 4. Game orchestration | Integrate legal-move plans with transport. Handle capture, en passant, castling, and promotion through a documented physical exchange policy. Persist game and parking inventory. Start with manual UCI input for human moves. | A complete local game can be played against a computer engine without running out of parking or losing board/slot agreement. Fault recovery is exercised mid-capture. |
| 5. Sensing and polish | Add or integrate physical piece detection if desired, then UI/clock/online features after the local game is stable. | Detected moves and mismatches are verified against legal state; regression and physical game logs are saved. |

## Immediate next actions

1. Record the relay module model, electromagnet voltage/current, wiring, and a serial log around the reported release reset. Resolve this before loaded route development.
2. Run the firmware's existing simulator and build in a reproducible toolchain; record actual Uno flash/RAM use.
3. Specify and implement protocol version 1 on both sides, starting with handshake/status/reference/stop and a simulator, then add route execution once the memory budget is known.
4. Develop the host route planner with an explicit clearance model for the 31.25 mm pitch and real piece/magnet diameters; validate every route on hardware.

## Open design decisions

- Confirm the actual electrical fix and whether the Uno can fit general route execution plus a framed protocol. If it cannot, use a larger controller or a smaller command/state design.
- Choose the physical promotion inventory and human workflow: spare promoted pieces, manual exchange, or a separate rack. The logical planner must not imply that a pawn transforms physically.
- Decide whether the first usable version relies on manual move entry/confirmation or new sensors. The imported hardware has no piece detection.
- Confirm accessible slot spacing and collision clearance with real pieces, including occupied neighbors and parked pieces.

# Architecture and ownership

## Guiding boundary

The computer owns chess decisions and expected physical state. The Arduino
executes one bounded motor operation. The board has no piece sensors; an
ID-matched completion updates expected occupancy, not observed piece positions.
Uncertain outcomes pause play for physical inspection and reconciliation.

| Concern | Computer program | Arduino firmware |
| --- | --- | --- |
| Rules and inventory | Authoritative legal board, piece identities, parking selection and atomic checkpoints. | Commanded occupancy for motion guards; no rules or piece identity. |
| Motion | Preflight complete moves/resets using geometry and expected occupancy. | Motors, magnet, route validation, measured travel bounds and active limits. |
| Calibration | Require a referenced carriage and compatible geometry/capabilities. | Home/reference, measure span, map coordinates and save geometry in EEPROM. |
| Serial transport | Frame commands, assign IDs, negotiate capabilities, await completion, log and time out. | Validate commands, reject busy requests, report ID-matched progress/completion/faults, handle STOP. |
| Player and engine | Human input, legal expression resolution, Stockfish and optional supervision. | No engine or user account logic. |
| Aina | Authenticate the companion, persist one source binding and mode/color, save resolutions/receipts, fence moves by game and position. | No network, account or Aina logic. |
| Recovery | Inspect/reconcile expected inventory before resuming uncertain motion. | Report controller state; cannot verify pickup or delivery. |

The [module map](../computer/README.md#code-map) lists implementation ownership.
`physical.py` and `reset_plan.py` plan without sending motor commands;
`game.py` owns the runner, persists prepared plans and executes motion;
`game_preflight.py` supplies complete move/reset planning and
`game_recovery.py` supplies inspected reconciliation and commits. Both method
groups operate on that same runner state and store.

`gui.py` starts the listeners. `gui_controller.py` creates the single session,
runner, locks and serial owner; `gui_worker.py` and `gui_remote.py` group its
worker loops and Aina request handling. These internal mixins have no separate
constructor or state, so queueing, STOP and game-to-reset handoff still share
one controller. `host_runtime.py` shares connection setup and engine/geometry
helpers; `gui_snapshot.py` isolates synchronized checkpoint reads and browser
presentation. Aina's separate loopback adapter uses typed requests and session
policy in `remote_contract.py` and `remote_session.py`; every move still uses
the same `GameRunner`. See the [local API contract](REMOTE_AINA.md#local-api-contract).

## Operation order

1. Validate the move and any remote identity/position fence.
2. Preflight every action and route, including captures and temporary staging.
3. Save the pending plan and remote receipt before motion.
4. Execute one command at a time; checkpoint expected occupancy after matched
   `DONE CARRY`/`CARRY_OFFSET`, or the within-square position after `DONE OFFSET`.
5. Commit the legal move and inventory together after all actions finish.

Normal play advances without per-transfer prompts; `--supervised` adds them.
Timeout, STOP, controller reset or fault blocks automatic continuation.
Routes must obey measured bounds and the 12-waypoint cap. Tight-route fallback
and crowded-horizontal cost cannot establish physical clearance or override
modeled violations. See [ROUTING.md](ROUTING.md) for algorithms and
[PROTOCOL.md](PROTOCOL.md) for frames, firmware validation and release extension.

## Parking geometry

The host chooses among 32 perimeter parking slots, each holding one piece.
Firmware's occupancy bitmap covers 64 squares and 32 slots without sensing or
identity. Slot numbering and coordinate mapping are in [PROTOCOL.md](PROTOCOL.md).

## Promotion rule

A promoted pawn remains the physical pawn while chess rules and Stockfish use
its selected promoted type. Checkpoints preserve both identities. Capture parks
that pawn; promotion does not exchange pieces or reuse a parked replacement.

## Recovery contract

EEPROM retains geometry, not live carriage position or game inventory. Preserve
the checkpoint and serial log after interrupted motion. Safely release the
magnet, reference the carriage, inspect affected pieces and parking, reconcile
the saved action and restore matching host/firmware occupancy before resuming.
For offsets, verify the piece's square center or saved corner. Never substitute
a fresh game or automatically replay an uncertain carry.

Use the [GUI recovery guide](GUI_GAME.md) or [physical runbook](STAGE4_TEST_GAME.md)
for operator steps. Physical acceptance remains separate from software tests;
see [ROADMAP.md](ROADMAP.md).

# Architecture and ownership

The computer owns chess decisions and expected physical state. The Uno executes one bounded motor operation. The board has no piece sensors: matched completion updates expected occupancy, and an uncertain outcome requires physical inspection and reconciliation.

| Concern | Computer host | Uno firmware |
| --- | --- | --- |
| Rules and inventory | Legal board, piece identities, parking choice, atomic checkpoints | Declared occupancy guards; no rules or piece identity |
| Motion | Preflight every move/reset action using geometry and expected occupancy | Motors, magnet, route validation, measured bounds and active limits |
| Calibration | Require referenced carriage and compatible geometry/capabilities | Home/reference, measure span, map coordinates, EEPROM geometry |
| Transport | Frame IDs, negotiate capabilities, log, match ACK/completion/faults, time out | Validate commands, reject busy requests, report ID-matched outcomes, handle STOP |
| Players and remote control | Human input, Stockfish, authentication, source bindings, position fences and receipts | No engine, network or account logic |
| Recovery | Inspect/reconcile inventory and saved actions before continuing | Report controller state; cannot verify pickup or delivery |

## Host structure

The [module map](../computer/README.md#code-map) lists implementation ownership. `physical.py` and `reset_plan.py` plan without motor commands. `game.py` owns the runner, durable plans and execution; `game_preflight.py` and `game_recovery.py` operate on that same runner and store.

`gui.py` starts listeners; `gui_controller.py` owns the single session, locks and serial connection. Its worker and remote mixins share that state. `host_runtime.py` supplies connection/engine/geometry helpers; `gui_snapshot.py` reads synchronized checkpoints for presentation. Aina validation/session policy lives in `remote_contract.py` and `remote_session.py`; the [authenticated API](REMOTE_AINA.md#local-api-contract) still submits through the same controller and runner.

## Operation order

1. Validate the legal move and any remote identity/position fence.
2. Preflight every action, including captures, staging, offsets and release extensions.
3. Save the pending plan and remote receipt before motion.
4. Execute one command at a time; checkpoint occupancy after matched `DONE CARRY`/`CARRY_OFFSET`, or within-square position after `DONE OFFSET`.
5. Commit the legal move and inventory together after every action finishes.

Normal play advances without per-transfer prompts; `--supervised` adds them. Timeout, STOP, controller reset or fault blocks automatic continuation. Measured bounds and the 12-waypoint cap always apply. Routing costs and tight-route fallback do not establish physical clearance. See [routing](ROUTING.md) and the [wire contract](PROTOCOL.md).

## Inventory

The host selects among 32 single-piece perimeter slots. The firmware bitmap covers 64 squares and 32 slots without sensing or identity; [PROTOCOL.md](PROTOCOL.md) defines their mapping. Captures park the victim before carrying the attacker, updating expected occupancy between transfers.

A promoted pawn remains the physical pawn while chess rules and Stockfish use its selected promoted type. Checkpoints preserve both identities. Capture parks that pawn; promotion never exchanges it for a parked replacement.

## Recovery contract

EEPROM retains geometry, not live carriage position or inventory. Preserve the checkpoint and serial log. Safely release the magnet, inspect affected pieces and parking, reconcile the saved action, then restore carriage reference and matching host/firmware occupancy before resuming. For offsets, verify center or saved corner. Never automatically replay an uncertain carry or substitute a fresh game.

Use the [operator recovery controls](QUICK_START.md#pause-recover-and-reset). Physical acceptance is recorded separately from software tests in the [roadmap](ROADMAP.md).

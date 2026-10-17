# Computer host

`chess_table_host` owns chess rules, expected physical inventory, routing,
Stockfish, the GUI, and checkpointed serial control. The Uno executes bounded
motion. The board has no piece sensors; completion updates expected occupancy.
Uncertain motion requires inspection and reconciliation before another move.

## Code map

| Module | Responsibility |
| --- | --- |
| `planner.py` | Legal moves, capture order, parking selection and promotion stand-ins. |
| `routing.py` | Carry paths bounded by geometry, occupancy and clearance. |
| `knight_stage.py` | Temporary blocker staging with strict routing on every leg. |
| `offset_stage.py` / `corner_offset.py` | Reversible piece nudges and routing around displaced pieces. |
| `physical.py` | Expected inventory, transfers and promotion stand-in replay. |
| `reset_plan.py` | Preflight of the complete return to standard setup. |
| `checkpoint.py` | Versioned, atomic game and physical-state persistence. |
| `game.py` | Runner ownership, durable preparation and move/reset execution. |
| `game_preflight.py` | Complete move/reset preflight, staging and offset-aware routes. |
| `game_recovery.py` / `game_errors.py` | Inspected reconciliation, cancellation, commits and shared errors. |
| `protocol_limits.py` / `serial_link.py` / `wire.py` | Uno limits, framed transport, replies, timeouts and occupancy encoding. |
| `host_config.py` / `link_adapters.py` | Configuration, simulation and durable serial logs. |
| `play.py` / `gui.py` / `gui_http.py` | CLI, browser startup and HTTP transport using one `GameRunner`. |
| `gui_controller.py` | Session/serial ownership, local controls and browser state views. |
| `gui_worker.py` | Single-worker game/reset loops, engine turns and STOP-aware motion. |
| `gui_remote.py` | Aina request fences, queue tickets and durable receipt/status views. |
| `host_runtime.py` | Shared connection setup, geometry, engine selection and checkpoint archival. Entry points retain link ownership. |
| `gui_snapshot.py` | Synchronized checkpoint reads and browser history/action views; never reads the mutable runner. |
| `remote_http.py` / `remote_contract.py` / `remote_session.py` | Aina authentication, PocketMage LAN transport, typed requests, source binding and position fences. |
| `api_errors.py` | Structured refusals shared by HTTP adapters. |
| `move_expression.py` | Speech cleanup and legal expression resolution. |
| `gui_static/gui_*.js` / `build_gui.py` | Browser sources and generated `app.js`. |

## Configure and run

From `computer/`, activate a Python virtual environment and install dependencies:

```text
python -m pip install -r requirements.txt
python -m chess_table_host.play --help
python -m chess_table_host.gui --help
```

[`config.json`](config.json) supplies defaults. Override the serial port and
Stockfish path for the current machine; WSL needs Linux-visible paths.
Physical play reads live `GEOM` and requires firmware capability `OVER20`;
geometry fixtures are for simulation. Use the [quick start](../docs/QUICK_START.md)
for launch commands, [GUI guide](../docs/GUI_GAME.md) for browser play, and
[physical runbook](../docs/STAGE4_TEST_GAME.md) for USB setup and recovery.

## Local Aina control API

Enable the separate loopback API with `--remote-token-file PATH`; its default
port is 8766. The private token must contain 32–512 printable ASCII characters.
See the [local API contract](../docs/REMOTE_AINA.md#local-api-contract) for
authentication, routes, payloads and recovery.

Aina supports manual moves from both sides or Stockfish with the human as White
or Black. The host persists source identity, mode/color, legal resolutions and
receipts. Aina-bound games accept moves through that API; the GUI retains local
STOP, reset and reconciliation controls. A move's acceptance means durable
preparation. Wait for its committed receipt and status `can_move` before the
next human move, including Stockfish's reply.

## PocketMage control

Enable `--enable-pocketmage --pocketmage-bind ADDRESS --pocketmage-port 8767`
for trusted-LAN chess control. The separate `.tar` app displays host legal moves and
supports manual, human/Stockfish and Stockfish self-play. Commands share the
existing worker, ownership fences and checkpoint receipts. Aina remains
authenticated on loopback. See [the API contract](../docs/POCKETMAGE_CONTROL.md).

## Checks and limits

From `computer/`, run `python -m unittest discover -s tests -q` in the project
environment. After editing `gui_static/gui_*.js`, run `python build_gui.py` to
regenerate served `gui_static/app.js`.

Preserve `runs/` checkpoints and their `<state-name>.serial.jsonl` logs.
Resume the existing checkpoint after inspection; never automatically replay
an interrupted carry or create a fresh checkpoint to hide uncertainty.

Every route obeys measured travel bounds and the Uno's 12-waypoint cap.
Tight-route fallback and crowded-horizontal cost are planning choices, not
physical clearance guarantees; modeled clearance violations still block motion.
Standard chess is supported; Chess960 and piece sensing are out of scope.
See [architecture](../docs/ARCHITECTURE.md), [routing](../docs/ROUTING.md),
the [wire contract](../docs/PROTOCOL.md), and [roadmap](../docs/ROADMAP.md).

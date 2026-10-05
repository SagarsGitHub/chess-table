# Computer host

`chess_table_host` owns chess rules, expected piece and parking locations, route
planning, Stockfish, the local GUI, and checkpointed serial control. The Uno
executes one validated carry at a time. The table has no piece sensors; a
completed `CARRY` updates expected occupancy, while an uncertain transfer
requires inspection and reconciliation.

## Code map

| Module | Responsibility |
| --- | --- |
| `planner.py` | Legal UCI moves, capture order, parking choice, and pawn promotion stand-ins. |
| `routing.py` | Bounded half-square carry paths checked against geometry and occupied locations. |
| `physical.py` | Expected physical inventory, transfer application, and replay of stand-in pawns. |
| `reset_plan.py` | Preflight of the complete board return, including staging when parked pieces block each other. |
| `checkpoint.py` | Versioned, atomic game and physical-state persistence. |
| `game.py` | Move/reset execution, commits, and fault reconciliation. |
| `serial_link.py` / `wire.py` | Framed controller commands, replies, timeouts, and occupancy encoding. |
| `host_config.py` / `link_adapters.py` | Shared settings validation, simulated carries, and durable serial logs. |
| `play.py` / `gui.py` / `gui_http.py` | CLI, browser session, and HTTP entry points using the same `GameRunner`. |
| `gui_static/gui_*.js` / `build_gui.py` | Browser model, view, and events; builds standalone `app.js`. |

A capture parks its victim before moving the attacker; castling and en passant
also use ordered transfers. A promoted pawn remains the physical pawn while
chess rules treat it as its selected piece. Board reset restores all 32 original
pieces from the checkpointed physical inventory before starting a fresh game.
The host plans a complete move or reset before sending its first carry.

## Configure and run

Install [`requirements.txt`](requirements.txt) in a Python virtual environment.
[`config.json`](config.json) defaults to COM3, Stockfish in
`~/Documents/stockfish`, 18 mm moving and standing pieces, a 0 mm clearance
margin, and tight-route fallback. On WSL, pass the detected `/dev/ttyACM*` or
`/dev/ttyUSB*` port and the Linux-visible Stockfish path. The host reads fresh
`GEOM` from the connected controller; a geometry fixture is for simulation.

Use `python -m chess_table_host.play --help` for CLI options, including
`--self-play`, `--manual-both`, `--reset-board`, `--resume`, and
`--strict-clearance`. Use `python -m chess_table_host.gui --help` for the
browser game. Exact WSL setup, USB attachment, and physical recovery commands
are in the [stage 4 runbook](../docs/STAGE4_TEST_GAME.md) and
[GUI runbook](../docs/GUI_GAME.md).

Normal physical play advances after an ID-matched `DONE CARRY`; it does not
pause after every move. `--supervised` adds prompts for deliberate testing.
The host records TX/RX frames beside a physical checkpoint as
`<state-name>.serial.jsonl`. Do not replay a stopped, timed-out, or faulted
carry automatically: inspect its source, destination, parking, and magnet
position, then reconcile the saved state. The GUI shows expected locations,
not sensed piece positions.

## Checks and limits

From `computer/` run `python -m unittest discover -s tests -q`. The tests
cover chess special moves, routing and clearance fallback, checkpoint
migration, serial faults, reset cycles, and GUI flows. The three tracked host
JSON files serve different purposes: runtime defaults, measured simulation
geometry, and a reset regression fixture. `runs/*.json` are ignored physical
or simulated checkpoints; preserve physical runs and their serial logs.

After editing a `gui_static/gui_*.js` source file, run `python build_gui.py`
from `computer/` to regenerate the standalone `gui_static/app.js`. The
standalone file remains compatible with an already-running GUI server.

The clearance override can route closer to standing pieces after strict
clearance fails, while still avoiding occupied centers and enforcing the Uno's
12-waypoint and measured travel limits. It does not prove real clearance.
Standard chess is supported; Chess960 castling and automatic piece sensing are
out of scope. See the [architecture](../docs/ARCHITECTURE.md),
[routing notes](../docs/ROUTING.md), and [roadmap](../docs/ROADMAP.md) for the
physical acceptance record.

# Aina remote control

Use the [quick start](QUICK_START.md) for host installation, launch, and checkpoint
resume. This guide covers the Aina connection, commands, API, and recovery.

## Connection path

```text
User -> Aina Core (HTTPS) -> authenticated companion WebSocket
     -> authenticated loopback chess API -> GameController/GameRunner -> Uno
```

Run the chess-capable companion and host in the same Linux environment or WSL
distribution. The GUI defaults to port **8765**, the Aina API to **8766**. Both
stay local; the companion connects outward to Core. Do not expose the GUI or
chess API publicly. Core owns user/device routing; the host owns rules, Stockfish,
inventory, checkpoints, and serial execution.

## Authentication and first connection

| Credential | Purpose | Location |
| --- | --- | --- |
| `AINA_DEVICE_TOKEN` | Companion connection to Core | Private device env file; WSL default `~/.config/aina/wsl-agent.env` |
| Local chess bearer | Companion requests to the chess host | `~/.config/chess-table/aina.token`, readable by both processes |
| `AINA_CLIENT_TOKEN` | Device registration/revocation | Core administrator setup only |

Keep credentials outside repositories, messages, and logs. Core does not receive
the chess token. Use one companion per device identity; Windows and WSL companions
need distinct identities. Per-person OAuth and board consent are not implemented.

### One-time setup

1. Install Aina's locked dependencies and configure its device credential using
   Aina's `DEVICE_SETUP.md`. Reuse an existing identity. For a missing WSL
   credential, run from the Aina checkout in PowerShell:

   ```powershell
   .\scripts\setup_agent_env.ps1 -Wsl -Url '<Core URL>' -UserId '<user>' -WslDeviceId '<device>'
   ```

   On native Linux, use Aina's onboarding guide or
   `scripts/register_agent_device.py`. Registration issues a new credential;
   use it only for initial setup or deliberate rotation. Setup prompts securely
   for the administrator token when needed.

2. Create the local chess token once in WSL/Linux:

   ```bash
   umask 077
   mkdir -p "$HOME/.config/chess-table"
   chmod 700 "$HOME/.config/chess-table"
   if [ ! -e "$HOME/.config/chess-table/aina.token" ]; then
     python3 -c 'import secrets; from pathlib import Path; p = Path.home()/".config/chess-table/aina.token"; f = p.open("x", encoding="ascii"); f.write(secrets.token_urlsafe(48) + "\n"); f.close()'
   fi
   test -f "$HOME/.config/chess-table/aina.token"
   test ! -L "$HOME/.config/chess-table/aina.token"
   chmod 600 "$HOME/.config/chess-table/aina.token"
   ```

3. Start the companion with chess actions enabled using either recipe below.
   Start the host through the [quick start](QUICK_START.md), or use Aina's launch
   command after the companion connects. Core must also run a chess-capable release.

### PowerShell walkthrough

From the Aina checkout:

```powershell
.\scripts\wsl_agent_companion.ps1 -EnableChessActions
```

Defaults are `http://127.0.0.1:8766` and the chess token path above. Use
`-ChessHostUrl`, `-ChessHostTokenFile`, `-Distro`, or `-WslEnvFile` for overrides.
The wrapper finds a sibling chess-table checkout for `chess.launch`.

### WSL terminal walkthrough

In WSL/Linux, use the actual checkout and device-env paths:

```bash
cd /path/to/aina_core
set -a
. "$HOME/.config/aina/wsl-agent.env"
set +a
.venv/bin/python -u scripts/local_agent_daemon.py \
  --terminal-input --enable-chess-actions \
  --chess-table-root /path/to/chess-table \
  --chess-host-url http://127.0.0.1:8766 \
  --chess-host-token-file "$HOME/.config/chess-table/aina.token"
```

Keep the host and companion terminals open. `/status detail` in the companion
must show the intended Core URL, user/device identity, and `connected`. An
unauthenticated local check must return **401** while the host is listening:

```bash
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8766/v1/status
```

Then ask Aina for **“chess table status”**. Its response should include the host
game ID, ply, side to move, and ASCII expected board derived from FEN. The board
has no piece sensors; status cannot confirm actual placement.

## Game requests and spoken moves

- **“Start chess table”** launches a fresh physical host; add **“in simulation
  mode”** for simulation. Launch leaves the game idle and does not move motors.
- **“Start a chess game against Stockfish with me as white”** or **“as black”**
  binds and starts the game. Black allows Stockfish's White opening move.
- **“Start a chess game with me as white”** or **“with me as black”** selects
  Stockfish without requiring its name in the spoken command.
- **“Start a chess match”** selects manual play for both sides.
- **“Start a chess game in manual move mode”** accepts explicit moves for both sides.
- Moves include **“knight to c3”**, **“knight from b1 to c3”**, **“pawn takes d5”**,
  **“castle kingside”**, **“pawn to a8 promote to queen”**, SAN `Nc3`, and UCI `b1c3`.

Confirm physical setup before starting: game start can reference/home the machine.
For Aina play, leave the GUI game unstarted and let Aina bind it. For local play,
use GUI **Start game** instead. Browser move entry is disabled once Aina binds
the game; local STOP and recovery controls remain available.

Speech cleanup accepts `Queen 2D3` as `queen to d3`, and two-square separators
such as `d22d4`, `d2-d4`, `d2_d4`, `D2, D4`, whitespace, `to`/`two`/`too`, and
arrows. Explicit source squares are preserved; incomplete `2d3` needs a piece
or source. The host rejects illegal or ambiguous moves; supply the requested
source or promotion piece. Capture words require a capture. Commands must be
immediate, not quoted, conditional, future requests, or requests for advice.

## Local API contract

Version 1 uses the separate loopback listener. Requests need
`Authorization: Bearer <local chess token>`; POST bodies are JSON objects with
`Content-Type: application/json` and at most 8192 bytes. Browser origins and
nonlocal Host headers are rejected.

| Route | Purpose | Success |
| --- | --- | --- |
| `GET /v1/status` | Identity, mode/color, ply/FEN, phase, pending action, `can_move` | 200 status |
| `POST /v1/session/start` | Bind a fresh standard game or resume its matching binding | 202 status |
| `POST /v1/session/reset` | End the fenced game after its current move, then restore standard setup | 202 status |
| `POST /v1/moves/resolve` | Save a legal interpretation and its position fence without motion | 200 move payload |
| `POST /v1/moves` | Preflight, save, and execute a fenced UCI move | 202 prepared/pending; 200 committed/aborted replay |
| `GET /v1/moves/{request_id}` | Read a durable receipt | 200 receipt |
| `POST /v1/stop` | Request STOP; body `{}` | 202 status |

Read status before session start:

```json
{
  "host_game_id": "<current host UUID>",
  "source_kind": "aina",
  "source_game_id": "<stable Aina game ID>",
  "request_id": "<start request ID>",
  "mode": "human_engine",
  "human": "white"
}
```

`mode` is `external` (manual, default) or `human_engine` (Stockfish); `human` is
`white` (default) or `black`. Both persist and must match on resume. A matching
start resumes the same game without clearing its position or receipts. New games
require reset/setup; the API offers no reconciliation or raw serial route.

Reset accepts only `{"host_game_id":"<current host UUID>"}`. Obtain that UUID
from status and preserve it for every retry of the same reset request; do not
replace it with a newer game's UUID. A 202 response means reset was requested,
not completed. Poll status until `busy` is false, `phase` is `idle`, and a new
`host_game_id` appears. While queued or running, phase is `resetting` and moves
are disabled. The host finishes the current chess move before returning pieces,
preserves the played checkpoint in an archive, then clears the Aina binding and
creates a fresh game identity. Even a board already at standard setup receives
a fresh identity. Matching retries while resetting observe the same worker;
requests for the previous UUID after completion are rejected with 409. Missing,
malformed or extra request fields are rejected with 400. Reset can reference the
axes and move pieces; it does not bypass an uncertain move or reset transfer.

The board has no piece sensors. After interruption or uncertain delivery,
inspect and reconcile the saved action through local controls before another
move. Preserve checkpoints and their serial logs. Firmware `DONE CARRY` advances
expected occupancy but cannot prove actual piece delivery.
Resolve accepts `{"request_id":"move-1","expression":"pawn to e4"}` and returns
`host_game_id`, `source_game_id`, `expected_ply`, `expected_fen`, and legal `uci`.
Add the same `request_id` and submit these fields unchanged to `/v1/moves`.
Exact UCI clients may build those fields from current status without resolving.
Request IDs are bounded to 128 characters; source IDs, expressions, and FEN to 256.
Unknown fields and malformed types are rejected before binding or queueing.

Each new move uses a fresh request ID. Retries preserve the ID, host/source
identity, exact committed ply/FEN, and UCI. A repeated resolution returns its
original saved payload even after position changes or restart; changing its
expression is rejected. A repeated move returns its receipt without repeating
motion; changed bodies, stale positions, wrong games, and competing sources are rejected.
The companion freezes requests privately beside its token file.

Receipts are `prepared`, `in_flight`, `committed`, `aborted`, or
`reconciliation_required`. **202 means the plan and receipt are saved, not that
a piece arrived.** Poll the receipt for the move outcome, then wait for status
`can_move` before the next human move; Stockfish may still be replying after the
human receipt commits. STOP preserves the checkpoint and binding. A process
restart alone does not resume play; send a matching start after any required inspection.

Errors use `{"error":{"code":"...","message":"..."}}`:

| HTTP | Meaning |
| --- | --- |
| 400 / 413 / 415 | Malformed fields / oversized body / wrong content type |
| 401 / 403 | Missing or incorrect bearer / browser origin or nonlocal Host |
| 404 / 405 | Missing route or unprepared receipt / unsupported method |
| 409 | Conflicting binding, request ID, position, turn, or host operation |
| 422 | Illegal, incomplete, or ambiguous move |
| 503 | Unavailable Stockfish configuration or filesystem state |
| 504 | Preparation still pending; retry the same request ID |

Checkpoint schema **6** requires session options and the resolution map. Migration
preserves identity, receipts, and physical plans. Earliest v5 files lacking all
three fields receive manual/White/empty-cache defaults. Explicit v5 mode/color
must occur together; a missing cache becomes empty, while a cache without both
options is rejected. Missing v6 fields are rejected. Loading checks historical
FEN/ply, legality, and active receipt/pending-move agreement before recovery.

## Credential changes and recovery

| Symptom | Check |
| --- | --- |
| Companion Core 401/403 | Core URL, user/device identity, and device credential |
| Companion chess request gets 401 | Same chess token file/user; restart both after rotation |
| Cannot connect to chess host | Host running, port, same WSL distribution; `ss -ltn` |
| Generic chess reply or missing action | Deployed Core revision (`/health`), companion capabilities, device route |
| Launch fails or duplicate companion | Configured checkout/launcher, existing listener, one process per device ID |
| Move gets 409 | Status/receipt, binding, exact ply/FEN, and whose turn it is |
| Stockfish unavailable | Host engine path and executable |

For chess-token rotation, stop host and companion, replace only the local token,
then restart both using the existing checkpoint. For device-token rotation, use
Aina's setup flow with the same identity, verify connection, and revoke the old
credential through Core's administrator credential API. Keep env backups private.
Credential rotation does not resolve a game-state conflict.

After STOP, timeout, disconnect, reboot, or uncertain delivery, inspect the board
and reconcile the saved action through local controls before another move. Keep
`computer/runs/` checkpoints and serial logs. `DONE` advances expected occupancy;
it cannot prove physical delivery. See the [recovery runbook](STAGE4_TEST_GAME.md).

## Activation and acceptance order

Update/deploy chess-capable Core, update and opt in the companion, and restart the
host with its remote listener/token. A source push alone does not activate them.
Validate simulation first: status/start/move, same-ID replay across restart,
stale/conflicting requests, and STOP/reconciliation. Then run an attended physical
trial with the independent cutoff available. Current evidence and remaining
loaded trials belong in the [roadmap](ROADMAP.md).

## Future work

Per-user OAuth/board grants and Chess.com integration remain planned. Completed
game replay needs validated standard-chess PGN and the same ordered receipt
contract. Live Chess.com play needs official access and a separate mode without
Stockfish. See the [roadmap](ROADMAP.md) for priorities and acceptance gates.



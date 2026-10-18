# Aina remote control

Use the [quick start](QUICK_START.md) for installation, launch and checkpoint resume.

```text
User -> Aina Core (HTTPS) -> authenticated companion WebSocket
     -> authenticated loopback chess API -> GameController/GameRunner -> Uno
```

Run companion and host in the same Linux environment or WSL distribution. GUI port **8765** and API port **8766** stay local; the companion connects outward to Core. Do not expose them publicly. Core routes users/devices; the host owns rules, Stockfish, inventory, checkpoints and serial execution.

## Authentication and first connection

| Credential | Purpose | Location |
| --- | --- | --- |
| `AINA_DEVICE_TOKEN` | Companion connection to Core | Private device env file; WSL default `~/.config/aina/wsl-agent.env` |
| Local chess bearer | Companion requests to the chess host | `~/.config/chess-table/aina.token`, readable by both processes |
| `AINA_CLIENT_TOKEN` | Device registration/revocation | Core administrator setup only |

Keep credentials outside repositories, messages and logs. Core never receives the chess token. Use one companion per identity; Windows and WSL need distinct identities. Per-person OAuth/board consent remains planned.

### One-time setup

1. Install Aina's locked dependencies and configure its credential with Aina's `DEVICE_SETUP.md`. Reuse an existing identity. For initial WSL setup, run from its checkout:

   ```powershell
   .\scripts\setup_agent_env.ps1 -Wsl -Url '<Core URL>' -UserId '<user>' -WslDeviceId '<device>'
   ```

   Native Linux uses Aina's onboarding guide or `scripts/register_agent_device.py`. Registration creates a credential; reserve it for initial setup or deliberate rotation. Setup securely prompts for the administrator token.

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

3. Start the companion using either recipe below and launch the host through the [quick start](QUICK_START.md) or Aina. Core must also run a chess-capable release; a source push alone does not activate them.

### PowerShell walkthrough

From the Aina checkout:

```powershell
.\scripts\wsl_agent_companion.ps1 -EnableChessActions
```

The wrapper finds a sibling chess-table checkout and defaults to `http://127.0.0.1:8766` and the token path above. Override with `-ChessHostUrl`, `-ChessHostTokenFile`, `-Distro` or `-WslEnvFile`.

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

Keep both terminals open. Companion `/status detail` must show the intended Core URL, identity and `connected`. This unauthenticated check must return **401**:

```bash
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:8766/v1/status
```

Ask Aina for **“chess table status”** to read game ID, ply, side to move and expected board. Status cannot confirm actual piece placement.

## Game requests and spoken moves

- **“Start chess table”** launches an idle physical host; add **“in simulation mode”** for simulation.
- **“Start a chess game against Stockfish with me as white”** or **“as black”** binds and starts Stockfish play. Its name is optional. Human Black permits its White opening move.
- **“Start a chess match”** or **“start a chess game in manual move mode”** selects manual play for both sides.
- Moves accept **“knight to c3”**, **“knight from b1 to c3”**, **“pawn takes d5”**, **“castle kingside”**, **“pawn to a8 promote to queen”**, SAN `Nc3` or UCI `b1c3`.

Confirm physical setup before starting; game start can home/reference. Let Aina bind the unstarted GUI game. Browser move entry then becomes unavailable; local STOP and recovery remain available.

Speech cleanup accepts `Queen 2D3` as `queen to d3`, and square separators including `d22d4`, `d2-d4`, `d2_d4`, `D2, D4`, whitespace, arrows and `to`/`two`/`too`. Explicit sources are preserved; incomplete `2d3` needs a piece/source. Illegal or ambiguous moves need clarification; capture words require a capture. Only immediate commands are accepted, excluding quoted, conditional, future or advice requests.

## Local API contract

Version 1 uses the separate loopback listener with `Authorization: Bearer <local chess token>`. POST bodies must be JSON objects, `Content-Type: application/json`, at most 8192 bytes. Browser origins and nonlocal Host headers are rejected.

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

`mode` is `external` (manual, default) or `human_engine` (Stockfish); `human` is `white` (default) or `black`. Both persist and must match on resume. A matching start preserves position and receipts. New games require reset/setup. There is no remote reconciliation or raw serial route.

Reset accepts only `{"host_game_id":"<current host UUID>"}`. Read this UUID from status and preserve it for every retry; never substitute a newer game's UUID. **202 means requested, not completed.** Poll until `busy` is false, `phase` is `idle`, and a new `host_game_id` appears. Queued/running reset sets phase `resetting` and disables moves. The host finishes the current chess move, archives its checkpoint, returns pieces, clears the binding and creates a fresh identity even if pieces were already home. Matching retries observe the same worker; an old UUID after completion gets 409. Missing/malformed/extra fields get 400. Reset may reference axes and move pieces; uncertainty still requires local inspected recovery.

Resolve accepts `{"request_id":"move-1","expression":"pawn to e4"}` and returns `host_game_id`, `source_game_id`, `expected_ply`, `expected_fen` and legal `uci`. Add the same `request_id` and submit those fields unchanged to `/v1/moves`. Exact UCI clients may build the fields from status. Request IDs are limited to 128 characters; source IDs, expressions and FEN to 256. Unknown fields/malformed types are rejected before binding or queueing.

Each new move needs a fresh request ID. Retries preserve ID, host/source identity, exact committed ply/FEN and UCI. Resolution replay returns its original payload across position changes/restarts; changing the expression is rejected. Move replay returns its receipt without motion; changed bodies, stale positions, wrong games and competing sources are rejected. The companion freezes requests privately beside its token file.

Receipts are `prepared`, `in_flight`, `committed`, `aborted` or `reconciliation_required`. **202 means durable preparation, not piece delivery.** Poll the receipt, then wait for status `can_move` before the next human move; Stockfish may still be replying. STOP preserves checkpoint/binding. Process restart does not resume play: send matching start after any required inspection.

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

Checkpoint schema **6** requires session options and the resolution map. Migration preserves identity, receipts and physical plans. Earliest v5 files lacking all three receive manual/White/empty-cache defaults. Explicit v5 mode/color must appear together; missing cache becomes empty, but cache without both options is rejected. Missing v6 fields are rejected. Loading checks historical FEN/ply, legality and active receipt/pending agreement before recovery.

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

For chess-token rotation, stop host/companion, replace only the local token and restart both with the existing checkpoint. For device-token rotation, use Aina's setup with the same identity, verify connection, then revoke the old credential through Core's administrator API. Keep env backups private. Rotation does not resolve game-state conflicts.

After interruption or uncertain delivery, follow the [local inspection and recovery controls](QUICK_START.md#pause-recover-and-reset) before another move. Preserve checkpoints/logs: `DONE CARRY` updates expected occupancy and cannot verify delivery. Physical acceptance and future integration work belong in the [roadmap](ROADMAP.md).

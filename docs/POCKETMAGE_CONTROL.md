# PocketMage chess control

The standalone [PocketMage Chess app](https://github.com/SagarsGitHub/pocketmage-chess)
controls the existing host over trusted Wi-Fi. The computer retains rules,
legal moves, Stockfish, inventory, routing, checkpoints and the sole Uno serial
connection. No Uno firmware changes are required.

For the proposed device screens and host API extensions for daily-game
browsing and recorded replay, see [PocketMage and Chess.com](POCKETMAGE_CHESSCOM.md).
The current Chess.com controls are available in the host browser GUI.

## Enable the listener

```bash
./Play-ChessTable.sh --resume runs/EXISTING.json \
  --enable-pocketmage --pocketmage-bind '<table-ip>'
# Or start a simulation:
./Start-ChessTable.sh --enable-pocketmage --pocketmage-bind 0.0.0.0
```

PowerShell accepts `-EnablePocketMage -PocketMageBind ADDRESS -PocketMagePort 8767`.
Relative checkpoint paths resolve from `computer/`; use the existing physical
checkpoint or its absolute path when resuming. Replace `<table-ip>` with the
host's LAN address; the device URL is `http://<table-ip>:8767`. WSL additionally
needs a device-reachable LAN address.

The listener is opt-in and unauthenticated. Anyone who can reach it can issue
its permitted commands; use a trusted LAN. Literal-IP Host headers at the
configured port are required, and browser Origin headers are rejected. The
browser GUI/recovery listener remains on loopback 8765 and authenticated Aina
on loopback 8766. All listeners share one controller, worker and checkpoint.

## Routes and status

Requests are JSON objects, maximum 8192 bytes, with `Content-Type:
application/json`, normal Host and Content-Length headers, and no Origin.
Unknown fields and invalid types are refused. Responses use schema version 1.

| Method and path | Use |
| --- | --- |
| `GET /v1/status` | Committed board, legal moves, ownership, readiness and activity. |
| `POST /v1/session/start` | Bind a fresh session or explicitly resume a matching one. |
| `POST /v1/session/reset` | Confirm and request an automatic physical return to standard setup. |
| `GET /v1/resets/{request_id}` | Poll the durable physical reset outcome. |
| `POST /v1/moves` | Submit a fenced legal UCI move. |
| `GET /v1/moves/{request_id}` | Poll a durable receipt belonging to this source. |
| `POST /v1/stop` | Body `{}`; shared controller STOP. |

Expression resolution and reconciliation are not exposed. Use local host
controls to inspect/reconcile uncertainty or continue an interrupted reset.
Starting a session does not reset pieces. Reconnection does not resume motion.

Status fields: `schema_version`, `host_game_id`, `source_kind`, `source_game_id`,
`mode`, `human`, `ply`, `last_move_uci`, `fen`, `phase`, `can_move`, `pending`,
`busy`, `checkpoint_cached`, `error`, `legal_moves` (UCI array), `turn`
(`white|black`), `check`, and `result` (null until game end).

Enable submission only for the matching source/session, with fresh `can_move`
and no unresolved device intent. `busy` alone is not an input gate: the worker
is alive while awaiting a person. Legal destinations come from chess rules;
physical route preflight can still reject a move. Unknown phases disable input.
Phases include idle, waiting_for_move, moving, engine_thinking, paused,
resetting, reconciliation_required, game_over and error.

## Start / explicit resume

Read status, confirm the physical setup on the device, generate stable random
session and request IDs, and persist the complete intent before sending:

```json
{
  "host_game_id": "<UUID from status>",
  "source_kind": "pocketmage",
  "source_game_id": "pm-<unique-session-id>",
  "request_id": "start-<unique-request-id>",
  "mode": "human_engine",
  "human": "white"
}
```

Modes: `external` for manual input on both sides, `human_engine` for a human
playing `white` or `black`, and `engine_engine` for Stockfish on both sides.
The last maps to the host's existing self_play worker; Aina keeps its original
modes. Human Black and self-play can permit an immediate engine opening.

202 means accepted, not motion complete. Matching retries neither start another
worker nor clear a game. Competing source/session ownership is refused. Source
IDs coordinate ownership rather than authenticate the device. After a restart,
load saved identity and choose explicit Resume. A new game needs a completed
physical reset, requested explicitly from the app or local host controls.

## Automatic physical reset

The app's `reset board` command opens a confirmation before sending anything.
It returns all pieces, including captured pieces in parking, through the
existing inventory and route planner. The shared worker finishes an active
chess move before beginning the return plan. This is a graceful game-ending
request; use STOP to interrupt motion. Reset works only for a standard-start
game bound to the same PocketMage session, with no unresolved device move.
An idle board with uncertain saved work requires local inspection first.

Freeze and persist this complete intent before submission:

```json
{
  "request_id": "reset-<unique-request-id>",
  "host_game_id": "<current UUID>",
  "source_game_id": "<saved PocketMage session ID>",
  "expected_ply": 12,
  "expected_fen": "<latest committed FEN>",
  "confirmed": true
}
```

The receipt echoes every field and adds `schema_version: 1`, `state`,
`resulting_host_game_id`, `phase`, `action_number`, and `total_actions`.
States are `prepared`, `in_flight`, `committed`, `aborted`, and
`reconciliation_required`. `phase` also distinguishes `finishing_move` from
`resetting`; action counts describe the currently saved chess/reset action.
`resulting_host_game_id` is null until a committed reset creates a fresh
unbound standard-start game. POST returns 202 while active, or 200 for an
already terminal outcome; receipt GET returns 200, or 404 if never registered.

The checkpoint retains reset receipts across that new game identity and host
restarts. Exact duplicate POSTs only observe their saved outcome; altered
payloads with a reused ID are refused. Neither retries, reconnection nor reboot
resume an interrupted reset. At a known completed action boundary, an
interrupted request becomes `aborted`; any uncertain carry becomes
`reconciliation_required`. Inspect the table and use local recovery controls
before continuing. The app polls the same receipt after a timeout and never
automatically replays a reset. Historical checkpoint versions migrate without
changing their physical inventory or pending actions.

A wellformed new reset refused before durable registration returns 409 with
`error.code: "reset_not_accepted"` and `error.request` echoing all six frozen
request fields. Only that exact echoed refusal lets the app discard its unsent
intent and refresh stale FEN/ply before a new explicit request. Reused-ID
conflicts and any failure after registration never use this code: retain those
intents and inspect their receipts. This distinction also covers a Stockfish
move committing between the app's status poll and reset submission.

## Moves and receipts

Freeze the latest committed identity, FEN and ply. Save one complete payload
before submitting, and never update the fence within a retry:

```json
{
  "request_id": "move-<unique-request-id>",
  "host_game_id": "<current UUID>",
  "source_game_id": "<saved PocketMage session ID>",
  "expected_ply": 0,
  "expected_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
  "uci": "e2e4"
}
```

UCI promotion suffixes are q/r/b/n. Request IDs allow 1-128 characters from
`A-Z a-z 0-9 . _ : -`; source IDs and FEN are bounded to 256 characters.
Manual mode accepts the side to move; engine mode only accepts the human turn.

Receipt states are prepared, in_flight, committed, aborted and
reconciliation_required. Duplicates return the recorded outcome without
repeating motion; changed payloads with a reused ID are refused. After committed,
wait for fresh can_move, including any Stockfish reply. A timeout/disconnect is
an unknown outcome. Preserve the intent and look up the same receipt on reconnect;
never create a replacement move or automatically replay uncertain/aborted work.

Errors use `{"error":{"code":"...","message":"..."}}`: 400 malformed request,
403 policy, 404 absent route/receipt, 409 identity/position/readiness conflict,
413 oversized body, 415 wrong content type, 422 illegal move, 503 unavailable
engine/storage, 504 preparation still pending. Preserve unresolved intents and
inspect status. An offline STOP must show undelivered; use local controls/power
cutoff if the network cannot reach the table.

The board has no sensors. Status and committed receipts describe expected
occupancy, not observed delivery. Interrupted or uncertain motion requires local
physical inspection and reconciliation before another move.

## Verification

Run `python -m unittest discover -s tests -q` from `computer/`. Simulated motor
checks and device firmware builds are separate from attended device/table
acceptance. Preserve physical checkpoints and serial logs, and record actual
hardware observations before claiming acceptance.

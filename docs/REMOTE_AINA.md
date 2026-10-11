# Aina control on Linux

The chess host and a chess-enabled Aina companion must run as the same user
in the same Linux environment. The GUI listens on loopback port 8765; Aina's
authenticated API listens on loopback port 8766. Keep both listeners local.

Install `computer/requirements.txt` in `computer/.venv` and install the native
Stockfish engine. The Bash launcher finds `/usr/games/stockfish` when no downloaded
engine is found. `--engine PATH` overrides discovery.

The companion and host must read the same private bearer token. The default is
`~/.config/chess-table/aina.token`. Create it once if it does not exist:

```bash
umask 077
mkdir -p "$HOME/.config/chess-table"
chmod 700 "$HOME/.config/chess-table"
python3 - <<'PY'
import secrets
from pathlib import Path
p = Path.home() / ".config/chess-table/aina.token"
if not p.exists():
    with p.open("x", encoding="ascii") as f:
        f.write(secrets.token_urlsafe(48) + "\n")
PY
chmod 600 "$HOME/.config/chess-table/aina.token"
```

Start a fresh physical host with pieces in the standard starting position:

```bash
./Start-ChessTable.sh --mode physical --enable-aina --state runs/boot-physical.json
```

On later launches, preserve and resume that exact checkpoint:

```bash
./Start-ChessTable.sh --mode physical --enable-aina --resume runs/boot-physical.json
```

For a boot wrapper, choose `--resume` only if the checkpoint already exists;
otherwise choose `--state`. The host creates the parent directory and checkpoint.
Do not create an empty JSON file. Restarting the host leaves play stopped.

The launcher does not start Aina's companion. Configure the companion's device
credential separately and enable its chess actions, with host URL
`http://127.0.0.1:8766` and the same token file. Keep one chess companion connected.
Ask Aina for "chess table status", then "start a chess game against Stockfish
with me as white" (or black), or "start a chess game in manual move mode".

## API

Requests require `Authorization: Bearer <local token>`. POST bodies are JSON.
Browser origins and nonlocal Host headers are rejected.

| Route | Purpose |
| --- | --- |
| `GET /v1/status` | Read expected position, source binding, phase and `can_move`. |
| `POST /v1/session/start` | Bind a fresh game or resume its matching Aina binding. |
| `POST /v1/moves/resolve` | Resolve an expression to a unique legal UCI move without motion. |
| `POST /v1/moves` | Submit a move fenced by host/source ID, expected ply and FEN. |
| `GET /v1/moves/{request_id}` | Read the durable move receipt. |
| `POST /v1/stop` | Request STOP; JSON body is `{}`. |

Reuse a request ID only for an identical retry. Receipts prevent duplicate
physical execution; stale positions, conflicting sources and changed retry
payloads are rejected. Invalid/ambiguous move expressions return 422. A 202
response means saved/prepared, not delivered: poll the receipt and wait for
`can_move` before the next human move, including the Stockfish reply.

The board has no piece sensors. After interruption or uncertain delivery,
inspect and reconcile the saved action through local controls before another
move. Preserve checkpoints and their serial logs. Firmware `DONE CARRY` advances
expected occupancy but cannot prove actual piece delivery.

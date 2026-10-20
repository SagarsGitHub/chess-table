# Chess.com browsing and recorded-game replay

The host GUI can browse `ofroni`'s current daily games, find finished games in a
player's monthly archives, and search a bundled historical starter library.
Selecting a game previews its recorded position. Physical replay starts only
after explicit confirmation, from the standard starting position. A daily-game
switch resets and recreates the selected snapshot, then follows newly published
moves every 30 minutes while the host is running. Finished and historical games
support individual steps or autoplay with a chosen delay between half-moves.
All motion uses the existing runner.

Chess.com's [Published-Data API](https://www.chess.com/news/view/published-data-api)
provides public account data and recorded games. It is read-only: moves must
still be played on Chess.com. Responses can be cached or delayed, so this is not
a live connected-board transport. There is no engine assistance for an
in-progress daily game.

## Browse and select

Use the Chess.com controls in the existing host GUI. The default username is
`ofroni`; archived searches can use another public username. Choose an available
archive month before searching its completed games. Daily and archive reads use
the public API; historical search uses local bundled data.

| Host GUI route | Purpose |
| --- | --- |
| `GET /api/chesscom/daily?username=ofroni` | List current daily games. |
| `GET /api/chesscom/archives?username=ofroni` | List available archive months. |
| `GET /api/chesscom/finished?username=ofroni&year=2026&month=10&q=` | Search finished games in one month. |
| `GET /api/chesscom/historical?q=` | Search the curated historical games. |
| `GET /api/chesscom/selection` | Read the selected preview and replay state. |
| `POST /api/chesscom/select` | Select a daily, finished, or historical game by its returned ID. |
| `POST /api/chesscom/daily/switch` | Confirm, reset the table and replay the selected daily snapshot, then enable periodic following. |
| `POST /api/chesscom/daily/check` | Check the active daily game now and mirror safe appended recorded moves. |
| `POST /api/chesscom/replay/start` | Confirm and bind a replay from a clean standard-start checkpoint. |
| `POST /api/chesscom/replay/next` | Submit exactly the next recorded move. |
| `POST /api/chesscom/replay/autoplay` | Replay a finished or historical game with the requested delay. |
| `POST /api/chesscom/replay/pause` | Pause autoplay after the current recorded move finishes. |

These routes belong to the local host GUI listener. They are not added to the
PocketMage `/v1` API. See the [PocketMage extension note](POCKETMAGE_CHESSCOM.md)
for the proposed device work.

A selection request identifies the kind and game, with the same username and
archive month used for browsing when applicable:

```json
{
  "kind": "historical",
  "id": "opera-1858-morphy-duke-count"
}
```

Use the returned `selection_token` for replay requests. Selecting another game
or a changed score creates a new token; selecting the same unchanged score
keeps its token. An old browser action cannot silently operate on a newer
selection. Browsing and selection never issue motor commands, change the
physical checkpoint, or start Stockfish.

## Switch and follow a daily game

Select a daily game, inspect its players and snapshot, then submit
`{"selection_token":"<token>","confirmed":true}` to
`POST /api/chesscom/daily/switch`. This explicit motion command returns the
pieces to standard setup and replays the selected recorded moves to recreate
that game. It uses the existing reset planner, one shared worker and the same
durable physical checkpoints as normal play. Switching refuses a busy table,
remote-owned game, pending action or uncertain physical state; inspect/recover
the table and wait for existing work to finish first.

While that daily game is active, the host checks it every 1800 seconds. This
schedule runs inside the table host; it does not create a Codex automation and
does not run while the host is closed. `POST /api/chesscom/daily/check` with `{}`
requests the same check immediately. Checking fetches public data and can
start physical replay of newly recorded moves when the current source and
board are safe.

Following requires the same game ID, an unchanged exact move prefix and a
matching committed board position. Only legal moves appended to the saved
score may advance the table. A shorter score, divergent history, missing game
or failed validation disables automatic continuation and requests attention.
PubAPI responses may be cached or delayed, so a successful 30-minute check does
not guarantee that the table has the latest Chess.com position. The host never
submits a move to Chess.com or chooses a move with an engine.

Browsing another game remains a preview. Use another confirmed **Switch daily
game** command to reset and recreate that game's snapshot. A host restart does
not resume polling or motion: inspect/recover as needed and explicitly switch
again to restart following.

## Step through or autoplay a recorded game

1. Inspect the table and reconcile any interrupted or uncertain physical work
   using the existing local recovery controls.
2. Finish the existing physical reset when necessary. Replay start requires an
   unbound, standard-start checkpoint with no saved moves or pending action.
   Selecting a different game does not reset the pieces.
3. Select the source, inspect its players, result and move list, then explicitly
   confirm replay start using `{"confirmed":true,"selection_token":"<token>"}`.
4. Advance one half-move at a time. Each next request carries the selected token
   and the current replay's committed `host_game_id`, `expected_ply`, and
   `expected_fen`. Read fresh status after completion before advancing again.
5. For a finished or historical game, optionally start autoplay with those same
   token/host-game/ply/FEN fields plus `wait_seconds`, a number from 0.1 to 3600.
   The host waits that duration between completed half-moves. Send `{}` to
   `POST /api/chesscom/replay/pause` to finish the current move and pause;
   individual steps remain available once the worker settles.
6. To recreate another finished or historical game, complete an explicit
   physical reset and start that selection from the beginning.

The host checks the selected source, exact move prefix and FEN before every
advance. It executes the recorded legal move through the same physical planner,
worker and checkpoint used by ordinary play, including captures, castling and
promotion stand-ins. Autoplay still executes one saved legal move at a time;
it does not teleport to a FEN, reverse moves or run a second serial controller.
Use the existing STOP for an emergency interruption; autoplay pause is a
graceful request to stop at a legal move boundary.

Selection, replay cursor and daily polling state are held in the running host
process. Physical
actions and expected inventory remain durable in the existing checkpoint. An
atomic ownership marker beside the checkpoint, `<checkpoint-stem>.replay.json`
(for example, `game.replay.json` beside `game.json`), preserves the replay's host
game identity across restarts. It prevents ordinary play or a remote session
from taking over that saved replay. A completed physical reset creates a new
host game identity and releases that ownership.

After a host restart, the saved replay remains bound but its in-memory source
selection and cursor cannot resume. Inspect/recover any saved action, then
use a confirmed daily switch or complete an explicit reset before starting a
finished/historical replay. Reopening a page or
refreshing a source never resumes motion. Preserve the ownership marker with
the physical checkpoint and serial log. Automatic `before-reset` checkpoint
archives include the matching marker. When manually copying or archiving a
replay checkpoint, retain its matching marker with the copied checkpoint's stem;
checkpoint JSON alone does not contain that ownership guard.

STOP, timeouts, faults and unknown outcomes use the existing recovery gates.
The board has no piece sensors: a committed move records expected delivery,
and uncertain motion requires local inspection before continuing.

## Historical starter library and provenance

The curated library is stored in
[`historical_games.json`](../computer/chess_table_host/historical_games.json).
It contains complete, standard-start PGNs with factual move scores, stable IDs,
players, dates, results and links to their Chess.com references. Descriptions
are original summaries; annotations and alternate lines are omitted. Unknown
date components remain `??` rather than being guessed.

| Game | Date | Moves / plies | Chess.com reference |
| --- | --- | --- | --- |
| Anderssen–Kieseritzky, The Immortal Game | 1851-06-21 | 23 / 45 | [Sam Copeland's game article](https://www.chess.com/blog/SamCopeland/the-immortal-chess-game-every-move-explained-anderssen-vs-kieseritzky-1851) |
| Morphy–Duke Karl / Count Isouard, The Opera Game | 1858, month/day unspecified | 17 / 33 | [Opera Game](https://www.chess.com/terms/opera-game-chess) |
| Byrne–Fischer, The Game of the Century | 1956-10-17 | 41 / 82 | [Game of the Century](https://www.chess.com/article/view/game-of-the-century-chess) |

The scores were checked against the main lines in the game viewers on those
pages and parsed move by move with `python-chess`. All three end in the
recorded checkmate. These are historical games referenced on Chess.com, not
games retrieved from Chess.com account archives. PubAPI has no documented
historical master-game search endpoint; broader historical coverage requires
additional curated, attributed PGNs or a separately licensed data source.

## Upstream references

The host uses the documented player-game endpoints:
`/pub/player/{username}/games`, `/games/archives`, and `/games/{YYYY}/{MM}`.
Chess.com documents delayed publication, HTTP cache validators and rate limits
for concurrent requests in the
[PubAPI reference](https://www.chess.com/news/view/published-data-api).
Keep requests serial, respect response caching and surface upstream failures;
an unavailable response must not be mistaken for a verified empty game list.
No Chess.com login credentials are needed for this public-data integration.

Software verification uses `python -m unittest discover -s tests -q` from
`computer/`. Simulated replay and PGN validation do not establish physical
acceptance; attended table trials and preserved checkpoints are still required.

With the existing geometry fixture, 18 mm moving/standing footprints, zero
additional margin and `allow_tight_routes=True`, simulated routing completed the
Immortal Game's 45 plies, including nine captures, with the correct final FEN.
The Opera Game paused safely after 22 plies, before White's queenside castling
move `e1c1`; the Game of the Century paused after 49 plies, before Black's knight
move `c3d1` required staging. A supported PGN therefore does not guarantee that
every move fits the current physical route planner. Those two games need route
improvements and attended acceptance without bypassing existing safety checks.

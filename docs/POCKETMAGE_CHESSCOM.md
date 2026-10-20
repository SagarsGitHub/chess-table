# PocketMage extension for Chess.com

This is an implementation note for updating the separate
[PocketMage Chess app](https://github.com/SagarsGitHub/pocketmage-chess).
The current branch adds Chess.com browsing and recorded-game replay to the
host GUI. The existing PocketMage `/v1` listener remains as described in
[PocketMage control](POCKETMAGE_CONTROL.md); it does not expose these new GUI
routes yet.

Keep the host responsible for PubAPI access, PGN validation, chess rules,
inventory, routing, checkpoints and the sole Uno connection. PocketMage should
be a compact browsing and control surface. It does not need Chess.com
credentials or firmware-level knowledge of PGN or motor paths.

## Stage 1: browse and preview

Add a Chess.com menu with three lists: **Daily games**, **Finished games**, and
**Historical games**. Default the account to `ofroni` and persist a changed
username. Use short rows showing opponent/title, color, result or side to move,
and date. On a small display, choose an archive month first and search only that
month. Start the historical list with the three bundled classics in
[Chess.com integration](CHESSCOM.md#historical-starter-library-and-provenance).

Add host-owned `/v1/chesscom/...` browse/selection routes before enabling these
screens on the device. These are proposed routes, not available endpoints.
Reuse the host client and source validation behind the GUI's
`/api/chesscom/daily`, `/archives`, `/finished`, `/historical` and `/selection`
operations; do not make the device scrape Chess.com pages. Use bounded JSON
responses and server-side pagination if the archive outgrows the device's RAM.
Treat all returned names and descriptions as display text.

Selecting a row should open a preview with player names, source link, date,
result, recorded move count and selected position. Show a clear distinction
between the selected source and the game currently on the physical board.
Switching between previews must remain read-only. Display the host fetch time
and that daily data can be delayed; do not present cached deadlines as a live
clock. Preserve the previous successful list when a refresh fails and mark it
stale instead of showing a misleading empty list.

## Stage 2: switch daily games and control recorded replay

Add explicit **Set daily game**, **Start replay**, **Next recorded move**,
**Autoplay** and **Pause** commands after the device transport has durable
receipts. A daily row should offer **Set on table**, with a confirmation that
the host will reset the board, recreate the selected snapshot and follow
published updates. The GUI's current daily switch uses
`POST /api/chesscom/daily/switch` with `selection_token` and `confirmed:true`;
give the proposed device command matching source, host-game and request-ID
fences. It must refuse a busy table, remote-owned game, pending action or
unresolved uncertainty. Save the switch intent before submission and track the
entire reset/replay outcome through one durable operation receipt.

For finished and historical games, starting requires an inspected, unbound
standard-start board. If another game is present, guide the user through an
explicit reset and poll its durable receipt before binding the replay. Add a
delay setting from 0.1 to 3600 seconds per half-move, persist that preference,
and send it with the selected source and current host-game/FEN/ply fence when
starting autoplay. **Pause** should finish the current recorded move, then
return to step controls. Keep STOP separate and always reachable for an
emergency interruption.

The current `/v1/session/reset` is restricted to the matching PocketMage-owned
session. It cannot be assumed to reset a host-GUI-owned Chess.com replay.
Extend the host ownership and reset contract deliberately, with a matching
durable reset receipt, or direct the user to the existing local reset controls
until that extension exists. Never clear a checkpoint to make ownership match.

The GUI already uses selection tokens plus host-game/FEN/ply fences. For the
device, make the replay contract durable before use: bind an immutable source
ID, selected PGN digest, source kind and replay ply into the checkpoint; add
stable device session/request IDs and receipt lookup. Save each complete
device intent before sending it. The host must atomically register its receipt
before motion and validate all of the following under the shared controller
lock:

- Expected host game and source/session ownership match.
- The selected PGN identity and current replay prefix match the saved binding.
- Expected FEN and ply match the committed board.
- There is no conflicting move, reset, pending action or reconciliation gate.
- The requested move is exactly the next recorded legal move.

A retried request with the same ID and payload should return the saved receipt
without moving again; changed payloads with a reused ID must fail. Poll the
existing request after a timeout or disconnect. Never generate a replacement
move or infer successful motion from a local animation. After reboot, show the
saved intent, refresh status and offer explicit recovery/resume only once the
host's durable contract supports it. The GUI's current in-memory selection and
cursor are insufficient for automatic device resume. Its separate
`<checkpoint-stem>.replay.json` marker preserves ownership after a restart and
blocks other sessions from taking over; it does not store a resumable PGN or
device request receipts. Preserve this marker alongside the checkpoint and
serial log. Automatic `before-reset` archives retain the marker; manual copies
of a replay checkpoint need a matching marker with the copied checkpoint's stem.

Replay always proceeds from the standard setup. Rewinding, jumping to a FEN, or
switching the physical game means an explicit reset followed by forward replay.
The host owns autoplay and daily catch-up; the device must not create a parallel
timer that submits successive move requests. Disable conflicting controls
while a switch, reset or autoplay worker runs. Enable step controls only after
fresh status shows that the previous move is committed. An undelivered network
STOP must be shown as undelivered. Unknown phases or unknown outcomes disable
motion controls. Only local inspected recovery may reconcile uncertain physical
movement.

## Stage 3: daily synchronization and future connected play

For the active daily game, show the source account/game ID, recorded position,
whose turn the snapshot reports, the last successful fetch, the next check and
any attention state. The host checks every 30 minutes while it is running and
mirrors only newly appended legal recorded moves after proving the same game
ID, unchanged exact prefix and matching committed board. Add **Check now** using
the corresponding proposed device route; the current GUI command is
`POST /api/chesscom/daily/check` with `{}`. It may trigger physical motion, so
show the active game and pending operation clearly.

Display shorter/divergent histories, a missing game and validation failures as
attention states with automatic continuation disabled. Refreshing the browse
list alone never replaces the active source or moves pieces. A host restart
does not resume its polling state; preserve the device intent and offer an
explicit confirmed daily switch after inspection/recovery. This is a host
runtime schedule, so the app should not create a separate Codex automation or
claim checks will occur while the host is closed.

PubAPI is read-only and may lag. **Next recorded move** replays a move already
in the published score; it does not play the user's next turn on Chess.com.
Do not expose Stockfish, hints, evaluations, engine suggestions or automatic
move choice for an in-progress daily game. A real connected-board experience
would require separately approved Chess.com connected-board/OAuth capabilities,
move submission permissions and a new synchronization/recovery design. It is
outside this PubAPI stage. See Chess.com's
[official PubAPI reference](https://www.chess.com/news/view/published-data-api).

## Verification before device release

Exercise preview selection among `ofroni` daily games, month selection, empty
archives, historical search, upstream failures and stale data. Then test
confirmed reset/replay switches, a 30-minute check with appended moves,
unchanged snapshots, shorter/divergent histories, missing games, delayed
responses and busy-table checks. Cover autoplay delay boundaries, graceful
pause, reset ownership, source replacement, stale FEN/ply/token rejection,
duplicated requests, disconnects before/after submission, host/device restart,
STOP and uncertain carries. Include captures, castling and promotion stand-ins. Run host
tests and the PocketMage app's build/tests, followed by attended device/table
trials. Preserve physical checkpoints and serial logs; a simulated success is
not evidence that the sensorless board delivered a piece.

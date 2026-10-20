# Roadmap

The physical game, board reset, and inspected recovery are accepted after a 126-ply GUI draw and return of all 32 pieces. New routing refinements still need attended physical trials. Electrical reset diagnosis remains deferred.

## Accepted work

- Python owns rules, Stockfish, routing, expected inventory, and durable checkpoints; the Uno owns bounded motor and magnet execution.
- Framed serial commands, completion IDs, STOP, fault/reboot detection, measured geometry, and perimeter parking are implemented and tested.
- GUI and Aina play support manual moves and Stockfish, with shared ownership, position fences, and recoverable receipts.
- [PocketMage control](POCKETMAGE_CONTROL.md) adds an opt-in trusted-LAN API for manual play, Stockfish with either color, self-play, STOP, and durable physical-reset requests through the same host.
- [Chess.com integration](CHESSCOM.md) adds daily switch/reset commands and 30-minute update checks, monthly finished-game search, and a curated historical library with step controls and timed autoplay. [PocketMage support](POCKETMAGE_CHESSCOM.md) is planned; physical replay acceptance remains pending.
- Short clear rank/file/diagonal carries now use direct one- or two-square paths. Longer or obstructed moves use grid routing, blocker staging, or checked within-square offsets.
- Software checks cover captures, castling, en passant, promotion stand-ins, reset, and interrupted-motion recovery. The Uno retains measured travel limits and the 12-waypoint cap.

## Physical evidence and saved runs

Keep machine-specific run names, deployment history, and inspection notes in the
private project folder. Preserve each checkpoint and serial log in
`computer/runs/` with its live geometry and physical observations. Successful
runs do not resolve uncertainty in a different checkpoint; inspect and reconcile
every interrupted or uncertain action before reopening that game.

## Immediate next actions

1. Run attended direct carries, crowded detours, blocker staging, offset bundles, and castling. Inspect pickup, neighbor contact, and delivery; measure the 6.25/5.625 mm release extensions against actual landing.
2. Record promoted-pawn movement/capture, interrupted-capture recovery after victim parking, and remaining parking-slot/board-region coverage.
3. Run attended PocketMage trials for both colors, self-play, captures/promotion, physical reset, STOP, disconnection/reboot, and local inspected recovery. Continue to preserve `computer/runs/` and record physical outcomes privately.
4. When electrical diagnosis resumes, capture relay, magnet supply/current, wiring, and serial evidence around the reported release reset ([issue #1](https://github.com/SagarsGitHub/chess-table/issues/1)).

The board has no piece sensors. Expected occupancy, route costs, and a matched `DONE` do not prove physical delivery or clearance. See [routing](ROUTING.md), [protocol](PROTOCOL.md), and [recovery](QUICK_START.md#pause-recover-and-reset).

## Later work

- Laser-etched replacement glass: measured outline/holes, mirrored SVG/DXF, printed alignment proof, and replacement-board test fit.
- Completed Chess.com replay: validated PGN, source identity and result, restart without duplicate moves, then attended physical acceptance.
- Live Chess.com games: official connected-board/OAuth access, clocks and network recovery, and engine-free human play.

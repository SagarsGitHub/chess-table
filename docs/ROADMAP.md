# Roadmap

The physical game, board reset, and inspected recovery are accepted after a 126-ply GUI draw and return of all 32 pieces. New routing refinements still need attended physical trials. Electrical reset diagnosis remains deferred.

## Accepted work

- Python owns rules, Stockfish, routing, expected inventory, and durable checkpoints; the Uno owns bounded motor and magnet execution.
- Framed serial commands, completion IDs, STOP, fault/reboot detection, measured geometry, and perimeter parking are implemented and tested.
- GUI and Aina play support manual moves and Stockfish, with shared ownership, position fences, and recoverable receipts.
- Short clear rank/file/diagonal carries now use direct one- or two-square paths. Longer or obstructed moves use grid routing, blocker staging, or checked within-square offsets.
- Software checks cover captures, castling, en passant, promotion stand-ins, reset, and interrupted-motion recovery. The Uno retains measured travel limits and the 12-waypoint cap.

## Physical evidence and saved runs

| Checkpoint in `computer/runs/` | Recorded result |
| --- | --- |
| `physical-smoke-20261005-013125.json` | 16 plies, four parked pieces, 20 completed carries. Controller boot notices prevent a reset-free reliability claim. |
| `self-play-physical-20261005-020112.json` | 155 plies and 181 completed carries. **The next `h5h4` carry remains `in_flight` after STOP; inspect and reconcile it before resuming.** |
| `gui-physical-20261005-152550.json` | Accepted 126-ply draw and recovered reset. All pieces home; the played game is preserved in its pre-reset archive. |
| `chess-physical-20261011-171904-76335707.json` | Aina `d2d4` and Stockfish `g8f6` completed, including temporary g7-pawn staging; the user confirmed delivery and undisturbed neighbors. |

Successful runs do not resolve uncertainty in a different checkpoint. Keep each checkpoint, serial log, live geometry, and physical observations together.

## Immediate next actions

1. Run attended direct carries, crowded detours, blocker staging, offset bundles, and castling. Inspect pickup, neighbor contact, and delivery; measure the 6.25/5.625 mm release extensions against actual landing.
2. Record promoted-pawn movement/capture, interrupted-capture recovery after victim parking, and remaining parking-slot/board-region coverage.
3. Reconcile the old 155-ply checkpoint before reopening it. Continue to preserve `computer/runs/`.
4. When electrical diagnosis resumes, capture relay, magnet supply/current, wiring, and serial evidence around the reported release reset ([issue #1](https://github.com/SagarsGitHub/chess-table/issues/1)).

The board has no piece sensors. Expected occupancy, route costs, and a matched `DONE` do not prove physical delivery or clearance. See [routing](ROUTING.md), [protocol](PROTOCOL.md), and [recovery](QUICK_START.md#pause-recover-and-reset).

## Later work

- Laser-etched replacement glass: measured outline/holes, mirrored SVG/DXF, printed alignment proof, and replacement-board test fit.
- Completed Chess.com replay: validated PGN, source identity and result, restart without duplicate moves, then attended physical acceptance.
- Live Chess.com games: official connected-board/OAuth access, clocks and network recovery, and engine-free human play.

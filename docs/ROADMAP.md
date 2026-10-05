# Roadmap

The goal is a full physical game with perimeter parking and recoverable motion. The user accepts the physical game, board reset, and fault recovery after a 126-ply GUI draw and a recovered return to all 32 starting pieces. Longer runs work overall. Long crowded horizontal carries can disturb off-center neighbors; the host now favors detours, pending a loaded trial. The PTFE trial is closed. Electrical reset diagnosis remains deferred.

| Stage | Status and remaining evidence |
| --- | --- |
| 0. Foundation | Complete: integrated repo retains the reference history, firmware snapshot, provenance, and host/Uno ownership. |
| 1. Hardware reliability | Deferred: diagnose the reported magnet-release reset and verify relay supply, suppression, grounding, and limit wiring with logs ([issue #1](https://github.com/SagarsGitHub/chess-table/issues/1)). |
| 2. Serial link | Complete in software and simulation: framed commands, capabilities, IDs, ACK/completion/fault handling, STOP, timeouts, and reboot detection. |
| 3. Motion and parking | Host/Uno route and simulator matrices pass; longer loaded runs work overall. Physical trial of crowded-horizontal detours and all-region/all-slot coverage remain ([issue #2](https://github.com/SagarsGitHub/chess-table/issues/2)). |
| 4. Physical game and reset | Accepted by user: 126-ply game, reset to 32 starting pieces, and recovery. Promotion stand-in over later moves/capture and interrupted-capture specifics remain unrecorded edge cases. |

Run from `C:\Users\sagar\Documents\Coding\chess-table` (`/mnt/c/Users/sagar/Documents/Coding/chess-table` in WSL). The [CLI](STAGE4_TEST_GAME.md) and [GUI](GUI_GAME.md) runbooks have WSL, USB, Stockfish, and recovery commands. The host performs `HOME` or `REF`, reads live `GEOM`, and sends `MAP`; no separate Serial Monitor preparation is needed.

## Physical progress recorded 2026-10-05

| Saved run | Evidence and caveat |
| --- | --- |
| `computer/runs/physical-smoke-20261005-013125.json` | 16 plies, four parked pieces, 20/20 `DONE CARRY` replies and no controller error. Four `!BOOT` notices span connection sessions, so this does not establish reset-free running. A copied-state knight preflight required the tight-route fallback; the physical checkpoint was unchanged. |
| `computer/runs/self-play-physical-20261005-020112.json` | 155 plies, 24 parked pieces, 181 completed carries, and 17 clearance overrides. Ply 156 (`h5h4`) remains `in_flight` after STOP. **Inspect and reconcile its piece and magnet position before resuming this file.** |
| `computer/runs/gui-physical-20261005-152550.json` | 126-ply draw, 20 parked pieces. Reset first found a queen/bishop/rook exit cycle during preflight; interior staging then enabled completion. Its log records 32 reset carry requests, 31 `DONE CARRY` replies, one `ERR MOTION`, reboot, and reconciliation. The saved state has all 32 pieces home and parking empty. The user accepts this game, reset, and recovery. The fault's electrical cause is unverified. |

Software evidence includes 141-ply and 228-ply Stockfish self-play simulations, with no route or controller simulation failures. Neither simulation establishes physical promotion or electrical reliability.

## Physical refinements

- Diagonal grid legs and distance/turn-aware routing replaced circuitous paths. The host requires Uno `CARRY_DIAG`; software tests show straight `c1h6` and `e2g4` paths while occupied centers remain blocked.
- Loaded motion uses 50 mm/s at 400 mm/s², or 30 mm/s at 250 mm/s² on horizontal grid legs next to occupied squares. Compiled motor simulation verified both profiles; the user reports longer physical runs work overall.
- Board reset preflights the full return, checkpoints every transfer, and creates a new game only after all 32 pieces are home. Direct returns favor bishops and knights, then other back-rank pieces, then pawns. When parking exits form a cycle, an interior square can temporarily stage a blocker. Simulated reset plans improved from 15 to 12 transfers on the 16-ply checkpoint and 33 to 32 on the 155-ply checkpoint. The recovered 126-ply physical reset is accepted.

The ferrite rod under each piece is the glass contact point. PTFE dry lube did not help and is discontinued. If friction work resumes, test one piece with a thin skid or stable three-pad base, measuring slide force, magnet grip, rocking, and glass marks before changing the set. Added thickness may weaken magnetic coupling.

At 31.25 mm pitch, two centered 18 mm standing pieces leave a 13.25 mm gap, already smaller than the 18 mm moving footprint. Off-center pieces narrow it further. The host now adds cumulative cost to crowded horizontal edges, favoring a detour or shorter exposed passage while preserving travel and waypoint limits. Replanning a logged `h3` to `c1` carry replaced a 93.75 mm two-sided horizontal leg with a six-waypoint route without one. This is software evidence; the new route still needs a loaded trial.

## Full-game GUI

The GUI and CLI share the same game runner, checkpoints, routing, Stockfish, and serial owner. The GUI supports human/Stockfish, manual, and self-play games; displays expected board and P00–P31 parking locations, promoted-pawn stand-ins, motion, and faults; and offers STOP, reconciliation, and midgame board reset. Human moves are entered explicitly because the table has no sensors. A matched `DONE CARRY` advances expected state without a routine confirmation prompt.

Simulation covered manual capture/reset, castling, en passant, promotion stand-ins, restart, STOP/reconcile, and Stockfish self-play through game end (151 and 169 plies). The later 126-ply physical GUI game and recovered reset are accepted. Physical promotion through later use/capture, all-slot coverage, and interrupted-capture recovery remain unrecorded. The [GUI runbook](GUI_GAME.md) has launch and recovery steps.

## Planned glass fabrication and Aina integration

| Roadmap item | Planned work | Acceptance evidence |
| --- | --- | --- |
| 1. Laser-etched glass board | Create mirrored, full-size underside artwork and a separate reverse replacement-board template. Include grid/orientation, measured glass datums, and clearly marked drill-hole centers and diameters; `GEOM` does not measure the glass outline or holes. | Versioned 1:1 SVG/DXF files, a printed alignment proof, and a replacement-board test fit before fabrication. |
| 2. Aina and Chess.com games | Add a typed local host control/status API and an [aina_core](https://github.com/SagarsGitHub/aina_core) adapter. The chess host retains rules, inventory, checkpoints, and serial ownership. Begin with read-only Chess.com completed-game retrieval/replay; live move submission requires official OAuth/connected-board access and must not use Stockfish in online human games. | Aina starts/resumes and queries a local game without duplicate moves after restart, surfaces faults for reconciliation, and replays a completed Chess.com game. Live turn/clock/position sync and submission need separate authorized testing. |

## Immediate next actions

1. Try the new route preference on the physical board, starting with logged `h3` to `c1` and other long crowded passages. With neighbors slightly off-center, record the route, contact/displacement, slip, and delivery. Loaded trial evidence is still missing.
2. When convenient, cover a promoted pawn's later move and capture, interrupted-capture recovery after victim parking, and untested parking slots/board regions. Core game/reset/recovery acceptance remains recorded.
3. Before reopening the old 155-ply checkpoint, reconcile its canceled `h5h4` carry and magnet position. Later successful runs do not resolve that saved in-flight state.
4. Keep electrical diagnosis deferred. When resumed, record the relay, magnet supply/current, wiring, and serial log around the reported release reset.

## Fixed promotion rule

There are no spare promotion pieces. The original pawn stays on the board and later moves as the physical stand-in; chess rules and Stockfish use its selected promoted type. Capture parks that pawn. Checkpoints and the GUI retain both physical and legal identities; no piece swap or per-move confirmation is required.

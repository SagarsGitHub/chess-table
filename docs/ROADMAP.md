# Roadmap

The goal is a full physical game with perimeter parking and recoverable motion.
The user accepts the physical game, reset, and recovery after a 126-ply GUI draw
and return of all 32 pieces. New crowded-motion and delivery refinements still
need loaded trials; electrical reset diagnosis remains deferred.

## Milestones

| Stage | Status and remaining evidence |
| --- | --- |
| 0. Foundation | Complete: integrated reference history, firmware snapshot, provenance, and host/Uno ownership. |
| 1. Hardware reliability | Deferred: diagnose magnet-release resets, relay supply/suppression, grounding, and limit wiring with logs ([issue #1](https://github.com/SagarsGitHub/chess-table/issues/1)). |
| 2. Serial link | Complete in software/simulation: framed commands, IDs, capabilities, ACK/completion/fault handling, STOP, timeouts, and reboot detection. |
| 3. Motion and parking | Software matrices pass and longer loaded runs work overall. Crowded detours, reversible offsets, and all-region/all-slot physical coverage remain ([issue #2](https://github.com/SagarsGitHub/chess-table/issues/2)). |
| 4. Physical game and reset | Accepted: 126-ply game, reset, and recovery. Later promoted-pawn use/capture and interrupted-capture recovery remain unrecorded. |

Run from the authoritative checkout, `C:\Users\sagar\Documents\Coding\chess-table`
(`/mnt/c/Users/sagar/Documents/Coding/chess-table` in WSL). Use the
[quick start](QUICK_START.md), [CLI runbook](STAGE4_TEST_GAME.md), or
[GUI runbook](GUI_GAME.md). The host handles `HOME`/`REF`, live `GEOM`, and `MAP`.

## Physical progress (recorded 2026-10-05)

| Saved run | Evidence and recovery status |
| --- | --- |
| `computer/runs/physical-smoke-20261005-013125.json` | 16 plies, four parked pieces, 20/20 `DONE CARRY`, no controller error. Four connection-session `!BOOT` notices mean this is not reset-free evidence. |
| `computer/runs/self-play-physical-20261005-020112.json` | 155 plies, 24 parked pieces, 181 completed carries, 17 clearance overrides. **Ply 156 (`h5h4`) remains `in_flight` after STOP. Inspect and reconcile its piece and magnet position before resuming this file.** |
| `computer/runs/gui-physical-20261005-152550.json` | Accepted 126-ply draw and reset: 20 parked pieces; interior staging resolved a parking-exit cycle. Reset logged 32 carry requests, 31 completions, one `ERR MOTION`, reboot, and reconciliation. Saved state has all 32 pieces home and parking empty; electrical cause is unverified. |

Stockfish self-play simulations reached 141 and 228 plies without route/controller
failures. GUI simulations also covered capture/reset, castling, en passant,
promotion, restart, STOP/reconcile, and game end. These establish no physical
promotion or electrical reliability.

## Constraints and refinements awaiting physical evidence

- **Expected state and recovery:** the board has no sensors. GUI and CLI share
  one runner, serial owner, and checkpoints. Matched `DONE CARRY` or
  `DONE CARRY_OFFSET` advances expected occupancy; it cannot verify delivery.
  Inspect/reconcile interrupted or uncertain actions before another move, and
  preserve `computer/runs/` checkpoints and serial logs.
- **Routing:** measured travel bounds and the Uno's 12-waypoint cap always apply.
  Detour costs and tight-route retries are routing choices, not physical clearance
  guarantees. At 31.25 mm pitch, centered 18 mm pieces leave only 13.25 mm between
  them; off-center neighbors narrow it further. Diagonal routes require
  `CARRY_DIAG`. Loaded profiles are 50 mm/s at 400 mm/s², or 30 mm/s at 250 mm/s²
  on crowded horizontal grid legs.
- **Delivery extension:** board-square carries and `OFFSET` keep the magnet on
  0.20 pitch beyond the nominal target: 6.25 mm at measured pitch. If that exceeds
  travel bounds, retry at 5.625 mm; reject if still outside. Parking deliveries have no
  extension, and encoded endpoints/inventory stay nominal. Host and firmware
  preflight the extra segment: firmware checks 18 mm clearance from nominal `MAP`
  centers, and the host also checks displaced pieces. The user reports a favorable first trial;
  quantitative centering and longer-run evidence remain pending.
- **Crowded moves and castling:** plans can stage/restore a blocker or offset up
  to two standing neighbors within their squares. Castling uses rook
  `OFFSET OUT`, king carry, then direct `CARRY_OFFSET` from the rook's corner;
  a nearby pawn can be staged. Every action is preflighted/checkpointed.
  Crowded knight openings, midgame moves, capture-parking legs, and these new
  offset/castling plans are software-tested; loaded acceptance remains pending.
- **Reset:** preflight the full return and create a new game only after 32 pieces
  are home. Retrievability comes first; safe returns favor rooks, knights,
  bishops, kings/queens, then pawns, with staging or reversible offsets as needed.
  The earlier recovered physical reset is accepted; the new order/offsets need
  a loaded trial.
- **Promotion:** no spare pieces. The pawn remains the physical stand-in for its
  selected legal type and is parked if captured. Checkpoints/GUI preserve both
  identities; no replacement or routine per-move confirmation is required.
- **Friction:** the ferrite rod contacts the glass. PTFE dry lube did not help and
  is discontinued. If revisited, trial one thin skid or stable three-pad base;
  measure slide force, grip, rocking, and glass marks. Thickness may weaken grip.

## Planned glass fabrication and remote games

| Roadmap item | Work and acceptance evidence |
| --- | --- |
| 1. Laser-etched glass board | Mirrored 1:1 underside SVG/DXF and reverse replacement template: grid/orientation, measured glass datums, drill centers/diameters. `GEOM` does not measure glass outline or holes. Require a printed alignment proof and replacement-board test fit before fabrication. |
| 2. Aina local command path | Opt-in authenticated loopback API and typed [aina_core](https://github.com/SagarsGitHub/aina_core) adapter. Host retains rules, inventory, checkpoints, Stockfish, and serial ownership. Acceptance covers manual/Stockfish with either human color, expression resolution, status/stop, duplicate/restart replay, stale/altered/wrong-game rejection, and local fault recovery. Remote reset/reconcile remains unavailable. See [the contract](REMOTE_AINA.md#local-api-contract) and status below. |
| 3. Completed Chess.com replay | Read-only PubAPI; validated standard-chess PGN bound to source game ID, move prefix, and result. Require simulated replay/restart without duplicate moves and preservation of the source result, then an attended physical replay after loaded-motion trials. |
| 4. Live Chess.com games | Obtain official connected-board/OAuth access; define turn, clock, position, submission, and manual fallback for unroutable moves. Use an engine-free online human mode. Acceptance covers provider versus physical completion, network loss, stale/duplicate events, clock delay, and recovery; no Stockfish or suggestions in ongoing human games. |

## Aina status

**Maintenance:** typed request/session
modules separate validation and persisted policy from the execution worker.
Schema 6 explicitly migrates checkpoints and validates receipt/resolution
integrity. Aina-bound games reject browser moves; missing Stockfish configuration
returns a structured error without private paths or a new binding. Validation:
**248 host tests**, including HTTP stop/reopen/resume and malformed input;
**14 HTTP tests on Windows**; **56 shared speech cases** with Aina.

**Observed physical acceptance — October 11, 2026:** Render health confirmed
`6479b21`. Windows-origin Aina commands routed through the WSL companion to
launch a physical host and start Stockfish with human White. `D2_D4` resolved
to `d2d4` and its receipt committed; Stockfish replied `g8f6`, staging and restoring
the g7 pawn. All four carries had matched ACK/DONE with no controller error.
The user confirmed the pawn on d4, knight on f6, g7 pawn restored, and neighbors
undisturbed. The schema-6 checkpoint `chess-physical-20261011-171904-76335707.json`
returned to White at ply 2 without pending motion or error. The prior simulated
checkpoint was preserved. Broader loaded trials remain below.

Earlier evidence includes physical `b1c3`/`d7d5` integration on October 10 and
real Stockfish with simulated motors for manual mode, both human colors, and
request replay. Private request evidence remains outside the repo; setup and
recovery are in [REMOTE_AINA.md](REMOTE_AINA.md).

## Immediate next actions

1. Continue attended loaded trials with `CARRY_OFFSET` and `OVER20`: three-action
   kingside and five-action crowded castles; staged midgame moves; one-/two-neighbor
   offset bundles in play/reset; `h3`→`c1` detour with slightly off-center neighbors.
   Inspect pickup/contact/staging/delivery after each action and measure nominal
   versus actual landing after 6.25/5.625 mm extensions.
2. Record later promoted-pawn moves/capture, interrupted-capture recovery after
   victim parking, and untested parking slots/board regions.
3. Reconcile the old 155-ply checkpoint's canceled `h5h4` carry and magnet before
   reopening it. Later successful games do not resolve its saved uncertainty.
4. Keep electrical diagnosis deferred. When resumed, capture relay, magnet
   supply/current, wiring, and serial evidence around the reported release reset.

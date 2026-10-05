# Architecture and ownership

## Guiding boundary

The computer owns **what should happen** in a game. The Arduino owns **how to execute one bounded physical operation safely**. The table will not have physical piece sensing. During ordinary play, a matched `DONE CARRY` advances the computer's expected position without an operator prompt after every transfer. A controller fault, timeout, reset, or other uncertain outcome pauses play for physical reconciliation.

| Concern | Computer program | Arduino firmware |
| --- | --- | --- |
| Chess rules | Maintain authoritative `python-chess` board and legal moves, including captures, castling, en passant, promotion, game end. | No rule engine. Reject malformed or unsupported physical commands. |
| Piece and parking inventory | Track the legal piece type separately from the physical piece occupying each square or slot, including promoted pawns used as stand-ins. Decide which free slot receives a capture. Persist/reconcile game state. | Keep only compact occupancy or operation state needed for motion guards. No piece identity or slot policy. |
| Motion planning | Convert a legal move into ordered physical operations. Plan collision-aware routes using measured board geometry and known occupancy. Remove a captured piece before carrying the attacker. | Execute one validated route/segment at a time, control X/Y steppers and magnet, enforce travel bounds and active limits, and report progress/faults. |
| Calibration | Request calibration and record the geometry/version used for planning. Require a referenced carriage before dispatch. | Home/reference switches, measure span, map squares and parking coordinates, save compatible geometry in EEPROM. |
| Communications | Open/configure serial, negotiate capabilities, assign command IDs, wait for ACK and completion, time out, record events, and reconcile after uncertainty. | Parse framed commands, reject busy/invalid requests, associate every response with its ID, report boot/fault/completion, and hold a safe state on stop/fault. |
| Player and engine | Accept human UCI input, run Stockfish, and provide optional supervision prompts and game status. | No engine or user account logic. |
| Physical truth | Require manual UCI entry for human moves. Track expected board and slot occupancy from completed operations. Offer optional `--supervised` confirmation during testing; require inspection and reconciliation after uncertain outcomes. | Report switch/motion/magnet state. A completed step count is not a piece-presence measurement. |

## Operation order

1. The computer validates a proposed UCI move against its current board.
2. It creates a logical plan. A capture first moves the victim to an available parking slot; en passant uses the pawn's actual square. Castling requires two carries. On promotion, the pawn moves to the last rank and remains the physical piece while the legal game records the chosen queen, rook, bishop, or knight.
3. A physical route planner checks the current `GEOM` calibration and occupied locations, then selects a route within the Uno's 12-waypoint `CARRY` limit. It first seeks a route with measured circular piece clearance. If enabled and that fails, a tight-route fallback may pass closer to occupied pieces while avoiding occupied centers. It still obeys calibrated travel bounds and the waypoint limit. `--strict-clearance` disables the fallback. Loaded hardware clearance remains to be validated.
4. The computer sends one bounded command, waits for an ID-matched completion, and records the outcome. A matched `DONE CARRY` advances the expected physical occupancy automatically in normal mode. Optional `--supervised` mode pauses for operator confirmation. A timeout, reset, stop, or fault marks the physical state **uncertain**; software must not advance the authoritative game position automatically.
5. When all operations complete, the computer commits the chess move and updated parking inventory together. A discovered physical discrepancy pauses play for reconciliation before another move.

```mermaid
flowchart LR
    Input[Player or engine move] --> Game[Computer: rules and state]
    Game --> Plan[Computer: capture and parking plan]
    Plan --> Route[Computer: physical route]
    Route --> Link[Versioned serial transport]
    Link --> Motion[Arduino: motors, magnet, limits]
    Motion --> Link
    Link --> Verify[Completion or fault reconciliation]
    Verify --> Game
```

## Parking geometry

The firmware maps 32 perimeter grid intersections: 00–08 along the bottom, 09–17 along the top, 18–24 on the left, and 25–31 on the right. Each slot can hold one piece. A `CARRY` route can bring an interior captured piece to a reachable slot through grid intersections; the host must account for other pieces and the magnet's real clearance. The firmware bitmap represents commanded occupancy for 64 squares plus 32 slots, not sensed occupancy or identity.

## Promotion rule

There are no spare promoted pieces. A pawn that reaches the last rank stays on the board as the physical stand-in for its chosen legal piece. The computer uses that promoted type for move legality, Stockfish, and game end, while the motion planner continues to carry the pawn. If the promoted piece is captured, the captured physical pawn goes to parking and the stand-in ends. Promotion never takes a replacement from parking or asks the operator to exchange pieces. Checkpoints and displayed game state distinguish the legal piece from its physical pawn stand-in through later moves and recovery.

## Recovery contract

EEPROM retains measured geometry, not the carriage's live position or the game inventory. After power loss, motor stop, or interrupted carry: release the magnet safely, reference the carriage, inspect/reconcile every moved piece and occupied parking slot, including any promoted pawn stand-in, restore the computer and firmware maps to the same physical position, and only then resume. A software-only replay is unsafe because the firmware cannot tell whether pickup or release succeeded.

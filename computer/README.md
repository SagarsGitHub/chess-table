# Computer-side move planner

`chess_table_host` is the first host-side component. It uses `python-chess` to
check UCI moves and converts each legal move into ordered **logical** transfers
between board squares and perimeter parking slots. Slot IDs are `0..31`; the
firmware's internal `PieceMap` represents the same slots as IDs `64..95`.

Install `requirements.txt`, then run `python -m unittest discover -s tests -v`
from this directory.

```python
import chess
from chess_table_host import ParkingInventory, plan_move

board = chess.Board()
parking = ParkingInventory.empty()
plan = plan_move(board, "e2e4", parking)
for action in plan.actions:
    print(action)
# Commit board and parking state only after each physical action succeeds.
```

Capture plans park the victim before moving the attacker. En passant parks the
pawn from the square behind the destination. Castling moves the king and rook
in separate steps. Promotion defaults to a `ManualReplacement` action that
requires an operator to substitute a piece and confirm completion. With
`promotion_policy=PromotionPolicy.PARKED_PIECE`, a matching same-color piece
must already occupy a parking slot; the pawn is parked and the replacement
piece is retrieved. Capturing moves and parked-piece promotion both require an
empty slot, so planning fails explicitly if storage is full.

## Current limits

- This package does not calculate obstacle-free magnet paths or drive motors.
  The current firmware snapshot can park/retrieve only from an adjacent edge
  square, so most descriptive board-to-parking transfers still need a route
  planner and firmware protocol work.
- A `MovePlan` predicts state; callers must execute actions in order, verify
  each physical action, and persist the new state only after success. It has
  no camera/sensor reconciliation or recovery after a partial move.
- Manual promotion requires a spare physical piece and manual handling of the
  removed pawn. The parking inventory does not account for those manual items.
- The planner covers standard chess, not Chess960 castling. It records piece
  type and color per slot, not the identity of identical pieces.

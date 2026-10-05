# Computer-side move planner

`chess_table_host` uses `python-chess` to check UCI moves and converts each legal
move into ordered **logical** transfers between board squares and perimeter
parking slots. Slot IDs are `0..31`; the firmware's internal `PieceMap`
represents the same slots as IDs `64..95`. The host also plans bounded carry
routes and speaks the Uno's version 1 framed serial protocol.

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

## Carry routing

`plan_transfer_route()` takes one `Transfer` plus the occupied squares and
parking slots **at that action's start**. Its points use the firmware's
half-square coordinates: board centers are odd/odd (`a1=(1,1)`), parking is
on the even-coordinate perimeter, and the route passes through adjacent
even/even grid intersections. Pickup and delivery are included. Straight grid
runs are compressed to turn points. A vacant edge square beside a parking slot
may be used to approach it when adjacent slots are occupied. There can be at
most 84 points; the nominal empty-perimeter matrix of 64 board squares × 32
slots needs at most four. Set `max_waypoints` to a controller's capacity; over-cap routes raise
`RouteTooLongError` and are never truncated.

By default these are **nominal paths**: they avoid occupied piece centers but
do not establish physical clearance. `RouteGeometry` requires measured X/Y
travel spans plus the firmware's end inset and pulses-per-mm scale, along with
square pitch, moving and stationary piece diameters, and a safety margin.
Optional saved X/Y origins override the centered-calibration formula. Read
these machine values with `SerialLink.geometry()` after `home()` or
`reference()`. It mirrors the firmware's inward perimeter clamp and pulse
rounding before its circular-footprint clearance check. Allow a margin for
four-decimal reply rounding, piece shape, centering error, magnet behavior,
and path tracking. An impossible clearance raises
`NoRouteError`.

The default move planner selects the lowest empty slot. Pass a `slot_selector`
callback to choose a reachable one; the callback gets the source square,
piece symbol, and current immutable 32-slot snapshot. For one transfer,
`find_reachable_parking_slot()` tries free slots in order using the supplied
occupancy and geometry. When a move has multiple parking actions, such as a
capture followed by parked-piece promotion, create a fresh
`ReachableParkingSlotSelector(occupied_squares, geometry=...,
max_waypoints=...)` for that `plan_move()` call. It advances the square
occupancy between parking choices; the resulting plan still needs physical
execution and acknowledgement before board state is committed.

## Serial transport

`chess_table_host.serial_link.open_serial(port)` opens a 115200-baud link using
`pyserial`. Call `hello()` to check capabilities, then `home()` or `reference()`
and inspect `status()` and `geometry()`. `save_geometry()` stores a completed
calibration.
After inspecting and reconciling the actual board, `set_map(encode_map(board,
parking))` declares its 96-position occupancy. Plan each `Transfer` against
that action's current occupancy with `max_waypoints=12`, then send
`carry(*encode_carry(route))`. The transport matches command IDs and waits for
both `ACK` and terminal `DONE`/`ERR`; it treats reset, timeout, STOP, and
ambiguous motion faults as requiring physical reconciliation. See
[`docs/PROTOCOL.md`](../docs/PROTOCOL.md) for exact frames.

## Current limits

- General route execution has software and compiled-Uno simulator coverage;
  loaded physical carries and measured clearance are pending the electrical
  reset investigation and hardware acceptance runs.
- A `MovePlan` predicts state; callers must execute actions in order, verify
  each physical action, and persist the new state only after success. It has
  no camera/sensor reconciliation or recovery after a partial move.
- Manual promotion requires a spare physical piece and manual handling of the
  removed pawn. The parking inventory does not account for those manual items.
- The planner covers standard chess, not Chess960 castling. It records piece
  type and color per slot, not the identity of identical pieces.

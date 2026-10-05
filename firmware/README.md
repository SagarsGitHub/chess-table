# XY chess: saved geometry, occupancy, and full-board tests

Arduino Uno R3 / PlatformIO firmware for two M415B drivers, TR6x12 actuators,
1/8 microstepping, four normally-open limit switches, and an active-HIGH
magnet relay. Magnet-off travel is **150 mm/s**; carrying is **75 mm/s**.
No motion starts at boot.

The gap and knight routes passed the user's physical tests. The user explicitly
accepts neighboring pieces being displaced, provided they remain recoverable by
the magnet. The firmware permits these routes without a diameter-clearance veto.
Motion completion confirms commanded steps, not physical pickup or delivery.

## First full-board test

1. Upload, open Serial Monitor at **115200 baud**, and send `h`. Wait for
   `Board ready`. This measures X/Y between both limit switches.
2. Check board alignment with `g a1` and `g h8` (each followed by Enter).
   Send `s` to save the geometry to EEPROM. `q` lists all 64 square centers
   and 32 parking coordinates in millimeters from MIN.
3. Arrange the **normal 32-piece starting position**, with all parking slots
   empty. Send `f` to tell the firmware this is the physical arrangement.
   `f` updates the occupancy map; it does not move or detect pieces.
4. Send **`j` once per leg**, waiting for `Piece motion complete` and inspecting
   the result before the next `j`. There are **36 legs**. Each outward move is
   followed by its return, so every pair starts from the original arrangement.
5. Use `v` to inspect occupancy and `?` for reference/switch status.

The first 16 legs test each of these knight pairs out and back:
`b1-a3`, `b1-c3`, `g1-f3`, `g1-h3`, `b8-a6`, `b8-c6`, `g8-f6`, `g8-h6`.
The remaining 20 legs park and retrieve:

| Square | Parking slot | Location tested |
| --- | --- | --- |
| a1 | 00 | Bottom-left corner |
| h1 | 08 | Bottom-right corner |
| a8 | 09 | Top-left corner |
| h8 | 17 | Top-right corner |
| a2 | 18 | Left edge near rank 2 |
| a7 | 24 | Left edge near rank 7 |
| h2 | 25 | Right edge near rank 2 |
| h7 | 31 | Right edge near rank 7 |
| d1 | 04 | Bottom middle |
| d8 | 13 | Top middle |

Check that the mover stays coupled, reaches its destination, and that displaced
neighbors can still be picked up from their commanded square centers. Restore
and correct the map if a piece fails to arrive. A manual carry, occupancy edit,
or stop cancels the fixture sequence. Restore the starting arrangement and send
`f` to restart it. Rejected commands and magnet-off travel do not advance it.

## Commands

Commands are case-insensitive; spaces/tabs are optional. Send only the command,
without explanatory text. `g`, `d`, `k`, `p`, `u`, and `i` require Enter (CR/LF).
All other commands below are immediate.

| Command | Action |
| --- | --- |
| `h` | Measure both ends of both axes and generate a centered board in RAM |
| `s` | Save the current referenced geometry to EEPROM |
| `r` | Reference X/Y MIN only, using the saved spans and board geometry |
| `t` | Report referenced dimensions and board origin |
| `q` | Print 64 square centers and P00-P31 coordinates; saved geometry is readable before homing |
| `n` | Travel to the next square and stop, beginning at a1 |
| `w` | Travel through all 64 squares in serpentine order, pausing 500 ms |
| `g a1` | Travel to a square with the magnet off |
| `d b1 c3` | Dry knight route; magnet remains off |
| `k b1 c3` | Carry between any valid knight pair; source occupied, destination empty |
| `p a2 18` | Park the piece from a2 in slot 18 |
| `u a2 18` | Retrieve the piece from slot 18 to a2 |
| `f` | Declare 32 starting pieces and empty parking; restart the stepped fixture |
| `e` | Declare all squares and parking slots empty |
| `i b1 1` / `i b1 0` | Mark b1 occupied / empty after manually changing the arrangement |
| `i 18 1` / `i 18 0` | Mark parking slot 18 occupied / empty |
| `v` | Print occupancy: rows 8 to 1, files a to h, then slots 00 to 31 |
| `j` | Execute the next full-board fixture leg; never automatically starts the next leg |
| `o` | Magnet off; during a move also stops both axes; when idle retains the reference |
| `x` | Stop both axes, magnet off, invalidate the live reference even when idle |
| `?` | Help and reference/map/switch status; brief status while busy |

Parking numbers must contain **two digits**, 00 through 31. For example,
`p a1 00` then `u a1 00` tests the bottom-left corner.
Board operations own both motors and the magnet until complete. Busy commands
are rejected. `x` and `o` interrupt even partial or overlong command input.

For a custom fixture, physically arrange the pieces, send `e`, and add every
occupied location with `i`. Example for one piece: `e`, `i d4 1`, `k d4 f5`,
then `k f5 d4`. Do not send `e` on a populated board without rebuilding the map.

## Saved coordinates versus live position

`s` stores switch-to-switch spans, board origins, pitch/step-scale compatibility
information and a checksum. Square and parking coordinates are reconstructed
from that geometry. EEPROM updates happen only on `s`, not after every step or
piece move. Incomplete, corrupt or incompatible saved records are rejected.

After a reset or power cycle, the geometry survives but **the carriage must
still reference MIN**: send `r`. It does not remeasure MAX. Use `h` followed by
`s` after changing the mechanics, board alignment, step scale, pitch, or inset.
`h` generates new geometry in RAM; it does not overwrite the saved record until
`s`. Homing and referencing run with the magnet off.

**A completed knight or parking move retains the live reference.** The previous
firmware's `o` command invalidated it even when already idle; this version keeps
it in that case. `x`, an interrupted move, or a fault still requires `r` or `h`.
If `BOOT: carriage unreferenced; piece map unknown.` appears again after a move,
the controller restarted. That is different from completing a move or sending
idle `o`; record the serial output to distinguish a reset from a printed fault.
The firmware cannot safely infer its live carriage position across a reset.

The reported full-board log reaches `Waypoint 3/3` and then prints `BOOT`
before `Piece motion complete`. Immediately after that waypoint report, the
carry path switches the magnet relay OFF and begins its release dwell. This
confirms a restart during the release interval; the leading hardware suspect
is an inductive switching transient or supply/ground disturbance. The log
alone does not identify which electrical path caused it.

The user reports a 24 V electromagnet with no flyback diode, a USB-powered
Uno, and the relay's low-voltage side supplied from Uno VIN. Check the relay
terminal/model before rewiring: VIN is the Uno's external voltage input, not
its regulated 5 V supply output. For a conventional three-pin **5 V relay
module**, connect VCC to Uno **5V**, GND to Uno **GND**, and IN to **D2**, within
the available USB/board current budget. Other coil voltages or modules with
separate JD-VCC supplies require their own specified wiring. Do not connect
the 24 V magnet supply to Uno VIN, 5V, or D2. See the
[Arduino Uno R3 datasheet](https://docs.arduino.cc/resources/datasheets/A000066-datasheet.pdf).
The magnet's current/wattage and relay model are still needed to select the
diode and confirm the relay supply connection.

To isolate it, turn power off and disconnect the electromagnet load from the
relay contacts, leaving the relay module's control wiring connected. Insulate
the disconnected leads. Power back up, send `r`, wait for `Board ready`, send
`f` and then `j`. This runs the same carry code and relay switching without the
electromagnet load; no piece will be moved. If the reset disappears, investigate
the magnet load/supply wiring and suppression. If it persists, investigate the
relay module, logic supply/reset wiring and USB connection. A magnet-off `d`
route is also useful as a comparison, but does not exercise relay switching.

For a DC electromagnet, check for a suitably rated flyback diode directly
across the magnet terminals: cathode/striped end toward the terminal that is
positive when energized, anode toward the negative terminal. A diode across
the relay module's own coil does not suppress the separate electromagnet
load. Select suppression for the actual load voltage/current. A plain diode
can lengthen magnetic release, so recheck release before continuing piece
tests. See [Omron's DC inductive-load suppression guidance](https://www.ia.omron.com/support/faq/answer/36/faq02804/).

After the reported first leg, the knight should be at a3 if delivery succeeded.
To restart the fixture, manually put it back on b1 and restore any displaced
pieces, reference with `r`, then use `f`. **`f` only declares the piece map;
it does not home the carriage.** Resolve the reset before resuming loaded tests.

Occupancy is a **12-byte RAM bitmap** for 64 squares plus 32 parking slots.
It tracks commanded locations, not piece type, color, or actual displacement.
Loaded moves require an occupied source and an empty destination. The map is
updated after release completes. A stop/fault during a loaded move makes the
map unknown; a reset also starts with an unknown map. After reconciling the
physical pieces, use `f` for the starting arrangement, or `e` plus `i` for a
custom arrangement. Homing alone does not reconstruct occupancy. An idle `o`
and normal completed moves preserve both reference and occupancy.

## Parking geometry and route limits

Parking slots are perimeter **grid intersections**, with each corner counted
once. Orient the board with a1 nearest MIN/MIN, files increasing in +X and ranks
increasing in +Y. In units of one square pitch:

| Slots | Edge | Coordinates before applying the measured inset |
| --- | --- | --- |
| 00-08 | Bottom | (0,0) through (8,0), left to right |
| 09-17 | Top | (0,8) through (8,8), left to right |
| 18-24 | Left | (0,1) through (0,7), bottom to top |
| 25-31 | Right | (8,1) through (8,7), bottom to top |

Only outer coordinates are clamped inward to measured travel, rounding toward
the safe side of the 2 mm inset. `q` reports these actual target coordinates.
A square must share the selected perimeter intersection: for example, a1 can
use 00, 01 or 18; a2 can use 18 or 19. Parking/retrieval follows one diagonal
between that **adjacent edge square** and the slot. Interior-square parking,
travel along occupied perimeter slots, automatic capture routing and complete
board reset are not implemented. The full-board fixture tests 10 representative
slots; all 32 are available for manual adjacent-square tests.

Knight routes retain the physically tested four points: source center,
diagonal half-square entry into a boundary lane, one square along that lane,
and diagonal half-square exit to the destination. b1-c3 uses board-pitch
coordinates `(1.5,0.5) -> (2,1) -> (2,2) -> (2.5,2.5)`.
Occupancy checks apply to source and destination. Occupied neighbors are allowed
along these routes under the user's displacement override.

## Motion and wiring

Pitch stays **31.25 mm**. Measured usable travel is the switch-to-switch span
minus 4 mm. The centered board origin is `(span - 8 * pitch) / 2`, and square
centers are `origin + (index + 0.5) * pitch`. With usable travel
247.085 x 247.963 mm, the example origin from MIN is (0.5425,0.9815) mm and a1 is
(16.1675,16.6065) mm. Only the span between square centers must fit; firmware
never shrinks the pitch silently.

The initial approach runs at 150 mm/s, magnet off. Loaded route segments and
dry knight segments use 75 mm/s. Acceleration is 1000 mm/s^2 for travel and about
667 mm/s^2 for piece motion. Pickup/release settling is 350 ms; intermediate
knight junction dwell is 500 ms. Axis speeds and accelerations are scaled to
segment length. Both queues are submitted together; they are proportional
FastAccelStepper ramps, with pulse rounding and small timing skew.

Homing seeks at 20 mm/s, backs off 3 mm and latches at 2 mm/s. First switch
contact immediately stops motion; contact is then confirmed while stationary.
Searches have distance/time limits. Any active limit during board motion stops
both axes and drops the magnet, including during pickup or release. Open-loop
positions cannot detect missed steps or slip. Software stop commands are not a
hardware emergency stop.

| Connection | X | Y |
| --- | --- | --- |
| M415B PUL | D9 | D10 |
| M415B DIR | D3 | D5 |
| M415B OPTO | 5V | 5V |
| M415B ENA | Unconnected | Unconnected |
| MIN switch NO | D4 | D6 |
| MAX switch NO | D7 | D8 |
| Every switch COM | GND | GND |

All four NO inputs use INPUT_PULLUP: pressed is LOW. Relay IN is D2, active HIGH.
The relay contacts switch the magnet supply. D9/D10 and Timer1 belong to
FastAccelStepper. Configuration is in `include/config.h`.

## Build and verification

```powershell
pio run
pio run --target upload
pio device monitor --baud 115200
```

This build uses **30,474 / 32,256 bytes of flash** and **828 / 2,048 bytes of
static RAM**. Occupancy uses bits, routes use integer half-square points,
fixture pairs live in flash, and the same motion state machine handles knights
and parking. Legacy jog/speed/debug routines remain removed.

The pre-change source is backed up outside the firmware build at
`C:\Users\sagar\Documents\ChatGPT\chess table\tmp\firmware-before-parking`.
See `tests/README.md` for compiled Uno HEX simulations, EEPROM checks, exhaustive
route checks and fault cases. Simulations do not drive the physical table.

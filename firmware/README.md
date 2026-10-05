# Chess table Uno firmware

Arduino Uno R3 / PlatformIO firmware for two M415B drivers, TR6x12 actuators,
1/8 microstepping, four normally-open limit switches, and an active-HIGH magnet
relay. Travel is 150 mm/s with the magnet off; carrying is 75 mm/s. No motion
starts at boot. The older one-letter motion and fixture commands were removed
to fit general routes on the Uno; their implementation remains in Git history.

## Computer link

Use 115200 baud and one ASCII request per line:

```text
@<id> HELLO
@<id> STATUS
@<id> GEOM
@<id> HOME
@<id> REF
@<id> SAVE
@<id> MAP <24-uppercase-hex>
@<id> CARRY <source> <destination> <waypoints>
@<id> STOP
```

IDs are decimal 1..65535 and strictly increase within one boot session. They
do not wrap. A recognized ID gets `@<id> ACK`, then exactly one
`@<id> DONE <verb> ...` or `@<id> ERR <code>`. A pending ID may be resent and
gets another ACK without restarting motion. A completed or older ID gets
`ERR DUPLICATE`. The maximum line payload after `@` is 95 bytes. Startup
emits `!BOOT V=1`; the host must discard its pending request and reconcile
position and occupancy after a reset. Human-readable diagnostic lines may
appear between frames.

`HELLO` reports `V=1 CAPS=STATUS,HOME,REF,STOP,MAP,CARRY,SAVE,GEOM`.
`STATUS` reports `REF`, `BUSY`, `MAP`, and `SAVED` as 0/1 fields.
`GEOM` requires a live reference and reports measured X/Y travel, board
origins OX/OY, configured pitch P, inset I, and pulses/mm PPM in millimeters
with four decimal places. For example, `DONE GEOM X=251.0850 Y=251.9625
OX=0.5425 OY=0.9813 P=31.2500 I=2.0000 PPM=133.3333` (one wire line).
HELLO and GEOM return `ERR BUSY` during motion or calibration, keeping their
long replies out of the switch-monitoring loop.
`HOME` measures both limits on X and Y; `REF` touches the MIN limits only
using saved geometry. Their DONE response is sent only after both axes finish
and the geometry passes validation. `SAVE` writes referenced geometry to
EEPROM. After a reset, use REF or HOME before any carry, and send MAP again:
the live carriage reference and occupancy map are not restored from EEPROM.

`MAP` replaces the firmware's 12-byte occupancy bitmap after physical
reconciliation. Bits 0..63 are a1..h8 in rank-major order; bits 64..95 are
parking slots P00..P31. Within each byte, the lower numbered location is the
least significant bit. For example, the starting 32-piece board and empty
parking is `FFFF00000000FFFF00000000`. MAP is a declaration by the computer;
the Uno has no piece sensors.

`CARRY` uses decimal source/destination IDs 0..95 and 2..12 waypoints,
including pickup and delivery. Each waypoint is two base-17 characters for
half-square X and Y coordinates: `0-9A-G` encode 0..16. For example,
`CARRY 35 64 79680800` moves d5 to P00 via (7,9), (6,8), (0,8), (0,0).
Board square centers have odd X/Y; grid intersections and parking slots have
even X/Y. Diagonal center-to-grid links must be exactly one half-square in
both axes. Grid-to-grid links must be orthogonal. A vacant edge-square center
adjacent to the parking endpoint may be an intermediate waypoint.

Before moving, the Uno checks the complete path, matching endpoints, measured
software bounds, known occupancy, occupied source, empty destination, and
crossings of occupied centers. Parking-to-parking moves are unsupported. It
does not measure physical piece clearance; the host must plan a suitable route
and the loaded route matrix still needs bench validation. `DONE CARRY` is
sent after the destination magnet-release dwell, when the commanded occupancy
map is updated. A motion fault returns `ERR MOTION`; STOP during carry returns
`ERR CANCELED`. Both stop the axes, drop the magnet, and invalidate reference
and occupancy because physical delivery is uncertain. Preflight errors such
as `ERR ROUTE`, `ERR MAP`, `ERR REFERENCE`, `ERR GEOMETRY`, and
`ERR BUSY` do not start a new carry. `ERR SYNTAX` rejects malformed wire
arguments. A standalone unframed `x` remains as a bench stop.

## Motion and limits

Homing seeks at 20 mm/s, backs off 3 mm, and latches at 2 mm/s. The first raw
switch edge stops calibration motion immediately; contact is confirmed while
stationary. Searches have distance and time bounds.

During board travel, limit inputs are sampled only while a motor is moving.
A closure continuous for 3 ms stops both axes and drops the magnet. Brief
pulses during movement and all idle/dwell switch activity are ignored. Each
route segment starts a fresh 3 ms window, so a switch held before movement
still stops it once motion begins. Commanded position and every waypoint must
remain within the measured 2 mm software inset. A software stop is not a
hardware emergency stop.

The board pitch is 31.25 mm. a1 is nearest MIN/MIN; files increase in +X and
ranks in +Y. Parking slots are perimeter grid intersections: P00..P08 bottom,
P09..P17 top, P18..P24 left, and P25..P31 right. Outer grid coordinates are
clamped inward to the measured software inset. Motion is open-loop, so DONE
confirms commanded steps and relay timing, not physical pickup or delivery.
The reported magnet-release reset remains an unresolved hardware issue; see
repository issue #1 before relying on a full loaded game.

| Connection | X | Y |
| --- | --- | --- |
| M415B PUL | D9 | D10 |
| M415B DIR | D3 | D5 |
| M415B OPTO | 5V | 5V |
| M415B ENA | Unconnected | Unconnected |
| MIN switch NO | D4 | D6 |
| MAX switch NO | D7 | D8 |
| Every switch COM | GND | GND |

All four NO inputs use INPUT_PULLUP: pressed is LOW. Relay IN is D2, active
HIGH. The relay contacts switch the magnet supply. D9/D10 and Timer1 belong
to FastAccelStepper. Configuration is in `include/config.h`.

## Build and verification

```powershell
pio run
node tests/machine-sim.cjs
node tests/machine-sim.cjs --restore
node tests/machine-sim.cjs --small-table
```

Run the default test before `--restore`; it saves a simulated EEPROM image.
The compiled Uno build uses **28,698 / 32,256 flash bytes** and
**1,005 / 2,048 static RAM bytes**. Tests execute the real HEX in AVR8js with
simulated GPIO, switches, motors, relay, and EEPROM. They do not drive the
physical table or resolve the reported release reset.

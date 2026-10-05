# Uno firmware

PlatformIO firmware for an Arduino Uno R3, two M415B drivers, TR6x12 actuators at 1/8 microstepping, four normally-open limit switches, and an active-HIGH magnet relay. No motion starts at boot. The host supplies expected occupancy; the Uno has no piece sensors.

| File | Responsibility |
| --- | --- |
| `src/main.cpp` | Startup, homing, and framed command parser. |
| `src/BoardController.cpp` | Measured geometry and declared occupancy. |
| `src/BoardControllerRoute.cpp` | Full-route validation and coordinate conversion before motion. |
| `src/BoardControllerMotion.cpp` | Nonblocking motors, magnet, and faults. |
| `include/BoardCoordinates.h` | Square and parking mapping shared with the host. |

## Computer link

The Uno accepts version 1 framed ASCII requests at 115200 baud: `HELLO`, `STATUS`, `GEOM`, `HOME`, `REF`, `SAVE`, `MAP`, `CARRY`, and `STOP`. See the [serial protocol](../docs/PROTOCOL.md) for frame IDs, replies, bitmap and waypoint encoding, errors, and recovery rules. The maximum request line is 96 bytes including the newline. An unframed `x` remains available as a bench stop.

After a reboot, the host must run `REF` or `HOME`, read live `GEOM`, reconcile the pieces, and send `MAP` before carrying. EEPROM stores measured geometry, not a live carriage reference or occupancy. `HOME` measures both axes; `REF` uses saved geometry and touches the MIN limits. `MAP` declares occupancy without sensing it. `DONE CARRY` follows the destination magnet-release dwell and confirms commanded steps, not physical delivery.

The Uno checks route topology, occupied-center crossings, source/destination occupancy, measured bounds, and the 2–12-waypoint limit before moving. Parking-to-parking carries are unsupported. The host chooses parking slots and plans physical clearance; the firmware validates the command it receives.

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

For CARRY, the 50 mm/s, 400 mm/s² loaded profile applies after pickup. A
horizontal grid-to-grid segment next to any occupied board-square center in
its X span uses 30 mm/s and 250 mm/s². The controller checks its occupancy
map for each segment; it does not sense whether the neighboring piece actually
moved. Vertical and diagonal legs keep the ordinary loaded profile. Empty
carriage travel remains at 150 mm/s and 1000 mm/s²; HOME/REF use their separate
seek and latch speeds. The 15-second watchdog applies to each segment,
including the longest possible grid diagonal or crowded horizontal run.
For a new piece or board geometry, check both loaded profiles with short carries;
the right settings depend on piece/glass friction and magnetic grip.

The board pitch is 31.25 mm. a1 is nearest MIN/MIN; files increase in +X and
ranks in +Y. Parking slots are perimeter grid intersections: P00..P08 bottom,
P09..P17 top, P18..P24 left, and P25..P31 right. Outer grid coordinates are
clamped inward to the measured software inset. Motion is open-loop, so DONE
confirms commanded steps and relay timing, not physical pickup or delivery.
The reported magnet-release reset still has no verified electrical cause;
diagnosis in [issue #1](https://github.com/SagarsGitHub/chess-table/issues/1) is deferred.

## Wiring

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

Run `pio run` and `node tests/machine-sim.cjs` from this directory. The build
must stay within the Uno flash and RAM limits. The compiled-HEX simulator covers
GPIO, motors, relay, switches, and EEPROM; see [tests/README.md](tests/README.md)
for all modes. It does not drive the physical table or resolve the reported
release reset.

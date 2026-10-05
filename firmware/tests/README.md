# Compiled Uno firmware and geometry checks

Tests use the checked-in TR6x12, 1/8 microstep, 31.25 mm pitch, four enabled
NO limits, 150 mm/s travel, and 75 mm/s carrying configuration.

Build and install the pinned AVR8js dependency from the project directory:

```powershell
pio run
npm --prefix tests install
```

Alternatively use `pnpm --dir tests install --frozen-lockfile`.
Run the modes below. The first four are independent and may run in parallel;
run storage modes in the shown order because they share an EEPROM artifact.
Each motion simulation can take several minutes. None uploads or drives motors.

```powershell
node tests/firmware-sim.cjs
node tests/firmware-sim.cjs --faults
node tests/firmware-sim.cjs --small-table
node tests/firmware-sim.cjs --full-board
node tests/firmware-sim.cjs --storage
node tests/firmware-sim.cjs --restore
node tests/firmware-sim.cjs --corrupt
node tests/firmware-sim.cjs --interrupted-save
node tests/firmware-sim.cjs --config-change
```

The simulator executes the actual `.pio/build/uno/firmware.hex`, including
Timer0/Timer1, serial, GPIO, relay and EEPROM. It closes NO switches at modeled
trip positions and measures STEP edges, direction setup, physical pulse
positions, and loaded path deviation on every emitted pulse.

| Mode | Coverage |
| --- | --- |
| Default | Startup, malformed/oversized input, reference requirement, full MIN/MAX homing, 64-square walk, named/zero-distance moves, busy ownership, all eight knight orientations, reversals/corners, 150/75 mm/s profiles, relay settling, retained reference, immediate `o` during carrying |
| `--faults` | Immediate `x` during loaded motion/partial input, limit during carrying, stuck MIN, missing MAX, both ends pressed, homing cancellation |
| `--small-table` | Undersized table never enables board motion or silently shrinks pitch |
| `--full-board` | All 36 fixture legs against a 32-piece occupancy model, map after every delivery, every corner and all four parking edges, unknown/occupied-destination rejection, manual parking/retrieval, occupied-slot rejection, map edits, idle `o` retaining home after every leg |
| `--storage` | Missing-record rejection, real EEPROM write after homing, coordinates after carrying and idle `o`; writes `.pio/storage-test.eeprom` |
| `--restore` | Fresh MCU process reading that EEPROM image, coordinates available before reference, motion blocked until MIN-only reference, no MAX traversal, occupancy unknown after reboot |
| `--corrupt` | Modified EEPROM payload rejected |
| `--interrupted-save` | Missing commit marker rejected |
| `--config-change` | Incompatible step scale rejected even with a valid record checksum |

Normal STEP high/low and direction timing are checked. An immediate stop can
truncate the last high pulse; at most one such pulse per axis is allowed within
5 ms of a stop command or newly triggered limit. Separate assertions check that
queued pulses do not continue afterward. The harness waits for complete serial
status output before checking the retained-reference diagnostic.

The pulse-perfect model does not simulate missed steps, backlash, magnetic
coupling, slip, or neighboring-piece displacement. The user has accepted the
observed displacement on the real board; full-board physical acceptance remains
an inspection of coupling, landing, and subsequent pickup of displaced neighbors.

## Exhaustive geometry and occupancy checks

With C++11 (MSYS2 g++ on this machine):

```powershell
g++ -std=c++11 -Wall -Wextra -Werror -I include tests/knight-route.cpp -o .pio/knight-route-test.exe
.pio/knight-route-test.exe
```

This compiles the actual route and map headers. It checks all 4096 square pairs:
336 legal directed knight routes, every orientation and edge, reverse waypoints,
diagonal entry/exit, and the boundary segment. It also checks 32 unique perimeter
slots, all 2048 square/slot combinations (60 valid adjacent approaches), exact
retrieval reversals, invalid ranges, the starting occupancy, delivery/parking
transitions and unknown-map rejection.

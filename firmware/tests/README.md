# Compiled Uno firmware checks

Build the actual firmware HEX and install the pinned simulator dependency:

```powershell
pio run
pnpm --dir tests install --frozen-lockfile
```

Run the default mode first. It saves `.pio/storage-test.eeprom` for the
reboot and damaged-record cases.

```powershell
node tests/machine-sim.cjs
node tests/machine-sim.cjs --restore
node tests/machine-sim.cjs --small-table
node tests/machine-sim.cjs --corrupt
node tests/machine-sim.cjs --interrupted-save
node tests/machine-sim.cjs --config-change
```

The simulator executes `.pio/build/uno/firmware.hex` in AVR8js, including
Timer0/Timer1, serial, GPIO, relay, and EEPROM. It models four normally-open
switches at measured trip positions and records every STEP edge, direction
transition, magnet event, and commanded pulse position.

| Mode | Coverage |
| --- | --- |
| Default | HELLO/STATUS/GEOM, monotonic IDs across 32767, duplicate rejection, MAP syntax and busy rejection, async HOME/SAVE, route/map validation before motion, square carry, victim-first capture order, d5-to-P00 host vector, occupied perimeter blocker, retrieval, short travel pulse, dwell contact, sustained moving contact, STOP, raw homing edge |
| `--restore` | Fresh MCU from saved EEPROM still requires MIN reference and a fresh occupancy MAP |
| `--small-table` | Measured table too short for the configured board cannot enable carry |
| `--corrupt` / `--interrupted-save` / `--config-change` | Damaged, unfinished, or incompatible EEPROM records cannot authorize REF |

The simulator confirms commanded positions and motion safety decisions. It
cannot establish physical magnetic coupling, piece clearance, backlash, missed
steps, supply integrity, or the reported magnet-release reset. Those require
bench testing.

## Exhaustive route and map header checks

The earlier C++ route/map headers remain in source history and are still
checked independently:

```powershell
g++ -std=c++11 -Wall -Wextra -Werror -I include tests/knight-route.cpp -o .pio/knight-route-test.exe
.pio/knight-route-test.exe
```

This checks all 4096 square pairs, 32 parking locations, and occupancy
transitions. The active firmware's general CARRY routes are exercised by the
compiled-HEX simulator above.

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
| Default | HELLO/STATUS/GEOM and CARRY_DIAG/CARRY_OFFSET/OFFSET capabilities, monotonic IDs across 32767, duplicate rejection, MAP syntax and busy rejection, async HOME/SAVE, route/map validation before motion, square carry, in-square OFFSET and its occupied-neighbor clearance guard, direct CARRY_OFFSET from the corner with occupied parking, accepted 45-degree grid diagonal, rejection of occupied-center crossing and non-45-degree diagonal, victim-first capture order, d5-to-P00 host vector, measured 50 mm/s loaded STEP ramp and 30 mm/s crowded-horizontal profile with one and two occupied neighbors, open/vertical/diagonal profile checks, occupied perimeter blocker, retrieval, short travel pulse, dwell contact, sustained moving contact, STOP of CARRY, CARRY_OFFSET, and OFFSET, raw homing edge |
| `--restore` | Fresh MCU from saved EEPROM still requires MIN reference and a fresh occupancy MAP |
| `--small-table` | Measured table too short for the configured board cannot enable carry |
| `--corrupt` / `--interrupted-save` / `--config-change` | Damaged, unfinished, or incompatible EEPROM records cannot authorize REF |

The simulator confirms commanded positions and motion safety decisions. It
cannot establish physical magnetic coupling, piece clearance, backlash, missed
steps, supply integrity, or the reported magnet-release reset. Those require
bench testing. The obsolete `KnightRoute` header test exercised a generator
that is no longer reachable from the shipped serial commands, so it was
removed. This compiled-HEX test still covers active CARRY validation,
representative parking coordinates, and occupancy transitions.

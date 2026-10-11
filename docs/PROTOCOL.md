# Serial protocol

The integration firmware uses a versioned, newline-terminated ASCII link at **115200 baud**. The computer must use framed mode and must never infer success from a delay or from a printed waypoint. Each host request, including its newline, is at most 96 bytes. The host accepts up to 128 bytes before the newline in controller replies so extended capability lists fit.

## Version 1 frame contract

The computer sends `@<id> <VERB> [arguments]\n`, where `<id>` is a positive decimal request ID. Only one normal request may be active; `STOP` can preempt a motion or reference operation. The firmware responds with `@<id> ACK` when it accepts a request and later emits exactly one terminal `@<id> DONE ...` or `@<id> ERR <CODE>`. The host matches both responses to the request ID. A controller restart is announced as `!BOOT V=1`; that event invalidates all in-flight physical assumptions.

| Request | Terminal success | Meaning |
| --- | --- | --- |
| `HELLO` | `DONE HELLO V=1 CAPS=STATUS,HOME,REF,STOP,MAP,CARRY,CARRY_DIAG,SAVE,GEOM` | Negotiate version and the implemented commands; `CARRY_DIAG` permits grid-to-grid diagonal legs. |
| `STATUS` | `DONE STATUS REF=<0\|1> BUSY=<0\|1> MAP=<0\|1> SAVED=<0\|1>` | Read reference, busy, occupancy-map and saved-geometry state. |
| `HOME` | `DONE HOME` | Full X/Y calibration has actually completed. |
| `REF` | `DONE REF` | Saved-geometry MIN reference has actually completed. |
| `STOP` | `DONE STOP` | Both axes and magnet are stopped; reference is invalidated. An interrupted request receives `ERR CANCELED`. |
| `SAVE` | `DONE SAVE` | Store completed calibration geometry in EEPROM. |
| `GEOM` | `DONE GEOM X=... Y=... OX=... OY=... P=... I=... PPM=...` | Read live referenced geometry: X/Y travel, X/Y board origin, pitch, inset (millimeters), and pulses per millimeter, printed to four decimal places. |
| `MAP <bitmap>` | `DONE MAP` | Replace the compact occupancy guard from a physically reconciled position. |
| `CARRY <source> <destination> <points>` | `DONE CARRY` | Execute one validated, bounded carry and release the magnet. |

Other errors include `BUSY`, `SYNTAX`, `UNKNOWN`, `DUPLICATE`, `REFERENCE`, `MAP`, `ROUTE`, `CAL_X`, `CAL_Y`, `GEOMETRY`, and `MOTION`. A command that times out, sees a reboot, or receives an unrelated/malformed terminal response enters an **uncertain** state. `GEOMETRY` from `CARRY` also requires reconciliation because it can report an invalid live carriage position. The computer must stop and reconcile before committing or resuming a physical action. A repeated ID while still pending may receive its ACK again; a completed ID must not be replayed as a new motion request. IDs increase without wrapping during one boot, with `65535` reserved for a final emergency `STOP`.

## Carry and occupancy encoding

`MAP` takes exactly 24 uppercase hexadecimal characters (12 bytes). Within each byte, the least significant bit is the lowest position ID. IDs `0..63` are `a1..h8` in rank-major order; IDs `64..95` are perimeter slots `P00..P31`. The initial chess position with all parking slots empty is `FFFF00000000FFFF00000000`. The host sends this command only after the physical board and host inventory have been reconciled. The bitmap records occupancy, not piece identity.

`CARRY` names source and destination as decimal position IDs `0..95`. `<points>` is 4 to 24 characters: two base-17 characters per waypoint, inclusive of pickup and delivery, for **2..12 waypoints**. Digits `0..9` and letters `A..G` encode a half-square coordinate `0..16`; each pair is `(x,y)`. For example, `79` is the center of `d5`, and `00` is parking slot `P00`. Square centers are odd/odd; grid intersections and parking positions are even/even. Center-to-grid links are one-step diagonals; grid-to-grid legs may be orthogonal or 45-degree diagonal. The host plans the route from a current occupancy snapshot, first checking measured piece clearance and optionally retrying a bounded route that avoids occupied centers when that clearance blocks play. The firmware checks route topology, map occupancy, and travel bounds before energizing the magnet. It rejects a route that cannot fit in 12 waypoints; neither side truncates one.

After `HOME` or `REF`, the host reads `GEOM` and uses those numbers for checked route planning. The four-decimal reply has finite precision; the clearance margin must allow for that rounding, motor path error, and physical centering error. `GEOM` returns `ERR REFERENCE` before a live reference exists. `HELLO` and `GEOM` return `ERR BUSY` during motion or calibration so their longer replies cannot delay limit sampling.

During a board move, a limit input must remain closed for 3 ms before it stops both axes. Switch activity while idle or between movement segments is ignored. Homing still uses the first raw switch edge followed by stationary confirmation. A sustained limit trip during a carry stops motion and makes the physical position and map uncertain.

`DONE` confirms commanded steps and the release sequence. It does **not** prove physical pickup or delivery; there are no piece-presence sensors in the imported hardware. After a stop, reset, fault or timeout, the live carriage and board map need physical reconciliation.

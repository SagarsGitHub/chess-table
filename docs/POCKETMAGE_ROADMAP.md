# PocketMage chess delivery

Standalone PocketMage chess control and an opt-in host LAN API are implemented.
See the [control contract](POCKETMAGE_CONTROL.md) and the [app installation guide](https://github.com/SagarsGitHub/pocketmage-chess#install).

| Area | Implementation / remaining acceptance |
| --- | --- |
| Source policy | Typed aina/pocketmage ownership, saved options, fenced moves and durable receipts through one controller. |
| LAN API | Opt-in status/start/STOP/moves/receipt listener on 8767. Aina retains authenticated loopback access. Bash/PowerShell launcher options. |
| Game modes | Manual, human versus Stockfish with either color, and Stockfish self-play. |
| Sideloadable app | Frame-based board cells, individual piece assets and side panes; host legal destinations, turn, mode, connection and activity. |
| Retry behavior | Persist exact intents, explicit resume after reconnect/reboot, stale/changed/wrong-source moves refused. |
| Physical acceptance | Pending attended PocketMage trial: both colors, self-play, captures/promotion, STOP, disconnection/reboot and local inspected recovery. Preserve checkpoint/serial evidence. |
| Device reset UX | Deferred; use existing local host reset/setup/reconciliation. |
| Chess.com | Separate future integration. |

The host keeps rules, Stockfish, routes, expected inventory, checkpoints and the
sole Uno connection. No Uno firmware change is required. The board has no sensors;
uncertain motion needs local inspection. Device flashing remains an explicit
operator action. Builds do not establish hardware acceptance.

## Software and Pi deployment evidence (October 17, 2026)

264 host tests pass in the development environment. The Pi ran the same suite;
a missing staging copy of build_gui.py was supplied and all eight GUI tests
then passed. Real `/usr/games/stockfish` with simulated motors exercised manual
both sides (2 plies), human White (move and reply), human Black (engine opening),
and engine self-play (4 plies). The deterministic self-play integration test
covers a complete checkmate through the existing runner and simulated carries.
These checks do not establish loaded physical acceptance.

The Pi service now enables PocketMage at `http://192.168.4.62:8767`, with Aina
still authenticated on loopback 8766. Deployment preserved the exact SHA-256
of `computer/runs/boot-physical.json`; the host remains idle at ply 0 with 20
legal opening moves. Source and checkpoint backup is under
`~/.local/state/chess-table-pocketmage/20261017-154945/before-deploy.tgz`.

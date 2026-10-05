# Contributor guide

- On the current development machine, the authoritative checkout is `C:\Users\sagar\Documents\Coding\chess-table` (WSL: `/mnt/c/Users/sagar/Documents/Coding/chess-table`); the similarly named ChatGPT workspace is a mirror. In other clones, use the local repository root.
- Keep chess rules, physical inventory, routing, checkpoints, Stockfish, and the GUI in `computer/`. Keep motor execution, limits, magnet control, and route validation in `firmware/`. The board has no piece sensors.
- From `computer/`, run `python -m unittest discover -s tests -q` in the project environment. From `firmware/`, run `pio run` and `node tests/machine-sim.cjs` when firmware changes.
- Edit `computer/chess_table_host/gui_static/gui_*.js`, then run `python build_gui.py` from `computer/` to regenerate the served `app.js`.
- A matched `DONE CARRY` advances expected occupancy automatically in normal play; it cannot sense actual delivery. Inspect and reconcile an interrupted or uncertain carry before another move. Preserve `computer/runs/` physical checkpoints and serial logs.
- Keep the Uno's measured travel bounds and 12-waypoint cap. The tight-route fallback and crowded-horizontal cost are routing choices, not physical clearance guarantees.
- Use [README.md](README.md) for entry points, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for ownership, [docs/PROTOCOL.md](docs/PROTOCOL.md) for the wire contract, and [docs/ROADMAP.md](docs/ROADMAP.md) for accepted and pending work.

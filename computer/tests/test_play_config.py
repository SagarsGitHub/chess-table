import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chess_table_host.game import GameRunner, GameState, GameStateError, JsonGameStore, MoveDeferred
from chess_table_host.play import (
    DEFAULT_CONFIG, LoggedSerialStream, SimulatedLink, _geometry_from_file, _load_config,
    _play_moves, _reconcile_at_start, _resolve_engine_path, _route_geometry, main,
)


class PlayConfigurationTests(unittest.TestCase):
    def test_checked_in_defaults_use_com3_and_stockfish_folder(self) -> None:
        config = _load_config(DEFAULT_CONFIG)
        self.assertEqual(config["serial_port"], "COM3")
        self.assertEqual(config["stockfish_path"], "~/Documents/stockfish")
        self.assertEqual(config["moving_diameter_mm"], 18)
        self.assertEqual(config["standing_diameter_mm"], 18)
        self.assertIs(config["allow_tight_routes"], True)

    def test_physical_script_advances_after_controller_completion_without_routine_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "game.json"
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            output = io.StringIO()
            with (
                patch("chess_table_host.play._connect_physical", return_value=(SimulatedLink(), controller)),
                patch("builtins.input", side_effect=AssertionError("unexpected physical-play prompt")),
                redirect_stdout(output),
            ):
                result = main(["--new", "--state", str(state), "--moves", "g1f3"])
            self.assertEqual(result, 0)
            self.assertIn("CLEARANCE OVERRIDE", output.getvalue())
            self.assertIn("Committed g1f3", output.getvalue())
            saved = JsonGameStore(state).load()
            self.assertEqual(saved.moves, ["g1f3"])
            self.assertEqual(
                [event["confirmation_source"] for event in saved.events
                 if event["event"] == "action_confirmed"],
                ["controller_done"],
            )

    def test_clean_physical_resume_continues_without_routine_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "game.json"
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            with (
                patch("chess_table_host.play._connect_physical", return_value=(SimulatedLink(), controller)),
                patch("builtins.input", side_effect=AssertionError("unexpected physical-play prompt")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main([
                    "--new", "--state", str(state), "--moves", "e2e4",
                ]), 0)
                self.assertEqual(main([
                    "--resume", "--state", str(state), "--moves", "e2e4", "a7a6",
                ]), 0)
            self.assertEqual(JsonGameStore(state).load().moves, ["e2e4", "a7a6"])

    def test_strict_clearance_cli_override_rejects_crowded_knight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "game.json"
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            output = io.StringIO()
            error = io.StringIO()
            with (
                patch("chess_table_host.play._connect_physical", return_value=(SimulatedLink(), controller)),
                patch("builtins.input", side_effect=AssertionError("unexpected physical-play prompt")),
                redirect_stdout(output),
                redirect_stderr(error),
            ):
                result = main([
                    "--new", "--state", str(state), "--moves", "g1f3", "--strict-clearance",
                ])
            self.assertEqual(result, 1)
            self.assertIn("Tight-route fallback: disabled", output.getvalue())
            self.assertIn("move g1f3 cannot start", error.getvalue())
            saved = JsonGameStore(state).load()
            self.assertEqual(saved.moves, [])
            self.assertIsNone(saved.pending)

    def test_supervised_physical_mode_keeps_explicit_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "game.json"
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            prompts: list[str] = []

            def answer(prompt: str) -> str:
                prompts.append(prompt)
                return "y"

            with (
                patch("chess_table_host.play._connect_physical", return_value=(SimulatedLink(), controller)),
                patch("builtins.input", side_effect=answer),
                redirect_stdout(io.StringIO()),
            ):
                result = main([
                    "--new", "--state", str(state), "--moves", "e2e4", "--supervised",
                ])
            self.assertEqual(result, 0)
            self.assertEqual(len(prompts), 4)
            self.assertIn("entire physical board", prompts[0])
            self.assertIn("Start this physical move", prompts[1])
            self.assertIn("Start this transfer", prompts[2])
            self.assertIn("Confirm physical action", prompts[3])
            saved = JsonGameStore(state).load()
            self.assertEqual(saved.moves, ["e2e4"])
            self.assertEqual(
                [event["confirmation_source"] for event in saved.events
                 if event["event"] == "action_confirmed"],
                ["operator"],
            )

    def test_automatic_mode_still_prompts_for_uncertain_carry_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "game.json"
            store = JsonGameStore(state)
            runner = GameRunner(GameState.new(), store)
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            geometry = _route_geometry(controller, 18, 18, 0)
            runner.prepare_move("e2e4", geometry)

            class DisconnectingLink(SimulatedLink):
                def carry(self, source_id: str, destination_id: str, point_chars: str):
                    raise OSError("serial disconnect during CARRY")

            with redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):
                    runner.execute_pending(DisconnectingLink(), geometry, lambda _action: True)
            self.assertEqual(store.load().pending.phase, "in_flight")
            prompts: list[str] = []
            answers = iter(("source", "y", "y"))

            def answer(prompt: str) -> str:
                prompts.append(prompt)
                return next(answers)

            with (
                patch("chess_table_host.play._connect_physical", return_value=(SimulatedLink(), controller)),
                patch("builtins.input", side_effect=answer),
                redirect_stdout(io.StringIO()),
            ):
                result = main(["--resume", "--state", str(state), "--moves", "e2e4"])
            self.assertEqual(result, 0)
            self.assertEqual(len(prompts), 3)
            self.assertIn("SOURCE, DESTINATION, or ABORT", prompts[0])
            self.assertIn("inspect", prompts[1])
            self.assertIn("entire physical board", prompts[2])
            self.assertEqual(store.load().moves, ["e2e4"])

    def test_explicit_linux_engine_folder_resolves_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            binary = folder / "stockfish"
            binary.write_bytes(b"test fixture")
            self.assertEqual(_resolve_engine_path(str(folder)), str(binary))

    def test_serial_transcript_records_boot_ack_and_sent_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "serial.jsonl"
            stream = LoggedSerialStream(io.BytesIO(b"!BOOT V=1\n@1 ACK\n"), path)
            while stream.read(1):
                pass
            stream.write(b"@2 STATUS\n")
            stream.close()
            frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                [(frame["direction"], frame["line"]) for frame in frames],
                [("rx", "!BOOT V=1"), ("rx", "@1 ACK"), ("tx", "@2 STATUS")],
            )

    def test_custom_config_drives_scripted_simulation_without_cli_port_or_sizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = {
                "serial_port": "/dev/ttyACM0",
                "stockfish_path": "/opt/stockfish",
                "moving_diameter_mm": 18,
                "standing_diameter_mm": 18,
                "clearance_margin_mm": 0,
            }
            config_path = folder / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            geometry = DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            state = folder / "game.json"
            output = io.StringIO()
            with redirect_stdout(output):
                result = main([
                    "--config", str(config_path), "--simulate", "--new",
                    "--state", str(state), "--geometry-file", str(geometry),
                    "--moves", "e2e4",
                ])
            self.assertEqual(result, 0)
            self.assertIn("Committed e2e4", output.getvalue())
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["moves"], ["e2e4"])

    def test_ready_partial_capture_can_be_aborted_after_manual_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JsonGameStore(Path(temporary) / "game.json")
            runner = GameRunner(GameState.new(), store)
            store.save(runner.state)
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            geometry = _route_geometry(controller, 18, 18, 0)
            link = SimulatedLink()
            with redirect_stdout(io.StringIO()):
                for uci in ("e2e4", "d7d5"):
                    runner.prepare_move(uci, geometry)
                    runner.execute_pending(link, geometry, lambda action: True)
                runner.prepare_move("e4d5", geometry)
                approvals = iter((True, False))
                with self.assertRaises(MoveDeferred):
                    runner.execute_pending(
                        link, geometry, lambda action: True,
                        lambda action, route, wire: next(approvals),
                    )
                self.assertEqual(runner.state.pending.next_action, 1)
                self.assertEqual(runner.state.pending.phase, "ready")
                with patch("builtins.input", side_effect=("abort", "y", "y")):
                    _reconcile_at_start(runner)
            self.assertIsNone(runner.state.pending)
            self.assertEqual(runner.state.moves, ["e2e4", "d7d5"])
            self.assertEqual(runner.state.physical_parking, [None] * 32)

    def test_resume_rejects_script_that_disagrees_with_pending_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JsonGameStore(Path(temporary) / "game.json")
            runner = GameRunner(GameState.new(), store)
            controller = _geometry_from_file(
                DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
            )
            geometry = _route_geometry(controller, 18, 18, 0)
            runner.prepare_move("e2e4", geometry)
            args = SimpleNamespace(moves=["d2d4"], engine=None, manual_both=False)
            with self.assertRaisesRegex(GameStateError, "pending physical move"):
                _play_moves(args, runner, SimulatedLink(), geometry)
            self.assertEqual(JsonGameStore(store.path).load().pending.uci, "e2e4")


if __name__ == "__main__":
    unittest.main()

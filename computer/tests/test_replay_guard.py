"""Replay ownership survives restart and prevents ordinary CLI takeover."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chess_table_host.game import GameRunner, GameState, GameStateError, JsonGameStore
from chess_table_host.link_adapters import SimulatedLink
from chess_table_host.play import _geometry_from_file, _route_geometry, main
from chess_table_host.replay_guard import (
    read_replay_marker, replay_marker_path, save_replay_marker,
)


FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "board_geometry_2026-10-05.json"


class ReplayGuardTests(unittest.TestCase):
    def test_absent_and_valid_marker_keep_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.before-reset-2026.json"
            self.assertEqual(replay_marker_path(checkpoint).name,
                             "game.before-reset-2026.replay.json")
            self.assertIsNone(read_replay_marker(checkpoint))
            marker = {"schema_version": 1, "host_game_id": "host-a", "kind": "daily",
                      "game_id": "https://www.chess.com/game/daily/1"}
            save_replay_marker(checkpoint, marker)
            self.assertEqual(read_replay_marker(checkpoint), marker)

    def test_corrupt_or_invalid_marker_fails_closed(self):
        invalid = ("{", "null", "[]", "1", '"host-a"', '{}',
                   '{"schema_version":true,"host_game_id":"host-a"}',
                   '{"schema_version":2,"host_game_id":"host-a"}',
                   '{"schema_version":1,"host_game_id":""}',
                   '{"schema_version":1,"host_game_id":1}')
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            marker_path = replay_marker_path(checkpoint)
            for text in invalid:
                with self.subTest(text=text):
                    marker_path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(GameStateError, "preserve it and inspect"):
                        read_replay_marker(checkpoint)
                    self.assertEqual(marker_path.read_text(encoding="utf-8"), text)
            marker_path.write_bytes(b"\xff")
            with self.assertRaisesRegex(GameStateError, "preserve it and inspect"):
                read_replay_marker(checkpoint)

    def test_failed_atomic_replacement_preserves_existing_marker_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            original = {"schema_version": 1, "host_game_id": "host-a"}
            replacement = {"schema_version": 1, "host_game_id": "host-b"}
            save_replay_marker(checkpoint, original)
            with patch("chess_table_host.replay_guard.os.replace", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    save_replay_marker(checkpoint, replacement)
            self.assertEqual(read_replay_marker(checkpoint), original)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_fsync_precedes_publication_and_written_marker_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            marker = {"schema_version": 1, "host_game_id": "host-a"}
            events = []
            actual_fsync, actual_replace = os.fsync, os.replace

            def sync(descriptor):
                events.append("fsync")
                return actual_fsync(descriptor)

            def publish(source, destination):
                self.assertEqual(events, ["fsync"])
                self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), marker)
                events.append("replace")
                return actual_replace(source, destination)

            with patch("chess_table_host.replay_guard.os.fsync", side_effect=sync), \
                    patch("chess_table_host.replay_guard.os.replace", side_effect=publish):
                save_replay_marker(checkpoint, marker)
            self.assertEqual(events, ["fsync", "replace"])
            self.assertEqual(read_replay_marker(checkpoint), marker)

    def test_cli_refuses_replay_before_serial_or_engine_and_preserves_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            state = GameState.new()
            JsonGameStore(checkpoint).save(state)
            save_replay_marker(checkpoint, {"schema_version": 1, "host_game_id": state.host_game_id})
            original = checkpoint.read_bytes()
            with patch("chess_table_host.play._connect_physical") as connect, \
                    patch("chess_table_host.play.chess.engine.SimpleEngine.popen_uci") as engine, \
                    redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(["--resume", "--state", str(checkpoint), "--self-play"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("checkpoint belongs to a recorded replay", error.getvalue())
            connect.assert_not_called()
            engine.assert_not_called()
            self.assertEqual(checkpoint.read_bytes(), original)

    def test_cli_invalid_marker_fails_before_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            JsonGameStore(checkpoint).save(GameState.new())
            replay_marker_path(checkpoint).write_text("null", encoding="utf-8")
            with patch("chess_table_host.play._connect_physical") as connect, \
                    patch("chess_table_host.play.chess.engine.SimpleEngine.popen_uci") as engine, \
                    redirect_stderr(io.StringIO()) as error:
                with self.assertRaises(SystemExit) as raised:
                    main(["--resume", "--state", str(checkpoint), "--self-play"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("replay ownership marker is invalid", error.getvalue())
            connect.assert_not_called()
            engine.assert_not_called()

    def test_cli_explicit_reset_preserves_archive_marker_and_releases_old_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "game.json"
            store = JsonGameStore(checkpoint)
            runner = GameRunner(GameState.new(), store, allow_tight_routes=True)
            geometry = _route_geometry(_geometry_from_file(FIXTURE), 18.0, 18.0, 0.0)
            with redirect_stdout(io.StringIO()):
                runner.prepare_move("e2e4", geometry)
                runner.execute_pending(SimulatedLink(), geometry, lambda _action: True,
                                       confirmation_source="simulation")
            old_id = runner.state.host_game_id
            marker = {"schema_version": 1, "host_game_id": old_id, "kind": "daily"}
            save_replay_marker(checkpoint, marker)
            with redirect_stdout(io.StringIO()), \
                    patch("chess_table_host.play.chess.engine.SimpleEngine.popen_uci") as engine:
                result = main(["--resume", "--state", str(checkpoint), "--reset-board",
                               "--simulate", "--geometry-file", str(FIXTURE)])
            self.assertEqual(result, 0)
            engine.assert_not_called()
            reset = store.load()
            self.assertEqual(reset.moves, [])
            self.assertNotEqual(reset.host_game_id, old_id)
            self.assertEqual(read_replay_marker(checkpoint), marker)
            archives = [path for path in Path(directory).glob("game.before-reset-*.json")
                        if not path.name.endswith(".replay.json")]
            self.assertEqual(len(archives), 1)
            self.assertEqual(read_replay_marker(archives[0]), marker)


if __name__ == "__main__":
    unittest.main()

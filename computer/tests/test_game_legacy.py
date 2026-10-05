"""Older checkpoints may resume only when their physical state is unambiguous."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import chess

from chess_table_host.game import GameState, GameStateError, JsonGameStore
from chess_table_host.play import DEFAULT_CONFIG, main


def version_one(state: dict[str, object]) -> dict[str, object]:
    legacy = dict(state, version=1)
    legacy.pop("initial_fen")
    legacy.pop("promotion_rule")
    return legacy


class LegacyCheckpointTests(unittest.TestCase):
    def test_completed_five_ply_smoke_resumes_without_rewriting_version_one_file(self) -> None:
        moves = ["e2e4", "a7a6", "e4e5", "d7d5", "e5d6"]
        geometry = DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "smoke.json"
            args = ["--simulate", "--state", str(path), "--geometry-file", str(geometry)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*args, "--new", "--moves", *moves]), 0)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["moves"], moves)
            path.write_text(json.dumps(version_one(saved), indent=2) + "\n", encoding="utf-8")
            original = path.read_bytes()

            restored = JsonGameStore(path).load()
            self.assertEqual(restored.initial_fen, chess.Board().fen())
            self.assertEqual(restored.moves, moves)
            self.assertEqual(restored.physical_parking.count("p"), 1)
            self.assertEqual(path.read_bytes(), original)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([*args, "--resume", "--moves", *moves]), 0)
            self.assertEqual(path.read_bytes(), original)

    def test_version_one_pending_nonpromotion_keeps_uncertain_phase(self) -> None:
        saved = version_one(GameState.new().to_json())
        after = chess.Board()
        after.push_uci("e2e4")
        saved["pending"] = {
            "uci": "e2e4", "fen_after": after.fen(),
            "parking_after": [None] * 32,
            "actions": [{
                "type": "transfer", "piece": "P", "source": {"square": "e2"},
                "destination": {"square": "e4"}, "reason": "move",
            }],
            "next_action": 0, "phase": "in_flight",
            "active_route": ["e2", "e4", "0,0"],
        }
        restored = GameState.from_json(saved)
        self.assertEqual(restored.pending.phase, "in_flight")
        self.assertEqual(restored.pending.next_action, 0)
        self.assertEqual(restored.physical_squares["e2"], "P")

        saved["pending"]["actions"][0]["destination"] = {"square": "d4"}
        with self.assertRaisesRegex(GameStateError, "pending actions do not match"):
            GameState.from_json(saved)

    def test_version_one_rejects_nonstandard_or_corrupt_history(self) -> None:
        saved = version_one(GameState.new().to_json())
        saved["board_fen"] = chess.Board("8/8/8/8/8/8/8/K6k w - - 0 1").fen()
        with self.assertRaisesRegex(GameStateError, "initial position cannot be inferred"):
            GameState.from_json(saved)
        saved["moves"] = ["e2e5"]
        with self.assertRaisesRegex(GameStateError, "move history cannot be replayed"):
            GameState.from_json(saved)
        saved = version_one(GameState.new().to_json())
        saved["pending"] = {}
        with self.assertRaises(GameStateError):
            GameState.from_json(saved)

    def test_version_one_and_two_reject_promotions(self) -> None:
        for version in (1, 2):
            with self.subTest(version=version):
                saved = GameState.new().to_json()
                saved["version"] = version
                saved.pop("promotion_rule")
                if version == 1:
                    saved.pop("initial_fen")
                saved["moves"] = ["a7a8q"]
                with self.assertRaisesRegex(GameStateError, "promotion checkpoint needs manual reconciliation"):
                    GameState.from_json(saved)
                saved["moves"] = []
                saved["pending"] = {"uci": "a7a8q"}
                with self.assertRaisesRegex(GameStateError, "promotion checkpoint needs manual reconciliation"):
                    GameState.from_json(saved)

    def test_unsupported_version_reports_cli_error_without_traceback(self) -> None:
        geometry = DEFAULT_CONFIG.parent / "fixtures" / "board_geometry_2026-10-05.json"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "game.json"
            saved = GameState.new().to_json()
            saved["version"] = 99
            path.write_text(json.dumps(saved), encoding="utf-8")
            error = io.StringIO()
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                main([
                    "--simulate", "--resume", "--state", str(path),
                    "--geometry-file", str(geometry),
                ])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("unsupported game-state version", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())


if __name__ == "__main__":
    unittest.main()

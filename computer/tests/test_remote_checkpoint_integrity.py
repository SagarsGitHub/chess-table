"""Remote checkpoint recovery rejects corrupt fences before any motion."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import chess

from chess_table_host.checkpoint import (
    GameState, JsonGameStore, PendingMove, PendingReset, RemoteMoveReceipt,
    RemoteMoveResolution, STATE_VERSION,
)
from chess_table_host.game_errors import GameStateError
from chess_table_host.physical import _board_pieces
from chess_table_host.planner import BoardSquare, ParkingInventory, Transfer


def bound_game() -> GameState:
    state = GameState.new()
    state.source_kind = "aina"
    state.source_game_id = "source-game"
    state.remote_start_request_id = "start-request"
    return state


def command(state: GameState, uci: str = "e2e4", request_id: str = "move-1") -> RemoteMoveReceipt:
    return RemoteMoveReceipt(
        request_id=request_id, host_game_id=state.host_game_id,
        source_game_id=state.source_game_id, expected_ply=len(state.moves),
        expected_fen=state.board_fen, uci=uci,
    )


def prepare(state: GameState, uci: str = "e2e4", *, remote: bool = True) -> None:
    board = state.board()
    move = board.parse_uci(uci)
    piece = board.piece_at(move.from_square).symbol()
    board.push(move)
    receipt = command(state, uci) if remote else None
    state.pending = PendingMove(
        uci=uci, fen_after=board.fen(), parking_after=state.committed_parking,
        actions=(Transfer(
            piece, BoardSquare(chess.square_name(move.from_square)),
            BoardSquare(chess.square_name(move.to_square)), "move",
        ),), remote_request_id=receipt.request_id if receipt else None,
    )
    if receipt:
        state.remote_receipts[receipt.request_id] = receipt


def commit_simple_move(state: GameState, uci: str) -> None:
    """Construct confirmed simple non-capture history without calling a runner."""
    board = state.board()
    board.push_uci(uci)
    state.moves.append(uci)
    state.board_fen = board.fen()
    state.physical_squares = _board_pieces(board)
    state.pending = None


class RemoteCheckpointMigrationTests(unittest.TestCase):
    def test_v6_requires_all_session_fields(self) -> None:
        saved = bound_game().to_json()
        self.assertEqual(saved["version"], STATE_VERSION)
        for key in ("remote_mode", "remote_human", "remote_resolutions"):
            with self.subTest(key=key):
                damaged = deepcopy(saved)
                del damaged[key]
                with self.assertRaises(GameStateError):
                    GameState.from_json(damaged)

    def test_early_v5_defaults_only_new_fields_preserving_identity(self) -> None:
        state = bound_game()
        prepare(state)
        saved = state.to_json()
        saved["version"] = 5
        for key in ("remote_mode", "remote_human", "remote_resolutions"):
            del saved[key]
        original = deepcopy(saved)
        restored = GameState.from_json(saved)
        self.assertEqual(saved, original)
        self.assertEqual(restored.remote_mode, "external")
        self.assertEqual(restored.remote_human, "white")
        self.assertEqual(restored.remote_resolutions, {})
        migrated = restored.to_json()
        for key in ("host_game_id", "source_kind", "source_game_id",
                    "remote_start_request_id", "remote_receipts", "pending",
                    "physical_squares", "physical_parking", "moves"):
            self.assertEqual(migrated[key], original[key])
        self.assertEqual(migrated["version"], STATE_VERSION)

    def test_v5_stockfish_black_resolution_and_inflight_plan_round_trip(self) -> None:
        state = bound_game()
        state.remote_mode = "human_engine"
        state.remote_human = "black"
        commit_simple_move(state, "e2e4")
        receipt = command(state, "e7e5")
        state.remote_resolutions[receipt.request_id] = RemoteMoveResolution(
            expression="pawn to e5", command=deepcopy(receipt),
        )
        prepare(state, "e7e5")
        state.pending.phase = "in_flight"
        state.pending.active_route = ("e7", "e5", "0,0")
        state.remote_receipts[receipt.request_id].state = "in_flight"
        saved = state.to_json()
        saved["version"] = 5
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            path.write_text(json.dumps(saved), encoding="utf-8")
            original_bytes = path.read_bytes()
            restored = JsonGameStore(path).load()
            self.assertEqual(path.read_bytes(), original_bytes)
            migrated = restored.to_json()
        self.assertEqual(migrated, {**saved, "version": STATE_VERSION})
        self.assertIsInstance(restored.remote_resolutions[receipt.request_id], RemoteMoveResolution)
        self.assertEqual(restored.remote_mode, "human_engine")
        self.assertEqual(restored.remote_human, "black")

    def test_v5_missing_identity_or_receipts_never_mints_new_session(self) -> None:
        saved = bound_game().to_json()
        saved["version"] = 5
        for key in ("host_game_id", "source_kind", "source_game_id",
                    "remote_start_request_id", "remote_receipts"):
            with self.subTest(key=key):
                damaged = deepcopy(saved)
                del damaged[key]
                with self.assertRaises(GameStateError):
                    GameState.from_json(damaged)

    def test_v5_partial_newer_metadata_never_guesses_mode_or_color(self) -> None:
        state = bound_game()
        state.remote_mode = "human_engine"
        state.remote_human = "black"
        saved = state.to_json()
        saved["version"] = 5
        for missing in (("remote_mode",), ("remote_human",),
                        ("remote_mode", "remote_human"),
                        ("remote_mode", "remote_resolutions"),
                        ("remote_human", "remote_resolutions")):
            with self.subTest(missing=missing):
                damaged = deepcopy(saved)
                for key in missing:
                    del damaged[key]
                with self.assertRaisesRegex(GameStateError, "incomplete version 5"):
                    GameState.from_json(damaged)

    def test_v5_explicit_mode_and_color_without_resolution_cache_are_preserved(self) -> None:
        state = bound_game()
        state.remote_mode = "human_engine"
        state.remote_human = "black"
        saved = state.to_json()
        saved["version"] = 5
        del saved["remote_resolutions"]
        restored = GameState.from_json(saved)
        self.assertEqual(restored.remote_mode, "human_engine")
        self.assertEqual(restored.remote_human, "black")
        self.assertEqual(restored.remote_resolutions, {})

    def test_v5_reset_plan_preserves_uncertain_phase(self) -> None:
        state = bound_game()
        commit_simple_move(state, "e2e4")
        state.reset = PendingReset(
            actions=(Transfer("P", BoardSquare("e4"), BoardSquare("e2"), "reset_board"),),
            phase="awaiting_confirmation", active_route=("e4", "e2", "0,0"),
        )
        saved = state.to_json()
        saved["version"] = 5
        self.assertEqual(GameState.from_json(saved).to_json(), {**saved, "version": STATE_VERSION})

    def test_v5_promotion_standin_plan_preserves_pawn_body(self) -> None:
        board = chess.Board("7k/P7/8/8/8/8/8/7K w - - 0 1")
        state = GameState(
            board_fen=board.fen(), committed_parking=ParkingInventory.empty(),
            physical_squares=_board_pieces(board), physical_parking=[None] * 32,
        )
        prepare(state, "a7a8q", remote=False)
        saved = state.to_json()
        saved["version"] = 5
        restored = GameState.from_json(saved)
        self.assertEqual(restored.to_json(), {**saved, "version": STATE_VERSION})
        self.assertEqual(restored.pending.actions[0].piece, "P")


class RemoteCheckpointIntegrityTests(unittest.TestCase):
    def test_active_receipt_must_match_pending_uci_even_when_both_moves_are_legal(self) -> None:
        state = bound_game()
        prepare(state)
        saved = state.to_json()
        saved["remote_receipts"]["move-1"]["uci"] = "d2d4"
        with self.assertRaisesRegex(GameStateError, "differs from pending move"):
            GameState.from_json(saved)

    def test_active_receipt_must_name_current_ply_and_fen(self) -> None:
        state = bound_game()
        commit_simple_move(state, "e2e4")
        prepare(state, "e7e5")
        saved = state.to_json()
        receipt = saved["remote_receipts"]["move-1"]
        receipt.update(expected_ply=0, expected_fen=chess.Board().fen(), uci="d2d4")
        with self.assertRaisesRegex(GameStateError, "differs from pending move"):
            GameState.from_json(saved)

    def test_all_receipt_lifecycles_require_exact_historical_fen(self) -> None:
        for lifecycle in ("prepared", "in_flight", "committed", "aborted"):
            with self.subTest(lifecycle=lifecycle):
                state = bound_game()
                prepare(state)
                receipt = state.remote_receipts["move-1"]
                receipt.state = lifecycle
                if lifecycle == "committed":
                    commit_simple_move(state, "e2e4")
                elif lifecycle == "aborted":
                    state.pending = None
                saved = state.to_json()
                altered = chess.Board(receipt.expected_fen)
                altered.halfmove_clock = 7
                saved["remote_receipts"]["move-1"]["expected_fen"] = altered.fen()
                with self.assertRaisesRegex(GameStateError, "fence differs from game history"):
                    GameState.from_json(saved)

    def test_committed_receipt_must_match_history_and_be_unique_for_its_ply(self) -> None:
        state = bound_game()
        receipt = command(state)
        receipt.state = "committed"
        state.remote_receipts[receipt.request_id] = receipt
        commit_simple_move(state, "e2e4")
        saved = state.to_json()
        wrong_move = deepcopy(saved)
        wrong_move["remote_receipts"]["move-1"]["uci"] = "d2d4"
        duplicate = deepcopy(saved)
        duplicate["remote_receipts"]["move-2"] = {
            **duplicate["remote_receipts"]["move-1"], "request_id": "move-2",
        }
        for damaged in (wrong_move, duplicate):
            with self.assertRaisesRegex(GameStateError, "receipt differs from game history"):
                GameState.from_json(damaged)

    def test_resolution_requires_exact_historical_fen_and_legal_uci(self) -> None:
        state = bound_game()
        state.remote_resolutions["spoken-1"] = RemoteMoveResolution(
            expression="pawn to e4", command=command(state, request_id="spoken-1"),
        )
        saved = state.to_json()
        for field, value in (("expected_ply", 1), ("uci", "e2e5"),
                             ("expected_fen", chess.Board().fen().replace("0 1", "4 1"))):
            with self.subTest(field=field):
                damaged = deepcopy(saved)
                damaged["remote_resolutions"]["spoken-1"]["payload"][field] = value
                with self.assertRaises(GameStateError):
                    GameState.from_json(damaged)

    def test_resolution_and_same_request_receipt_must_agree(self) -> None:
        state = bound_game()
        prepare(state)
        state.remote_resolutions["move-1"] = RemoteMoveResolution(
            expression="pawn to e4", command=command(state),
        )
        saved = state.to_json()
        saved["remote_resolutions"]["move-1"]["payload"]["uci"] = "d2d4"
        with self.assertRaisesRegex(GameStateError, "receipt differs from saved move resolution"):
            GameState.from_json(saved)

    def test_unsubmitted_resolution_and_aborted_receipt_can_survive_a_different_move(self) -> None:
        state = bound_game()
        unsubmitted = command(state, "d2d4", "spoken-1")
        state.remote_resolutions["spoken-1"] = RemoteMoveResolution(
            expression="pawn to d4", command=unsubmitted,
        )
        aborted = command(state, "b1c3", "move-aborted")
        aborted.state = "aborted"
        state.remote_receipts[aborted.request_id] = aborted
        commit_simple_move(state, "e2e4")
        saved = state.to_json()
        restored = GameState.from_json(saved)
        self.assertEqual(restored.to_json(), saved)
        payload = restored.remote_resolutions["spoken-1"].payload
        payload["uci"] = "e2e4"
        self.assertEqual(restored.remote_resolutions["spoken-1"].command.uci, "d2d4")

    def test_malformed_current_remote_fields_return_state_errors(self) -> None:
        state = bound_game()
        prepare(state)
        saved = state.to_json()
        for field, invalid in (("remote_mode", []), ("remote_human", {}),
                               ("remote_resolutions", []), ("remote_receipts", [])):
            with self.subTest(field=field):
                damaged = deepcopy(saved)
                damaged[field] = invalid
                with self.assertRaises(GameStateError):
                    GameState.from_json(damaged)
        for field, invalid in (("expected_ply", True), ("expected_ply", -1),
                               ("state", []), ("uci", "0000")):
            with self.subTest(receipt_field=field):
                damaged = deepcopy(saved)
                damaged["remote_receipts"]["move-1"][field] = invalid
                with self.assertRaises(GameStateError):
                    GameState.from_json(damaged)


if __name__ == "__main__":
    unittest.main()

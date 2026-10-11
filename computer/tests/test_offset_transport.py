"""Host-side behavior of the in-square rook nudge command."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chess_table_host.game import GameStateError, ReconciliationRequired
from chess_table_host.gui import GameController
from chess_table_host.link_adapters import SimulatedLink
from chess_table_host.planner import BoardSquare, RookOffset, Transfer
from chess_table_host.play import _connect_physical, _describe_action


class OffsetTransportTests(unittest.TestCase):
    def test_simulated_offset_preserves_occupied_square(self) -> None:
        link = SimulatedLink()
        with redirect_stdout(io.StringIO()):
            link.set_map("80" + "00" * 11)  # h1, firmware square ID 7
            before = link.bitmap
            self.assertEqual(link.offset(7, "SE", "OUT").verb, "OFFSET")
            self.assertEqual(link.offset(7, "SE", "IN").verb, "OFFSET")
        self.assertEqual(link.bitmap, before)
        self.assertEqual(link.command_id, 2)
        with self.assertRaisesRegex(GameStateError, "empty square"):
            link.offset(6, "SE", "OUT")

    def test_simulated_offset_carry_changes_occupancy_once(self) -> None:
        link = SimulatedLink()
        with redirect_stdout(io.StringIO()):
            link.set_map("80" + "00" * 11)
            link.offset(7, "NE", "OUT")
            completed = link.carry_offset("7", "NE", "5", "F173")
        self.assertEqual(completed.verb, "CARRY_OFFSET")
        self.assertFalse(link.bitmap & (1 << 7))
        self.assertTrue(link.bitmap & (1 << 5))

    def test_physical_session_requires_offset_before_homing(self) -> None:
        class OldFirmware:
            def __init__(self):
                self.stream = io.BytesIO()
                self.homed = False
                self.closed = False

            def hello(self):
                return SimpleNamespace(
                    version=1,
                    capabilities={"STATUS", "HOME", "REF", "MAP", "CARRY", "CARRY_DIAG", "GEOM"},
                )

            def home(self):
                self.homed = True

            def close(self):
                self.closed = True
                self.stream.close()

        firmware = OldFirmware()
        with tempfile.TemporaryDirectory() as directory:
            with patch("chess_table_host.play.open_serial", return_value=firmware):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(GameStateError, "OFFSET"):
                        _connect_physical(
                            "COM3", full_home=True,
                            serial_log=Path(directory) / "serial.jsonl",
                        )
        self.assertFalse(firmware.homed)
        self.assertTrue(firmware.closed)

    def test_cli_describes_nudge_and_recenter(self) -> None:
        out = RookOffset("R", BoardSquare("h1"), "SE", "OUT", "castle_rook_out")
        back = RookOffset("R", BoardSquare("h1"), "SE", "IN", "castle_rook_in")
        self.assertIn("nudge toward SE corner within h1", _describe_action(out))
        self.assertIn("recenter from SE corner within h1", _describe_action(back))
        carry = Transfer("R", BoardSquare("h1"), BoardSquare("f1"),
                         "castle_rook_from_offset")
        self.assertIn("h1 offset corner -> f1", _describe_action(carry))

    def test_gui_stop_gate_blocks_checkpointed_offset_before_send(self) -> None:
        class RecordingLink:
            def __init__(self):
                self.sent = False

            def offset(self, *_arguments):
                self.sent = True

        controller = object.__new__(GameController)
        controller._stop = threading.Event()
        link = RecordingLink()
        motion = controller._motion_link(link)
        controller._stop.set()
        with self.assertRaisesRegex(ReconciliationRequired, "piece nudge"):
            motion.offset(7, "SE", "OUT")
        self.assertFalse(link.sent)

    def test_gui_stop_gate_blocks_checkpointed_offset_carry_before_send(self) -> None:
        class RecordingLink:
            sent = False

            def carry_offset(self, *_arguments):
                self.sent = True

        controller = object.__new__(GameController)
        controller._stop = threading.Event()
        link = RecordingLink()
        motion = controller._motion_link(link)
        controller._stop.set()
        with self.assertRaisesRegex(ReconciliationRequired, "offset carry"):
            motion.carry_offset("7", "NE", "5", "F173")
        self.assertFalse(link.sent)


if __name__ == "__main__":
    unittest.main()

import unittest
from collections import deque

from chess_table_host import HalfSquarePoint, RouteGeometry
from chess_table_host.serial_link import (
    CommandIdExhausted,
    ControllerError,
    ControllerReset,
    LinkBusy,
    LinkDisconnected,
    LinkTimeout,
    ProtocolError,
    SerialLink,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeStream:
    def __init__(self, clock: FakeClock, on_write=None) -> None:
        self.clock = clock
        self.on_write = on_write
        self.incoming = deque()
        self.written = []
        self.fail_read = False
        self.fail_write = False

    def feed(self, lines: str) -> None:
        self.incoming.extend(bytes([byte]) for byte in lines.encode("ascii"))

    def read(self, size: int) -> bytes:
        assert size == 1
        if self.fail_read:
            raise OSError("USB disconnected")
        if self.incoming:
            return self.incoming.popleft()
        self.clock.now += 0.05
        return b""

    def write(self, data: bytes) -> int:
        if self.fail_write:
            raise OSError("USB disconnected")
        self.written.append(data)
        if self.on_write is not None:
            self.on_write(data, self)
        return len(data)


def link_with_script(script, *, first_command_id=1):
    clock = FakeClock()
    stream = FakeStream(clock, script)
    link = SerialLink(
        stream,
        clock=clock,
        ack_timeout=0.2,
        completion_timeout=0.3,
        first_command_id=first_command_id,
    )
    return link, stream


class SerialLinkTests(unittest.TestCase):
    def test_hello_status_and_generic_request(self) -> None:
        def script(data, stream):
            command_id = data.decode().split()[0]
            verb = data.decode().split()[1]
            if verb == "HELLO":
                payload = "HELLO V=1 CAPS=STATUS,HOME,REF,STOP"
            elif verb == "STATUS":
                payload = "STATUS REF=1 BUSY=0 MAP=1 SAVED=1"
            else:
                payload = "CARRY"
            stream.feed(f"{command_id} ACK\n{command_id} DONE {payload}\n")

        link, stream = link_with_script(script)
        hello = link.hello()
        self.assertEqual(hello.version, 1)
        self.assertEqual(hello.capabilities, frozenset({"STATUS", "HOME", "REF", "STOP"}))
        status = link.status()
        self.assertTrue(status.referenced)
        self.assertFalse(status.busy)
        self.assertTrue(status.map_known)
        self.assertTrue(status.saved_geometry)
        route = "a1:b2," * 14  # 84 ASCII bytes; total framed command is 94.
        reply = link.request("CARRY", route)
        self.assertEqual(reply.payload, ("CARRY",))
        self.assertEqual(stream.written, [
            b"@1 HELLO\n", b"@2 STATUS\n", f"@3 CARRY {route}\n".encode(),
        ])
        self.assertFalse(link.physical_state_uncertain)

    def test_typed_map_and_carry_emit_exact_frames(self) -> None:
        def script(data, stream):
            command_id, verb = data.decode().split()[:2]
            stream.feed(f"{command_id} ACK\n{command_id} DONE {verb}\n")

        link, stream = link_with_script(script)
        bitmap = "00FF00000000000000A5FFFF"
        self.assertEqual(link.set_map(bitmap).payload, ("MAP",))
        self.assertEqual(link.carry("0", "95", "113355GG").payload, ("CARRY",))
        self.assertEqual(link.save_geometry().payload, ("SAVE",))
        self.assertEqual(
            stream.written,
            [
                f"@1 MAP {bitmap}\n".encode(),
                b"@2 CARRY 0 95 113355GG\n",
                b"@3 SAVE\n",
            ],
        )

    def test_geometry_reports_referenced_machine_dimensions(self) -> None:
        payload = "GEOM X=251.0850 Y=251.9625 OX=0.5425 OY=0.9813 P=31.2500 I=2.0000 PPM=133.3333"
        link, stream = link_with_script(
            lambda data, io: io.feed(f"@1 ACK\n@1 DONE {payload}\n")
        )
        geometry = link.geometry()
        self.assertEqual(stream.written, [b"@1 GEOM\n"])
        self.assertEqual(geometry.x_travel_mm, 251.085)
        self.assertEqual(geometry.board_origin_y_mm, 0.9813)
        self.assertEqual(geometry.pulses_per_mm, 133.3333)
        route_geometry = RouteGeometry(
            geometry.square_pitch_mm, 15, 15, margin_mm=0.3,
            x_travel_mm=geometry.x_travel_mm,
            y_travel_mm=geometry.y_travel_mm,
            end_inset_mm=geometry.end_inset_mm,
            pulses_per_mm=geometry.pulses_per_mm,
            board_origin_x_mm=geometry.board_origin_x_mm,
            board_origin_y_mm=geometry.board_origin_y_mm,
        )
        self.assertGreater(route_geometry.physical_point_mm(HalfSquarePoint(0, 0))[0], 0)

        for bad in ("GEOM X=nan Y=1 OX=0 OY=0 P=31 I=2 PPM=133",
                    "GEOM X=1 Y=1 OX=0 OY=0 P=31 I=2",
                    "GEOM X=1 Y=1 OX=0 OY=0 P=31 I=2 PPM=oops"):
            with self.subTest(payload=bad):
                broken, _ = link_with_script(
                    lambda data, io: io.feed(f"@1 ACK\n@1 DONE {bad}\n")
                )
                with self.assertRaises(ProtocolError):
                    broken.geometry()
                self.assertTrue(broken.physical_state_uncertain)

        unreferenced, _ = link_with_script(
            lambda data, io: io.feed("@1 ACK\n@1 ERR REFERENCE\n")
        )
        with self.assertRaises(ControllerError) as ctx:
            unreferenced.geometry()
        self.assertFalse(ctx.exception.requires_reconciliation)
        self.assertFalse(link.physical_state_uncertain)

    def test_typed_map_and_carry_reject_invalid_arguments_before_writing(self) -> None:
        link, stream = link_with_script(lambda data, stream: None)
        for value in ("F" * 23, "F" * 25, "f" * 24, "G" * 24, b"F" * 24, None):
            with self.subTest(map=value), self.assertRaises(ValueError):
                link.set_map(value)
        invalid_routes = (
            (-1, 1, "1131"),
            (96, 1, "1131"),
            (True, 1, "1131"),
            (1.0, 2, "1131"),
            ("01", 2, "1131"),
            ("96", 2, "1131"),
            ("1.0", 2, "1131"),
            (0, 96, "1131"),
            (0, 0, "1131"),
            (0, 1, "113"),
            (0, 1, "1" * 25),
            (0, 1, "11hh"),
            (0, 1, "11HH"),
            (0, 1, b"1131"),
        )
        for source, destination, points in invalid_routes:
            with self.subTest(carry=(source, destination, points)), self.assertRaises(ValueError):
                link.carry(source, destination, points)
        self.assertEqual(stream.written, [])

    def test_carry_preflight_rejections_leave_known_physical_state(self) -> None:
        for code in ("SYNTAX", "BUSY", "REFERENCE", "MAP", "ROUTE"):
            with self.subTest(code=code):
                link, _ = link_with_script(
                    lambda data, stream: stream.feed(f"@1 ACK\n@1 ERR {code}\n")
                )
                with self.assertRaises(ControllerError) as ctx:
                    link.carry(0, 1, "1131")
                self.assertEqual(ctx.exception.code, code)
                self.assertFalse(ctx.exception.requires_reconciliation)
                self.assertFalse(link.physical_state_uncertain)

    def test_carry_runtime_or_unknown_error_requires_reconciliation(self) -> None:
        for code in ("MOTION", "GEOMETRY", "CANCELED", "DUPLICATE", "UNKNOWN", "FUTURE_ERROR"):
            with self.subTest(code=code):
                link, _ = link_with_script(
                    lambda data, stream: stream.feed(f"@1 ACK\n@1 ERR {code}\n")
                )
                with self.assertRaises(ControllerError) as ctx:
                    link.carry(0, 1, "1131")
                self.assertTrue(ctx.exception.requires_reconciliation)
                self.assertTrue(link.physical_state_uncertain)

    def test_geometry_after_home_is_uncertain_but_map_busy_is_not(self) -> None:
        link, _ = link_with_script(lambda data, stream: stream.feed("@1 ACK\n@1 ERR GEOMETRY\n"))
        with self.assertRaises(ControllerError) as ctx:
            link.home()
        self.assertTrue(ctx.exception.requires_reconciliation)

        link, _ = link_with_script(lambda data, stream: stream.feed("@1 ACK\n@1 ERR BUSY\n"))
        with self.assertRaises(ControllerError) as ctx:
            link.set_map("0" * 24)
        self.assertFalse(ctx.exception.requires_reconciliation)

    def test_stop_cancels_pending_carry_and_marks_state_uncertain(self) -> None:
        def script(data, stream):
            if b" CARRY" in data:
                stream.feed("@1 ACK\n")
            elif b" STOP" in data:
                stream.feed("@1 ERR CANCELED\n@2 ACK\n@2 DONE STOP\n")

        link, stream = link_with_script(script)
        carry = link.start("CARRY", "0", "1", "1131")
        link.poll()
        self.assertTrue(carry.acknowledged)
        link.stop()
        with self.assertRaises(ControllerError) as ctx:
            link.wait(carry)
        self.assertEqual(ctx.exception.code, "CANCELED")
        self.assertTrue(ctx.exception.requires_reconciliation)
        self.assertTrue(link.physical_state_uncertain)
        self.assertEqual(stream.written, [b"@1 CARRY 0 1 1131\n", b"@2 STOP\n"])

    def test_reset_during_carry_aborts_without_completion(self) -> None:
        link, _ = link_with_script(lambda data, stream: stream.feed("@1 ACK\n!BOOT V=1\n"))
        with self.assertRaises(ControllerReset):
            link.carry(0, 1, "1131")
        self.assertTrue(link.physical_state_uncertain)

    def test_typed_carry_rejects_unexpected_done_payload(self) -> None:
        link, _ = link_with_script(lambda data, stream: stream.feed("@1 ACK\n@1 DONE CARRY OK\n"))
        with self.assertRaises(ProtocolError):
            link.carry(0, 1, "1131")
        self.assertTrue(link.physical_state_uncertain)

    def test_legacy_diagnostic_lines_are_ignored_and_partial_frames_kept(self) -> None:
        def script(data, stream):
            stream.feed("X: ready; MIN=released " + ("diagnostic " * 20) + "\n@1 ACK\n@1 DONE HOME\n")

        link, _ = link_with_script(script)
        self.assertEqual(link.home().payload, ("HOME",))

    def test_only_one_normal_command_but_stop_can_preempt(self) -> None:
        def script(data, stream):
            if b" HOME" in data:
                stream.feed("@1 ACK\n")
            elif b" STOP" in data:
                stream.feed("@1 ERR CANCELED\n@2 ACK\n@2 DONE STOP\n")

        link, stream = link_with_script(script)
        home = link.start("HOME")
        with self.assertRaises(LinkBusy):
            link.start("STATUS")
        self.assertIsNone(link.poll())  # HOME ACK
        self.assertTrue(home.acknowledged)
        self.assertEqual(link.stop().payload, ("STOP",))
        with self.assertRaises(ControllerError) as ctx:
            link.wait(home)
        self.assertEqual(ctx.exception.code, "CANCELED")
        self.assertTrue(link.physical_state_uncertain)
        self.assertEqual(stream.written, [b"@1 HOME\n", b"@2 STOP\n"])

    def test_normal_request_waits_until_pending_stop_finishes(self) -> None:
        link, _ = link_with_script(lambda data, stream: None)
        link.start("STOP")
        with self.assertRaises(LinkBusy):
            link.start("STATUS")
        with self.assertRaises(LinkBusy):
            link.start("STOP")

    def test_rejected_busy_is_explicit_without_guessing_motion(self) -> None:
        def script(data, stream):
            stream.feed("@1 ACK\n@1 ERR BUSY\n")

        link, _ = link_with_script(script)
        with self.assertRaises(ControllerError) as ctx:
            link.home()
        self.assertEqual(ctx.exception.command_id, 1)
        self.assertEqual(ctx.exception.code, "BUSY")
        self.assertFalse(link.physical_state_uncertain)

    def test_duplicate_id_from_controller_marks_session_ambiguous(self) -> None:
        def script(data, stream):
            stream.feed("@1 ACK\n@1 ERR DUPLICATE\n")

        link, _ = link_with_script(script)
        with self.assertRaises(ControllerError) as ctx:
            link.hello()
        self.assertEqual(ctx.exception.code, "DUPLICATE")
        self.assertTrue(link.physical_state_uncertain)

    def test_ack_and_completion_have_separate_timeouts(self) -> None:
        link, _ = link_with_script(lambda data, stream: None)
        with self.assertRaises(LinkTimeout) as ctx:
            link.home()
        self.assertEqual(ctx.exception.phase, "ACK")
        self.assertTrue(link.physical_state_uncertain)

        def ack_only(data, stream):
            stream.feed("@1 ACK\n")

        link, _ = link_with_script(ack_only)
        with self.assertRaises(LinkTimeout) as ctx:
            link.home()
        self.assertEqual(ctx.exception.phase, "DONE/ERR")
        self.assertTrue(link.physical_state_uncertain)

    def test_stop_ignores_late_response_from_timed_out_id(self) -> None:
        def script(data, stream):
            if b" STOP" in data:
                stream.feed("@1 ACK\n@1 ERR CANCELED\n@2 ACK\n@2 DONE STOP\n")

        link, _ = link_with_script(script)
        with self.assertRaises(LinkTimeout):
            link.home()
        self.assertEqual(link.stop().payload, ("STOP",))
        self.assertTrue(link.physical_state_uncertain)

    def test_boot_aborts_request_and_records_uncertainty(self) -> None:
        def script(data, stream):
            stream.feed("@1 ACK\n!BOOT V=1\n")

        link, _ = link_with_script(script)
        with self.assertRaises(ControllerReset):
            link.home()
        self.assertEqual(link.boot_count, 1)
        self.assertEqual(link.take_events()[0].name, "BOOT")
        self.assertTrue(link.physical_state_uncertain)

    def test_idle_boot_event_is_available_to_caller(self) -> None:
        link, stream = link_with_script(lambda data, stream: None)
        stream.feed("!BOOT V=1\n")
        event = link.poll()
        self.assertEqual(event.name, "BOOT")
        self.assertTrue(link.physical_state_uncertain)

    def test_explicit_boot_starts_a_new_id_epoch(self) -> None:
        def script(data, stream):
            stream.feed("@1 ACK\n@1 DONE HELLO V=1 CAPS=STATUS,HOME,REF,STOP\n")

        link, stream = link_with_script(script, first_command_id=65535)
        stream.feed("!BOOT V=1\n")
        link.poll()
        self.assertEqual(link.hello().version, 1)
        self.assertEqual(stream.written, [b"@1 HELLO\n"])
        self.assertTrue(link.physical_state_uncertain)

    def test_hello_retries_once_after_startup_boot_without_reusing_pending_id(self) -> None:
        def script(data, stream):
            command_id = data.decode().split()[0]
            stream.feed(
                f"{command_id} ACK\n"
                f"{command_id} DONE HELLO V=1 CAPS=STATUS,HOME,REF,STOP\n"
            )

        link, stream = link_with_script(script)
        stream.feed("!BOOT V=1\n")
        self.assertEqual(link.hello().version, 1)
        self.assertEqual(stream.written, [b"@1 HELLO\n", b"@2 HELLO\n"])
        self.assertEqual(link.boot_count, 1)
        self.assertTrue(link.physical_state_uncertain)

    def test_hello_does_not_retry_timeout_without_boot(self) -> None:
        link, stream = link_with_script(lambda data, stream: None)
        with self.assertRaises(LinkTimeout):
            link.hello()
        self.assertEqual(stream.written, [b"@1 HELLO\n"])

    def test_disconnect_aborts_request(self) -> None:
        link, stream = link_with_script(lambda data, stream: None)
        stream.fail_read = True
        with self.assertRaises(LinkDisconnected):
            link.home()
        self.assertTrue(link.physical_state_uncertain)

    def test_malformed_duplicate_and_wrong_id_frames_fail_closed(self) -> None:
        for response in (
            "@1 DONE HOME\n",  # no ACK
            "@1 ACK\n@1 ACK\n",
            "@99 ACK\n",
            "@1 WHATEVER\n",
            "@1 ACK extra\n",
        ):
            with self.subTest(response=response):
                link, _ = link_with_script(lambda data, stream: stream.feed(response))
                with self.assertRaises(ProtocolError):
                    link.home()
                self.assertTrue(link.physical_state_uncertain)

    def test_duplicate_terminal_after_success_is_detected(self) -> None:
        def script(data, stream):
            stream.feed("@1 ACK\n@1 DONE REF\n@1 DONE REF\n")

        link, _ = link_with_script(script)
        self.assertEqual(link.reference().payload, ("REF",))
        with self.assertRaises(ProtocolError):
            link.poll()
        self.assertTrue(link.physical_state_uncertain)

    def test_invalid_payload_and_version_fail_closed(self) -> None:
        for payload in ("HELLO V=2 CAPS=STATUS", "HELLO V=one CAPS=STATUS", "HELLO V=1"):
            with self.subTest(payload=payload):
                link, _ = link_with_script(
                    lambda data, stream: stream.feed(f"@1 ACK\n@1 DONE {payload}\n")
                )
                with self.assertRaises(ProtocolError):
                    link.hello()
                self.assertTrue(link.physical_state_uncertain)

    def test_command_validation_and_id_exhaustion(self) -> None:
        link, stream = link_with_script(lambda data, stream: stream.feed("@65535 ACK\n@65535 DONE STOP\n"), first_command_id=65535)
        with self.assertRaises(ValueError):
            link.start("home")
        with self.assertRaises(ValueError):
            link.start("CARRY", "a1\nb2")
        separate_link, _ = link_with_script(lambda data, stream: None)
        with self.assertRaises(ValueError):
            separate_link.start("CARRY", "x" * 125)
        with self.assertRaises(ValueError):
            separate_link.start("CARRY", "x" * 87)  # 97 bytes including frame.
        self.assertEqual(link.stop().command_id, 65535)
        with self.assertRaises(CommandIdExhausted):
            link.start("HELLO")
        self.assertEqual(stream.written, [b"@65535 STOP\n"])

    def test_final_normal_id_reserves_one_id_for_preemptive_stop(self) -> None:
        def script(data, stream):
            if b" HOME" in data:
                stream.feed("@65534 ACK\n")
            else:
                stream.feed("@65534 ERR CANCELED\n@65535 ACK\n@65535 DONE STOP\n")

        link, stream = link_with_script(script, first_command_id=65534)
        home = link.start("HOME")
        self.assertEqual(link.stop().command_id, 65535)
        with self.assertRaises(ControllerError):
            link.wait(home)
        with self.assertRaises(CommandIdExhausted):
            link.start("STATUS")
        self.assertEqual(stream.written, [b"@65534 HOME\n", b"@65535 STOP\n"])


if __name__ == "__main__":
    unittest.main()

"""Version 1 serial transport for the chess-table controller.

This module deliberately reports *command* completion, not piece delivery.
The controller has no sensor that can prove a magnet picked up or released a
piece. Callers must keep their chess and parking inventory uncommitted until
they have reconciled the physical operation.

The injected stream needs ``read(1)`` and ``write(bytes)`` methods. Reads must
return within a short, finite timeout (empty bytes means no data yet). This is
the behavior configured by :func:`open_serial` and makes the state machine
usable with an in-memory stream in tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
import re
import threading
import time
from typing import Callable, Deque, Protocol

from .protocol_limits import MAX_CARRY_WAYPOINTS


BAUD_RATE = 115200
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 96
# Controller capability lists can exceed the Uno's command-buffer limit.
# Keep outgoing commands bounded separately from incoming replies.
MAX_REPLY_BYTES = 128
MAX_COMMAND_ID = 65535
_TOKEN = re.compile(r"^[!-~]+$")
_REPLY = re.compile(r"^@([1-9][0-9]{0,4}) (ACK|DONE|ERR)(?: (.*))?$")
_EVENT = re.compile(r"^!([A-Z][A-Z0-9_]*)(?: (.*))?$")
_SAFE_REJECTIONS = frozenset({"BUSY", "SYNTAX", "LENGTH"})
_MOTION_PREFLIGHT_REJECTIONS = frozenset({"REFERENCE", "MAP", "ROUTE"})
_HEX_MAP = re.compile(r"^[0-9A-F]{24}$")
_BASE17_POINTS = re.compile(r"^[0-9A-G]+$")
_CORNERS = frozenset({"NW", "NE", "SW", "SE"})


class ByteStream(Protocol):
    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...


class LinkError(Exception):
    """Base class for transport and controller failures."""


class LinkBusy(LinkError):
    """A normal request is already pending (STOP is the one exception)."""


class CommandIdExhausted(LinkError):
    """The 16-bit ID space cannot be reused safely in this session."""


class LinkTimeout(LinkError):
    def __init__(self, command_id: int, phase: str):
        self.command_id = command_id
        self.phase = phase
        super().__init__(f"command {command_id} timed out waiting for {phase}")


class LinkDisconnected(LinkError):
    """Serial I/O failed or the stream was closed."""


class ProtocolError(LinkError):
    """A framed response was malformed or violated the request sequence."""


class ControllerReset(LinkError):
    """The controller booted while a request was outstanding."""


class ControllerFault(LinkError):
    """An unsolicited controller fault interrupted a request."""


class ControllerError(LinkError):
    def __init__(self, command_id: int, code: str, *, requires_reconciliation: bool = True):
        self.command_id = command_id
        self.code = code
        self.requires_reconciliation = requires_reconciliation
        super().__init__(f"controller rejected command {command_id}: {code}")


@dataclass(frozen=True)
class Completion:
    command_id: int
    verb: str
    payload: tuple[str, ...]


@dataclass(frozen=True)
class LinkEvent:
    name: str
    payload: tuple[str, ...]


@dataclass(frozen=True)
class HelloInfo:
    version: int
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ControllerStatus:
    referenced: bool
    busy: bool
    map_known: bool
    saved_geometry: bool


@dataclass(frozen=True)
class ControllerGeometry:
    """Referenced controller geometry in millimeters and pulses per mm."""

    x_travel_mm: float
    y_travel_mm: float
    board_origin_x_mm: float
    board_origin_y_mm: float
    square_pitch_mm: float
    end_inset_mm: float
    pulses_per_mm: float


@dataclass
class PendingCommand:
    command_id: int
    verb: str
    acknowledged: bool = False
    completion: Completion | None = None
    error: LinkError | None = None

    @property
    def finished(self) -> bool:
        return self.completion is not None or self.error is not None


class SerialLink:
    """One normal request at a time, with STOP permitted to preempt it.

    ``start``/``wait`` allow an event loop to send STOP while HOME or REF is
    pending. ``request`` is the synchronous convenience form. ``poll`` is safe
    to call from two waiting threads: only one thread reads a byte at a time,
    and each reply is routed by ID to its own :class:`PendingCommand`.
    """

    def __init__(
        self,
        stream: ByteStream,
        *,
        ack_timeout: float = 2.0,
        completion_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        first_command_id: int = 1,
    ) -> None:
        if ack_timeout <= 0 or completion_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not 1 <= first_command_id <= MAX_COMMAND_ID:
            raise ValueError("first_command_id must be in 1..65535")
        self.stream = stream
        self.ack_timeout = ack_timeout
        self.completion_timeout = completion_timeout
        self._clock = clock
        self._next_id = first_command_id
        self._pending: dict[int, PendingCommand] = {}
        self._normal_id: int | None = None
        self._stop_id: int | None = None
        self._abandoned_ids: set[int] = set()
        self._receive = bytearray()
        self._discard_legacy_line = False
        self._state_lock = threading.RLock()
        self._pump_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._events: Deque[LinkEvent] = deque(maxlen=64)
        self.physical_state_uncertain = False
        self.boot_count = 0
        self.capabilities: frozenset[str] | None = None

    def start(self, verb: str, *arguments: str) -> PendingCommand:
        """Send one command; call :meth:`wait` or :meth:`poll` for its result."""
        # Serialize ID allocation and writes together. Otherwise a concurrent
        # STOP could reach the Uno before the lower-ID motion request.
        with self._write_lock:
            return self._start_serialized(verb, *arguments)

    def _start_serialized(self, verb: str, *arguments: str) -> PendingCommand:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", verb):
            raise ValueError("verb must contain uppercase ASCII letters, digits, or underscore")
        if any(not _TOKEN.fullmatch(arg) for arg in arguments):
            raise ValueError("arguments must be nonempty ASCII tokens without whitespace")
        with self._state_lock:
            if verb == "STOP":
                if self._stop_id is not None:
                    raise LinkBusy("a STOP command is already pending")
            elif self._normal_id is not None or self._stop_id is not None:
                raise LinkBusy("another command is already pending")
            if self._next_id > MAX_COMMAND_ID:
                raise CommandIdExhausted("open a new controller session before ID wrap")
            if verb != "STOP" and self._next_id == MAX_COMMAND_ID:
                raise CommandIdExhausted("command ID 65535 is reserved for emergency STOP")
            command_id = self._next_id
            frame = f"@{command_id} {verb}"
            if arguments:
                frame += " " + " ".join(arguments)
            data = (frame + "\n").encode("ascii")
            if len(data) > MAX_FRAME_BYTES:
                raise ValueError("command exceeds maximum frame length")
            self._next_id += 1
            pending = PendingCommand(command_id, verb)
            self._pending[command_id] = pending
            if verb == "STOP":
                self._stop_id = command_id
            else:
                self._normal_id = command_id
        try:
            written = self.stream.write(data)
            if written is not None and written != len(data):
                raise OSError("short serial write")
        except (OSError, ValueError) as exc:
            error = LinkDisconnected(str(exc))
            self._fail_all(error)
            raise error from exc
        return pending

    def wait(
        self,
        pending: PendingCommand,
        *,
        ack_timeout: float | None = None,
        completion_timeout: float | None = None,
    ) -> Completion:
        """Wait for ACK and a matching DONE or ERR, with bounded deadlines."""
        ack_limit = self.ack_timeout if ack_timeout is None else ack_timeout
        done_limit = self.completion_timeout if completion_timeout is None else completion_timeout
        if ack_limit <= 0 or done_limit <= 0:
            raise ValueError("timeouts must be positive")
        ack_deadline = self._clock() + ack_limit
        done_deadline: float | None = None
        while True:
            with self._state_lock:
                if pending.error is not None:
                    raise pending.error
                if pending.completion is not None:
                    return pending.completion
                if pending.acknowledged and done_deadline is None:
                    done_deadline = self._clock() + done_limit
            deadline = ack_deadline if done_deadline is None else done_deadline
            if self._clock() >= deadline:
                phase = "ACK" if done_deadline is None else "DONE/ERR"
                error = LinkTimeout(pending.command_id, phase)
                if self._retire(pending, error, abandoned=True):
                    raise error
                # A different polling thread completed the command at the
                # deadline. Read its recorded terminal result instead.
                continue
            self.poll()

    def request(self, verb: str, *arguments: str, timeout: float | None = None) -> Completion:
        pending = self.start(verb, *arguments)
        return self.wait(pending, completion_timeout=timeout)

    def poll(self) -> LinkEvent | Completion | None:
        """Process at most one complete line; retain partial bytes for later."""
        with self._pump_lock:
            while True:
                try:
                    incoming = self.stream.read(1)
                except (OSError, ValueError) as exc:
                    error = LinkDisconnected(str(exc))
                    self._fail_all(error)
                    raise error from exc
                if not incoming:
                    return None
                if not isinstance(incoming, bytes) or len(incoming) != 1:
                    return self._protocol_failure("read(1) must return one byte")
                if incoming == b"\n":
                    if self._discard_legacy_line:
                        self._discard_legacy_line = False
                        continue
                    line = bytes(self._receive).rstrip(b"\r")
                    self._receive.clear()
                    if not line:
                        continue
                    try:
                        decoded = line.decode("ascii")
                    except UnicodeDecodeError:
                        return self._protocol_failure("non-ASCII serial frame")
                    return self._handle_line(decoded)
                if self._discard_legacy_line:
                    continue
                if not self._receive and incoming not in (b"@", b"!"):
                    self._discard_legacy_line = True
                    continue
                self._receive.extend(incoming)
                if len(self._receive) > MAX_REPLY_BYTES:
                    self._receive.clear()
                    return self._protocol_failure("serial frame too long")

    def take_events(self) -> tuple[LinkEvent, ...]:
        with self._state_lock:
            result = tuple(self._events)
            self._events.clear()
            return result

    def hello(self, *, timeout: float = 5.0) -> HelloInfo:
        # Opening an Uno's USB serial port commonly resets it. If its BOOT
        # frame interrupts the first HELLO, retry once with the next ID. A
        # general timeout is never retried: the command may have executed.
        try:
            reply = self.request("HELLO", timeout=timeout)
        except ControllerReset:
            reply = self.request("HELLO", timeout=timeout)
        fields = self._fields(reply, "HELLO", {"V", "CAPS"})
        try:
            version = int(fields["V"])
        except ValueError as exc:
            return self._protocol_failure("invalid protocol version", exc)
        if version != PROTOCOL_VERSION:
            return self._protocol_failure(f"unsupported controller protocol version {version}")
        capabilities = frozenset(fields["CAPS"].split(","))
        if not capabilities or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", cap) for cap in capabilities):
            return self._protocol_failure("invalid capability list")
        with self._state_lock:
            self.capabilities = capabilities
        return HelloInfo(version, capabilities)

    def status(self, *, timeout: float = 5.0) -> ControllerStatus:
        reply = self.request("STATUS", timeout=timeout)
        fields = self._fields(reply, "STATUS", {"REF", "BUSY", "MAP", "SAVED"})
        if any(value not in ("0", "1") for value in fields.values()):
            return self._protocol_failure("invalid status boolean")
        return ControllerStatus(
            referenced=fields["REF"] == "1",
            busy=fields["BUSY"] == "1",
            map_known=fields["MAP"] == "1",
            saved_geometry=fields["SAVED"] == "1",
        )

    def home(self, *, timeout: float | None = None) -> Completion:
        reply = self.request("HOME", timeout=timeout)
        self._expect_plain_done(reply, "HOME")
        return reply

    def reference(self, *, timeout: float | None = None) -> Completion:
        reply = self.request("REF", timeout=timeout)
        self._expect_plain_done(reply, "REF")
        return reply

    def geometry(self, *, timeout: float = 5.0) -> ControllerGeometry:
        """Read the live referenced spans, origins, pitch, inset and pulse scale."""
        reply = self.request("GEOM", timeout=timeout)
        fields = self._fields(reply, "GEOM", {"X", "Y", "OX", "OY", "P", "I", "PPM"})
        try:
            values = {name: float(value) for name, value in fields.items()}
        except ValueError as exc:
            return self._protocol_failure("invalid GEOM number", exc)
        if any(not isfinite(value) for value in values.values()) or any(
            values[name] <= 0 for name in ("X", "Y", "P", "I", "PPM")
        ):
            return self._protocol_failure("invalid GEOM dimensions")
        return ControllerGeometry(
            values["X"], values["Y"], values["OX"], values["OY"],
            values["P"], values["I"], values["PPM"],
        )

    def save_geometry(self, *, timeout: float = 5.0) -> Completion:
        """Persist a completed calibration in the controller's EEPROM."""
        reply = self.request("SAVE", timeout=timeout)
        self._expect_plain_done(reply, "SAVE")
        return reply

    def set_map(self, occupancy_hex: str, *, timeout: float = 5.0) -> Completion:
        """Set the reconciled 96-position occupancy bitmap on the controller.

        The argument is exactly 24 uppercase hexadecimal characters, as
        returned by ``wire.encode_map``. This command does not sense pieces;
        only call it after confirming the physical board and parking slots.
        """
        if type(occupancy_hex) is not str or _HEX_MAP.fullmatch(occupancy_hex) is None:
            raise ValueError("occupancy_hex must be exactly 24 uppercase hex characters")
        reply = self.request("MAP", occupancy_hex, timeout=timeout)
        self._expect_plain_done(reply, "MAP")
        return reply

    def carry(
        self,
        source_id: int | str,
        destination_id: int | str,
        point_chars: str,
        *,
        timeout: float | None = None,
    ) -> Completion:
        """Execute one compact route; completion does not prove piece delivery.

        ``wire.encode_carry`` supplies these three arguments. Coordinates are
        base-17 x/y pairs (0..G), including source and destination, with 2..12
        waypoints. The firmware validates endpoints, occupancy and geometry
        again before it energizes the magnet.
        """
        source = self._location_id_argument(source_id, "source_id")
        destination = self._location_id_argument(destination_id, "destination_id")
        if source == destination:
            raise ValueError("source and destination must differ")
        self._validate_point_chars(point_chars)
        reply = self.request("CARRY", source, destination, point_chars, timeout=timeout)
        self._expect_plain_done(reply, "CARRY")
        return reply

    def carry_offset(
        self,
        source_id: int | str,
        corner: str,
        destination_id: int | str,
        point_chars: str,
        *,
        timeout: float | None = None,
    ) -> Completion:
        """Carry a rook directly from its confirmed in-square offset corner."""
        source = self._location_id_argument(source_id, "source_id")
        destination = self._location_id_argument(destination_id, "destination_id")
        if int(source) >= 64:
            raise ValueError("source_id must identify a board square in 0..63")
        if source == destination:
            raise ValueError("source and destination must differ")
        self._validate_corner(corner)
        self._validate_point_chars(point_chars)
        reply = self.request(
            "CARRY_OFFSET", source, corner, destination, point_chars, timeout=timeout,
        )
        self._expect_plain_done(reply, "CARRY_OFFSET")
        return reply

    def offset(
        self,
        square_id: int | str,
        corner: str,
        direction: str,
        *,
        timeout: float | None = None,
    ) -> Completion:
        """Nudge an occupied board square toward a corner or back to center.

        The occupancy bitmap is unchanged. As with ``CARRY``, a matching
        controller completion proves command completion, not piece placement.
        """
        square = self._location_id_argument(square_id, "square_id")
        if int(square) >= 64:
            raise ValueError("square_id must identify a board square in 0..63")
        self._validate_corner(corner)
        if type(direction) is not str or direction not in {"OUT", "IN"}:
            raise ValueError("direction must be OUT or IN")
        reply = self.request("OFFSET", square, corner, direction, timeout=timeout)
        self._expect_plain_done(reply, "OFFSET")
        return reply

    def stop(self, *, timeout: float = 5.0) -> Completion:
        """Preempt a pending HOME/REF (or any later motion command)."""
        reply = self.request("STOP", timeout=timeout)
        self._expect_plain_done(reply, "STOP")
        return reply

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if close is not None:
            close()

    def _handle_line(self, line: str) -> LinkEvent | Completion | None:
        if not line.startswith(("@", "!")):
            # Temporary one-letter diagnostic mode prints human-readable lines.
            return None
        if line.startswith("!"):
            return self._handle_event_frame(line)
        return self._handle_response_frame(line)

    def _handle_event_frame(self, line: str) -> LinkEvent:
        """Record an unsolicited event; BOOT and FAULT retire pending commands."""
        match = _EVENT.fullmatch(line)
        if match is None:
            return self._protocol_failure("malformed unsolicited frame")
        event = LinkEvent(match.group(1), tuple((match.group(2) or "").split()))
        with self._state_lock:
            self._events.append(event)
            if event.name == "BOOT":
                interrupted_request = bool(self._pending)
                self.boot_count += 1
                self.physical_state_uncertain = True
                self.capabilities = None
                self._fail_all_locked(ControllerReset("controller booted; reconcile physical state"))
                # Start a fresh ID epoch only when idle. If a request was
                # already sent, its reply may still be buffered after the
                # BOOT frame. Keep advancing IDs so that late reply cannot
                # be mistaken for a retried command.
                if not interrupted_request:
                    self._next_id = 1
                    self._abandoned_ids.clear()
            elif event.name == "FAULT":
                self.physical_state_uncertain = True
                self._fail_all_locked(ControllerFault("controller reported a fault"))
        return event

    def _handle_response_frame(self, line: str) -> Completion | None:
        """Match ACK, DONE, or ERR to the pending command ID."""
        match = _REPLY.fullmatch(line)
        if match is None:
            return self._protocol_failure("malformed response frame")
        command_id = int(match.group(1))
        if command_id > MAX_COMMAND_ID:
            return self._protocol_failure("response ID exceeds 65535")
        kind = match.group(2)
        rest = match.group(3)
        with self._state_lock:
            pending = self._pending.get(command_id)
            if pending is None:
                if command_id in self._abandoned_ids:
                    return None
                return self._protocol_failure("duplicate or unknown response ID")
            if kind == "ACK":
                if rest is not None or pending.acknowledged:
                    return self._protocol_failure("malformed or duplicate ACK")
                pending.acknowledged = True
                return None
            if kind == "ERR":
                if rest is None or not re.fullmatch(r"[A-Z][A-Z0-9_]*", rest):
                    return self._protocol_failure("malformed ERR code")
                if not self._safe_rejection(pending.verb, rest):
                    self.physical_state_uncertain = True
                error = ControllerError(
                    command_id, rest, requires_reconciliation=self.physical_state_uncertain
                )
                self._retire_locked(pending, error)
                return None
            if not pending.acknowledged:
                return self._protocol_failure("DONE arrived before ACK")
            payload = tuple(rest.split()) if rest else ()
            completion = Completion(command_id, pending.verb, payload)
            pending.completion = completion
            if pending.verb == "STOP":
                self.physical_state_uncertain = True
            self._retire_locked(pending)
            return completion

    def _fields(self, reply: Completion, verb: str, required: set[str]) -> dict[str, str]:
        if not reply.payload or reply.payload[0] != verb:
            return self._protocol_failure(f"{verb} completion has wrong payload")
        result: dict[str, str] = {}
        for token in reply.payload[1:]:
            if "=" not in token:
                return self._protocol_failure(f"malformed {verb} field")
            key, value = token.split("=", 1)
            if key in result or key not in required or not value:
                return self._protocol_failure(f"unexpected {verb} field")
            result[key] = value
        if result.keys() != required:
            return self._protocol_failure(f"incomplete {verb} payload")
        return result

    def _expect_plain_done(self, reply: Completion, verb: str) -> None:
        if reply.payload != (verb,):
            self._protocol_failure(f"{verb} completion has wrong payload")

    @staticmethod
    def _location_id_argument(value: int | str, label: str) -> str:
        if type(value) is int and 0 <= value < 96:
            return str(value)
        if type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]?)", value) and int(value) < 96:
            return value
        raise ValueError(f"{label} must be a canonical decimal position ID in 0..95")

    @staticmethod
    def _validate_corner(corner: str) -> None:
        if type(corner) is not str or corner not in _CORNERS:
            raise ValueError("corner must be NW, NE, SW, or SE")

    @staticmethod
    def _validate_point_chars(point_chars: str) -> None:
        if (
            type(point_chars) is not str
            or len(point_chars) < 4
            or len(point_chars) > 2 * MAX_CARRY_WAYPOINTS
            or len(point_chars) % 2 != 0
            or _BASE17_POINTS.fullmatch(point_chars) is None
        ):
            raise ValueError("point_chars must encode 2..12 uppercase base-17 x/y pairs")

    @staticmethod
    def _safe_rejection(verb: str, code: str) -> bool:
        if code in _SAFE_REJECTIONS:
            return True
        if verb in {"CARRY", "CARRY_OFFSET", "OFFSET"} and code in _MOTION_PREFLIGHT_REJECTIONS:
            return True
        if (verb == "GEOM" or verb == "SAVE") and code == "REFERENCE":
            return True
        if verb == "REF" and code == "NO_GEOMETRY":
            return True
        # CARRY GEOMETRY may also come from a current-position preflight fault,
        # which cancels motion and invalidates the reference. The firmware's
        # MAP handler currently emits only BUSY or SYNTAX for rejections.
        # GEOMETRY during HOME/REF happens after calibration has moved both
        # axes. DUPLICATE and any unknown codes leave the session ambiguous.
        return False

    def _retire(self, pending: PendingCommand, error: LinkError, *, abandoned: bool = False) -> bool:
        with self._state_lock:
            if not pending.finished:
                self.physical_state_uncertain = True
                self._retire_locked(pending, error, abandoned=abandoned)
                return True
            return False

    def _retire_locked(
        self, pending: PendingCommand, error: LinkError | None = None, *, abandoned: bool = False
    ) -> None:
        if error is not None:
            pending.error = error
        self._pending.pop(pending.command_id, None)
        if self._normal_id == pending.command_id:
            self._normal_id = None
        if self._stop_id == pending.command_id:
            self._stop_id = None
        if abandoned:
            self._abandoned_ids.add(pending.command_id)

    def _fail_all(self, error: LinkError) -> None:
        with self._state_lock:
            self._fail_all_locked(error)

    def _fail_all_locked(self, error: LinkError) -> None:
        self.physical_state_uncertain = True
        for pending in tuple(self._pending.values()):
            self._retire_locked(pending, error, abandoned=True)

    def _protocol_failure(self, message: str, cause: Exception | None = None):
        error = ProtocolError(message)
        self._fail_all(error)
        if cause is not None:
            raise error from cause
        raise error


def open_serial(port: str, **link_options: object) -> SerialLink:
    """Open a pyserial port lazily, with 115200 baud and bounded reads.

    Pyserial is optional for simulator use. Install it only for a physical
    board connection. Opening an Uno's USB serial port may reset the board;
    ``hello()`` retries once if ``!BOOT`` interrupts the first handshake. A
    caller may instead call ``poll()`` until the startup BOOT event arrives,
    then call ``hello()``. In either case, a boot leaves the physical state
    uncertain until the caller reconciles it.
    """
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("install pyserial to use a physical serial port") from exc
    stream = serial.Serial(port=port, baudrate=BAUD_RATE, timeout=0.05, write_timeout=2.0)
    return SerialLink(stream, **link_options)

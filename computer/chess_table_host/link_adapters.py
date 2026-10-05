"""Simulation and durable serial logging adapters for the host."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

from .game_errors import GameStateError
from .serial_link import Completion


class SimulatedLink:
    """Completes host commands without controlling physical hardware."""

    def __init__(self) -> None:
        self.command_id = 0
        self.bitmap: int | None = None

    def set_map(self, occupancy_hex: str) -> None:
        bitmap = int.from_bytes(bytes.fromhex(occupancy_hex), "little")
        if self.bitmap is not None and bitmap != self.bitmap:
            raise GameStateError("simulated controller map differs from confirmed host state")
        self.bitmap = bitmap
        print(f"SIM MAP {occupancy_hex}")

    def carry(self, source_id: str, destination_id: str, point_chars: str):
        source, destination = int(source_id), int(destination_id)
        if (self.bitmap is None or not 0 <= source < 96 or not 0 <= destination < 96 or
                not self.bitmap & (1 << source) or self.bitmap & (1 << destination)):
            raise GameStateError("simulated controller rejected inconsistent CARRY occupancy")
        self.bitmap = (self.bitmap & ~(1 << source)) | (1 << destination)
        self.command_id += 1
        print(f"SIM CARRY {source_id} {destination_id} {point_chars}")
        return Completion(self.command_id, "CARRY", ())

    def close(self) -> None:
        pass


class LoggedSerialStream:
    """Record complete TX/RX lines from one physical serial session."""

    def __init__(self, stream, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = stream
        self.log = path.open("a", encoding="utf-8", newline="\n")
        self.lock = threading.Lock()
        self.receive = bytearray()

    def _record(self, direction: str, raw: bytes) -> None:
        item = {
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "line": raw.rstrip(b"\r\n").decode("ascii", errors="replace"),
        }
        with self.lock:
            self.log.write(json.dumps(item, sort_keys=True) + "\n")
            self.log.flush()
            os.fsync(self.log.fileno())

    def read(self, size: int) -> bytes:
        data = self.stream.read(size)
        for byte in data:
            self.receive.append(byte)
            if byte == 10:
                self._record("rx", bytes(self.receive))
                self.receive.clear()
        return data

    def write(self, data: bytes) -> int | None:
        written = self.stream.write(data)
        if written is None or written == len(data):
            self._record("tx", data)
        return written

    def close(self) -> None:
        try:
            if self.receive:
                self._record("rx_partial", bytes(self.receive))
            self.log.close()
        finally:
            self.stream.close()

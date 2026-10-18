"""Durable PocketMage reset intents, independent of the next game's identity."""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

import chess

from .game_errors import GameStateError


RESET_TERMINAL = frozenset({"committed", "aborted", "reconciliation_required"})


@dataclass
class RemoteResetReceipt:
    request_id: str
    host_game_id: str
    source_game_id: str
    expected_ply: int
    expected_fen: str
    confirmed: bool = True
    state: str = "prepared"
    resulting_host_game_id: str | None = None

    def to_json(self) -> dict[str, object]:
        return dict(vars(self))

    @classmethod
    def from_json(cls, value: object) -> RemoteResetReceipt:
        try:
            if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
                raise ValueError("invalid fields")
            receipt = cls(**value)
            receipt.validate()
            return receipt
        except (TypeError, ValueError) as exc:
            raise GameStateError("invalid remote reset receipt") from exc

    def validate(self) -> None:
        try:
            valid = (
                isinstance(self.request_id, str) and len(self.request_id) <= 128 and
                re.fullmatch(r"[A-Za-z0-9._:-]+", self.request_id) is not None and
                isinstance(self.source_game_id, str) and 0 < len(self.source_game_id) <= 256 and
                not any(ord(char) < 32 for char in self.source_game_id) and
                isinstance(self.host_game_id, str) and
                str(UUID(self.host_game_id)) == self.host_game_id and
                type(self.expected_ply) is int and self.expected_ply >= 0 and
                isinstance(self.expected_fen, str) and 0 < len(self.expected_fen) <= 256 and
                not any(ord(char) < 32 for char in self.expected_fen) and
                chess.Board(self.expected_fen).is_valid() and self.confirmed is True and
                isinstance(self.state, str) and
                self.state in {"prepared", "in_flight", *RESET_TERMINAL}
            )
            if self.state == "committed":
                valid = valid and isinstance(self.resulting_host_game_id, str) and (
                    str(UUID(self.resulting_host_game_id)) == self.resulting_host_game_id and
                    self.resulting_host_game_id != self.host_game_id
                )
            else:
                valid = valid and self.resulting_host_game_id is None
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise GameStateError("invalid remote reset receipt")

    def same_intent(self, other: RemoteResetReceipt) -> bool:
        return all(getattr(self, key) == getattr(other, key) for key in (
            "request_id", "host_game_id", "source_game_id", "expected_ply", "expected_fen", "confirmed",
        ))


def settle_reset_receipts(state) -> bool:
    """Terminate an interrupted request; a duplicate can only observe it.

    Local recovery may later continue a saved physical reset, but a network
    retry or process restart must never authorize that continuation.
    """
    changed = False
    active = state.reset or state.pending
    uncertain = active is not None and active.phase != "ready"
    for receipt in state.remote_reset_receipts.values():
        if receipt.state not in RESET_TERMINAL:
            receipt.state = "reconciliation_required" if uncertain else "aborted"
            changed = True
    return changed

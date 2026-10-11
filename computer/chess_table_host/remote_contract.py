"""Typed version-one remote requests, validated before session state changes."""

from dataclasses import dataclass
from http import HTTPStatus
import re
from typing import Any

from .api_errors import ApiError
from .checkpoint import RemoteMoveReceipt


def request_identifier(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128 or
            re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None):
        raise ApiError(f"{label} must be a bounded identifier", HTTPStatus.BAD_REQUEST)
    return value


def bounded_text(value: object, label: str, limit: int = 256) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit or
            any(ord(char) < 32 for char in value)):
        raise ApiError(f"{label} must be a bounded string", HTTPStatus.BAD_REQUEST)
    return value


def _body(value: object, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ApiError("invalid request fields", HTTPStatus.BAD_REQUEST)
    return value


@dataclass(frozen=True)
class SessionStartRequest:
    host_game_id: str
    request_id: str
    source_game_id: str
    mode: str = "external"
    human: str = "white"

    @classmethod
    def from_body(cls, value: object) -> "SessionStartRequest":
        body = _body(value, {"host_game_id", "request_id", "source_kind",
                             "source_game_id", "mode", "human"})
        mode, human = body.get("mode", "external"), body.get("human", "white")
        if (not isinstance(mode, str) or mode not in {"external", "human_engine"} or
                not isinstance(human, str) or human not in {"white", "black"}):
            raise ApiError("invalid game mode or human color", HTTPStatus.BAD_REQUEST)
        if body.get("source_kind") != "aina":
            raise ApiError("source_kind must be aina", HTTPStatus.BAD_REQUEST)
        return cls(request_identifier(body.get("host_game_id"), "host_game_id"),
                   request_identifier(body.get("request_id"), "request_id"),
                   bounded_text(body.get("source_game_id"), "source_game_id"), mode, human)


@dataclass(frozen=True)
class ResolveMoveRequest:
    request_id: str
    expression: str

    @classmethod
    def from_body(cls, value: object) -> "ResolveMoveRequest":
        body = _body(value, {"request_id", "expression"})
        return cls(request_identifier(body.get("request_id"), "request_id"),
                   bounded_text(body.get("expression"), "expression"))


@dataclass(frozen=True)
class MoveRequest:
    request_id: str
    host_game_id: str
    source_game_id: str
    expected_ply: int
    expected_fen: str
    uci: str

    @classmethod
    def from_body(cls, value: object) -> "MoveRequest":
        body = _body(value, {"request_id", "host_game_id", "source_game_id",
                             "expected_ply", "expected_fen", "uci"})
        ply, uci = body.get("expected_ply"), body.get("uci")
        if (type(ply) is not int or ply < 0 or not isinstance(uci, str) or
                re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", uci) is None):
            raise ApiError("invalid move request", HTTPStatus.BAD_REQUEST)
        return cls(request_identifier(body.get("request_id"), "request_id"),
                   request_identifier(body.get("host_game_id"), "host_game_id"),
                   bounded_text(body.get("source_game_id"), "source_game_id"), ply,
                   bounded_text(body.get("expected_fen"), "expected_fen"), uci)

    def receipt(self) -> RemoteMoveReceipt:
        return RemoteMoveReceipt(self.request_id, self.host_game_id, self.source_game_id,
                                 self.expected_ply, self.expected_fen, self.uci)

    def matches(self, receipt: RemoteMoveReceipt) -> bool:
        return all(getattr(self, field) == getattr(receipt, field)
                   for field in self.__dataclass_fields__)

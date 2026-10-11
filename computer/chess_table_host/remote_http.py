"""Authenticated loopback API for an external chess-table controller.

This server deliberately has a handler separate from the browser GUI. It
offers no reconciliation or raw serial controls. Stockfish is selected
through the typed session-start contract and remains owned by the host.
"""

from __future__ import annotations

import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from .game import GameStateError
from .api_errors import ApiError
from .planner import PlanError
from .routing import RouteError
from .serial_link import LinkError

if TYPE_CHECKING:
    from .gui import GameController


MAX_BODY_BYTES = 8192


def read_remote_token(path: Path) -> str:
    """Load a local bearer token without ever including its value in errors."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read remote token file") from exc
    if (len(token) < 32 or len(token) > 512 or
            not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ValueError("remote token file must contain 32–512 printable ASCII characters")
    return token


class RemoteHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: GameController, token: str):
        if address[0] != "127.0.0.1":
            raise ValueError("remote API must bind to 127.0.0.1")
        if (not isinstance(token, str) or len(token) < 32 or len(token) > 512 or
                not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError("remote API requires a token of at least 32 characters")
        self.controller = controller
        self.token = token
        super().__init__(address, RemoteHandler)


class RemoteHandler(BaseHTTPRequestHandler):
    server: RemoteHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        # Do not log arbitrary request paths or authorization material.
        pass

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _error(self, code: str, message: str, status: HTTPStatus) -> None:
        self._send_json({"error": {"code": code, "message": message}}, status)

    def _authorized(self) -> bool:
        host = self.headers.get("Host")
        if host not in {f"127.0.0.1:{self.server.server_port}",
                        f"localhost:{self.server.server_port}"}:
            self._error("forbidden", "local Host required", HTTPStatus.FORBIDDEN)
            return False
        if self.headers.get("Origin") is not None:
            self._error("forbidden", "browser origins are not accepted", HTTPStatus.FORBIDDEN)
            return False
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        try:
            authenticated = hmac.compare_digest(supplied, expected)
        except TypeError:
            authenticated = False
        if not authenticated:
            self._error("unauthorized", "bearer token required", HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _body(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise ApiError("send application/json", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise ApiError("valid Content-Length required", HTTPStatus.BAD_REQUEST) from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ApiError("request body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError("invalid JSON", HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(value, dict):
            raise ApiError("JSON body must be an object", HTTPStatus.BAD_REQUEST)
        return value

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            status = exc.status
        elif isinstance(exc, OSError):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif isinstance(exc, (GameStateError, RouteError, PlanError, LinkError)):
            status = HTTPStatus.CONFLICT
        else:
            raise exc
        code = {
            HTTPStatus.BAD_REQUEST: "bad_request",
            HTTPStatus.CONFLICT: "conflict",
            HTTPStatus.NOT_FOUND: "not_found",
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "body_too_large",
            HTTPStatus.UNPROCESSABLE_ENTITY: "invalid_move",
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE: "unsupported_media_type",
            HTTPStatus.SERVICE_UNAVAILABLE: "unavailable",
            HTTPStatus.GATEWAY_TIMEOUT: "preparation_timeout",
        }.get(status, "request_failed")
        # Filesystem and serial exceptions can include the private checkpoint
        # or device path. Keep those details on the host side.
        message = "host state unavailable" if isinstance(exc, OSError) else str(exc)
        self._error(code, message, status)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/v1/status":
                self._send_json(self.server.controller.remote_status())
            elif path.startswith("/v1/moves/") and path.count("/") == 3:
                request_id = unquote(path.removeprefix("/v1/moves/"))
                self._send_json(self.server.controller.remote_receipt(request_id))
            else:
                self._error("not_found", "route not found", HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path = urlsplit(self.path).path
        if path not in {"/v1/session/start", "/v1/session/reset", "/v1/moves",
                        "/v1/moves/resolve", "/v1/stop"}:
            self._error("not_found", "route not found", HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._body()
            controller = self.server.controller
            if path == "/v1/session/start":
                self._send_json(controller.remote_start(body), HTTPStatus.ACCEPTED)
            elif path == "/v1/session/reset":
                self._send_json(controller.remote_reset(body), HTTPStatus.ACCEPTED)
            elif path == "/v1/moves/resolve":
                self._send_json(controller.remote_resolve_move(body))
            elif path == "/v1/moves":
                receipt = controller.remote_submit_move(body)
                status = (HTTPStatus.OK if receipt["state"] in {"committed", "aborted"}
                          else HTTPStatus.ACCEPTED)
                self._send_json(receipt, status)
            else:
                if body:
                    raise ApiError("stop body must be empty", HTTPStatus.BAD_REQUEST)
                controller.stop()
                self._send_json(controller.remote_status(), HTTPStatus.ACCEPTED)
        except Exception as exc:
            self._handle_error(exc)

    def _unsupported_method(self) -> None:
        if self._authorized():
            self._error("method_not_allowed", "method not allowed",
                        HTTPStatus.METHOD_NOT_ALLOWED)

    do_HEAD = _unsupported_method
    do_OPTIONS = _unsupported_method
    do_PUT = _unsupported_method
    do_PATCH = _unsupported_method
    do_DELETE = _unsupported_method
    do_TRACE = _unsupported_method
    do_CONNECT = _unsupported_method

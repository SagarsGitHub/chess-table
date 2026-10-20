"""Loopback-only HTTP adapter for the GUI controller and static browser files."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from .api_errors import ApiError
from .game import GameStateError
from .chesscom import ChessComError
from .planner import PlanError
from .routing import RouteError
from .serial_link import LinkError

if TYPE_CHECKING:
    from .gui import GameController


STATIC_DIR = Path(__file__).with_name("gui_static")


class GuiHTTPServer(ThreadingHTTPServer):
    """Serve local requests while the game worker remains responsive."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: GameController):
        self.controller = controller
        super().__init__(address, GuiHandler)


class GuiHandler(BaseHTTPRequestHandler):
    """Expose the checkpoint and serialized controls to the local page."""

    server: GuiHTTPServer

    def _local_host(self) -> bool:
        """Reject mismatched Host headers on this loopback listener."""
        return self.headers.get("Host") in {
            f"localhost:{self.server.server_port}",
            f"127.0.0.1:{self.server.server_port}",
        }

    def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _static(self, name: str, content_type: str) -> None:
        """Read only files selected by the explicit GET route whitelist."""
        path = STATIC_DIR / name
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._local_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                self._send_json(self.server.controller.state())
            except (OSError, GameStateError) as exc:
                self._send_json({"error": f"checkpoint temporarily unavailable: {exc}"},
                                HTTPStatus.SERVICE_UNAVAILABLE)
        elif path == "/api/config":
            self._send_json(self.server.controller.config())
        elif path.startswith("/api/chesscom/"):
            try:
                query_lists = parse_qs(urlsplit(self.path).query, keep_blank_values=True, max_num_fields=8)
                if any(len(values) != 1 for values in query_lists.values()):
                    raise ApiError("duplicate query parameters", HTTPStatus.BAD_REQUEST)
                query = {key: values[0] for key, values in query_lists.items()}
                collection = path.removeprefix("/api/chesscom/")
                if collection == "selection":
                    if query:
                        raise ApiError("selection does not accept query parameters", HTTPStatus.BAD_REQUEST)
                    value = self.server.controller.chesscom_selection()
                else:
                    value = self.server.controller.chesscom_list(collection, query)
                self._send_json(value)
            except (ApiError, ChessComError) as exc:
                self._send_json({"error": str(exc)}, exc.status)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (OSError, GameStateError) as exc:
                self._send_json({"error": f"Chess.com data unavailable: {exc}"}, HTTPStatus.SERVICE_UNAVAILABLE)
        elif path in {"/", "/index.html"}:
            self._static("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
        elif path == "/style.css":
            self._static("style.css", "text/css; charset=utf-8")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._local_host():
            self._send_json({"error": "local Host required"}, HTTPStatus.FORBIDDEN)
            return
        path = urlsplit(self.path).path
        if path not in {"/api/start", "/api/move", "/api/stop", "/api/reconcile", "/api/reset-board", "/api/end-game-reset",
                        "/api/chesscom/select", "/api/chesscom/replay/start", "/api/chesscom/replay/next",
                        "/api/chesscom/replay/autoplay", "/api/chesscom/replay/pause",
                        "/api/chesscom/daily/switch", "/api/chesscom/daily/check"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        # JSON and same-origin requests prevent an unrelated website from
        # making blind form posts to this loopback-only motion controller.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self._send_json({"error": "cross-origin control is forbidden"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json({"error": "send application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 8192:
                raise ApiError("request body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ApiError("JSON body must be an object", HTTPStatus.BAD_REQUEST)
            controller = self.server.controller
            if path.startswith("/api/chesscom/"):
                action = {
                    "/api/chesscom/select": controller.chesscom_select,
                    "/api/chesscom/replay/start": controller.chesscom_replay_start,
                    "/api/chesscom/replay/next": controller.chesscom_replay_next,
                    "/api/chesscom/replay/autoplay": controller.chesscom_replay_autoplay,
                    "/api/chesscom/replay/pause": controller.chesscom_replay_pause,
                    "/api/chesscom/daily/switch": controller.chesscom_daily_switch,
                    "/api/chesscom/daily/check": controller.chesscom_daily_check,
                }[path]
                self._send_json(action(body), HTTPStatus.ACCEPTED)
                return
            if path == "/api/start":
                controller.start(body.get("mode"), body.get("human", "white"))
            elif path == "/api/move":
                controller.submit_move(body.get("uci"))
            elif path == "/api/stop":
                controller.stop()
            elif path == "/api/reconcile":
                controller.reconcile(body.get("outcome"), body.get("confirmed"))
            elif path == "/api/reset-board":
                controller.reset_board()
            elif path == "/api/end-game-reset":
                controller.end_game_and_reset()
            self._send_json(controller.state(), HTTPStatus.ACCEPTED)
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, HTTPStatus.BAD_REQUEST)
        except (ApiError, ChessComError) as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except OSError as exc:
            self._send_json({"error": f"checkpoint temporarily unavailable: {exc}"},
                            HTTPStatus.SERVICE_UNAVAILABLE)
        except (GameStateError, RouteError, PlanError, LinkError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)

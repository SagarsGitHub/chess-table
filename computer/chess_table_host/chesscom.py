"""Read-only Chess.com PubAPI access and validated standard-chess replays.

Only documented player endpoints are fetched. A game URL is an identifier to
display, never a URL for this client to download. PubAPI data can be delayed;
neither a FEN nor an active-game listing is evidence of physical board state.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import chess


API_BASE = "https://api.chess.com/pub"
DEFAULT_USERNAME = "ofroni"
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_PGN_BYTES = 256 * 1024
MAX_PLIES = 2000
_HTTP_LOCK = threading.Lock()
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,24}\Z")
_TAG = re.compile(r'\[([A-Za-z][A-Za-z0-9_]*)\s+"((?:[^"\\\r\n]|\\["\\])*)"\s*\]')
_TOKEN = re.compile(
    r"1/2-1/2|1-0|0-1|\*|\d+\.(?:\.\.)?|\$\d+|[()]|[!?]+|"
    r"(?:[O0]-[O0](?:-[O0])?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?)[+#]?"
)
_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}


class ChessComError(ValueError):
    """A useful integration error, with a status suitable for the local API."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _username(value: str) -> str:
    if not isinstance(value, str) or not _USERNAME.fullmatch(value):
        raise ChessComError("Chess.com username must be 3–25 letters, numbers, underscores or hyphens, starting with a letter or number")
    return value.lower()


def _period(year: int | str, month: int | str) -> tuple[int, int]:
    if isinstance(year, bool) or not re.fullmatch(r"[1-9][0-9]{3}", str(year)):
        raise ChessComError("Archive year must be a four-digit year")
    if isinstance(month, bool) or not re.fullmatch(r"(?:0?[1-9]|1[0-2])", str(month)):
        raise ChessComError("Archive month must be between 1 and 12")
    return int(year), int(month)


def _tokens(text: str):
    """Tokenize without python-chess's permissive skipping of unknown text."""
    offset = 0
    while offset < len(text):
        char = text[offset]
        if char.isspace():
            offset += 1
        elif char == ";":
            end = text.find("\n", offset)
            offset = len(text) if end < 0 else end + 1
        elif char == "{":
            end = text.find("}", offset + 1)
            if end < 0 or "{" in text[offset + 1:end]:
                raise ChessComError("PGN has an unclosed or nested comment")
            offset = end + 1
        else:
            match = _TOKEN.match(text, offset)
            if match is None:
                raise ChessComError(f"PGN contains unrecognized text near {text[offset:offset + 24]!r}")
            token = match.group()
            if (token not in _RESULTS and token[0] not in "$(!?)" and "." not in token
                    and match.end() < len(text) and not text[match.end()].isspace()
                    and text[match.end()] not in "{};()!?$"):
                raise ChessComError("PGN move tokens must be separated")
            yield token
            offset = match.end()


def parse_pgn(pgn: str, *, require_finished: bool = False, allow_empty: bool = False) -> dict:
    """Validate one PGN and return headers, UCI moves, full FENs and result.

    Only standard chess beginning from the ordinary starting position is
    supported. Comments, annotation glyphs and legal recursive variations are
    accepted, but only the main line is returned. A result marker must terminate
    the movetext; a second game or trailing garbage is rejected. ``allow_empty``
    permits an active ``*`` PGN before its first move, for Daily game syncing.
    """
    if not isinstance(pgn, str) or not pgn.strip():
        raise ChessComError("Chess.com did not provide PGN move history; this game is available for preview only")
    try:
        size = len(pgn.encode("utf-8"))
    except UnicodeError as exc:
        raise ChessComError("PGN contains invalid Unicode text") from exc
    if size > MAX_PGN_BYTES:
        raise ChessComError("PGN exceeds the supported size")
    text = pgn.lstrip("\ufeff \r\n\t")
    headers = {}
    offset = 0
    while offset < len(text) and text[offset] == "[":
        match = _TAG.match(text, offset)
        if match is None:
            raise ChessComError("PGN has a malformed header")
        key, value = match.groups()
        if key in headers:
            raise ChessComError(f"PGN repeats the {key} header")
        headers[key] = re.sub(r'\\(["\\])', r'\1', value)
        offset = match.end()
        while offset < len(text) and text[offset].isspace():
            offset += 1
    if headers.get("Variant", "Standard").lower() not in {"standard", "chess"}:
        raise ChessComError("Only standard chess PGNs can be replayed")
    if headers.get("SetUp", "0") not in {"0", "1"}:
        raise ChessComError("PGN has an invalid SetUp header")
    if headers.get("SetUp") == "1" and "FEN" not in headers:
        raise ChessComError("PGN SetUp requires a starting FEN")
    try:
        initial = chess.Board(headers.get("FEN", chess.STARTING_FEN))
    except ValueError as exc:
        raise ChessComError("PGN has an invalid starting FEN") from exc
    if initial.fen() != chess.STARTING_FEN:
        raise ChessComError("Only games from the standard starting position can be replayed")

    board = initial
    moves = []
    positions = [board.fen()]
    # Each nested variation branches before its immediately preceding move.
    variations = []
    before_move = None
    result = None
    token_count = 0
    for token in _tokens(text[offset:]):
        token_count += 1
        if token_count > MAX_PLIES * 12:
            raise ChessComError("PGN contains too many tokens")
        if result is not None:
            raise ChessComError("PGN contains a second game or text after its result")
        if token in _RESULTS:
            if variations:
                raise ChessComError("PGN result marker is inside a variation")
            result = token
        elif token == "(":
            if before_move is None or len(variations) >= 16:
                raise ChessComError("PGN has an invalid or excessively nested variation")
            variations.append((board, before_move))
            board = before_move.copy(stack=False)
            before_move = None
        elif token == ")":
            if not variations or before_move is None:
                raise ChessComError("PGN has an unmatched or empty variation")
            board, before_move = variations.pop()
        elif token.startswith("$") or token[0] in "!?":
            if before_move is None:
                raise ChessComError("PGN annotation has no preceding move")
        elif token[0].isdigit() and "." in token:
            number, dots = token.split(".", 1)
            if int(number) != board.fullmove_number or (len(dots) == 2) != (board.turn == chess.BLACK):
                raise ChessComError("PGN move number does not match its position")
        else:
            try:
                move = board.parse_san(token)
            except ValueError as exc:
                raise ChessComError(f"PGN contains an illegal or ambiguous move: {token}") from exc
            if not move or not board.is_legal(move):
                raise ChessComError(f"PGN contains an illegal move: {token}")
            before_move = board.copy(stack=False)
            board.push(move)
            if not variations:
                moves.append(move.uci())
                positions.append(board.fen())
                if len(moves) > MAX_PLIES:
                    raise ChessComError("PGN contains too many moves")
    if variations:
        raise ChessComError("PGN has an unclosed variation")
    if result is None:
        raise ChessComError("PGN is missing its final result marker")
    if "Result" in headers and headers["Result"] != result:
        raise ChessComError("PGN header and movetext results disagree")
    if require_finished and result == "*":
        raise ChessComError("A finished game must have a finished PGN result")
    if not moves and (not allow_empty or require_finished or result != "*"):
        raise ChessComError("PGN contains no replayable moves")
    return {"headers": headers, "moves": moves, "positions": positions, "result": result}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class _Cached:
    data: dict
    expires: float
    fetched_at: str
    headers: dict


def _header_dict(headers) -> dict:
    return {key.lower(): value for key, value in headers.items()}


def _ttl(headers: dict, default: int) -> float:
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)", headers.get("cache-control", ""), re.I)
    age = headers.get("age", "0")
    remaining = int(match.group(1)) - (int(age) if age.isdigit() else 0) if match else default
    # Even max-age=0 responses have a short local polling floor.
    return max(30, remaining)


def _game_url(value) -> str:
    if not isinstance(value, str):
        raise ChessComError("Chess.com returned a game without a URL", 502)
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ChessComError("Chess.com returned an invalid game URL", 502) from exc
    if (parts.scheme != "https" or parts.netloc.lower() not in {"www.chess.com", "chess.com"}
            or parts.query or parts.fragment
            or not re.fullmatch(r"/(?:game/(?:daily|live)|(?:daily|live)/game)/[0-9]+/?", parts.path)):
        raise ChessComError("Chess.com returned an unsupported game URL", 502)
    return value


def _player(value) -> str:
    if isinstance(value, dict):
        value = value.get("username") or value.get("@id")
    if isinstance(value, str):
        name = value.rstrip("/").rsplit("/", 1)[-1]
        if _USERNAME.fullmatch(name):
            return name
    return "Unknown"


def _normalize_game(raw: dict, *, finished: bool) -> dict:
    if not isinstance(raw, dict):
        raise ChessComError("Chess.com returned an invalid game record", 502)
    url = _game_url(raw.get("url"))
    pgn = raw.get("pgn") if isinstance(raw.get("pgn"), str) else ""
    raw_fen = raw.get("fen")
    valid_fen_text = isinstance(raw_fen, str) and len(raw_fen) <= 512
    white, black = _player(raw.get("white")), _player(raw.get("black"))
    record = {
        "id": url, "url": url, "title": f"{white} vs {black}",
        "white": white, "black": black, "fen": raw_fen if valid_fen_text else "", "pgn": pgn,
        "moves": [], "positions": [], "replay_supported": False, "unsupported_reason": "",
        "turn": raw.get("turn"), "move_by": raw.get("move_by"),
        "result": "*", "rules": raw.get("rules", "chess"),
        "time_class": raw.get("time_class", ""), "time_control": raw.get("time_control", ""),
        "end_time": raw.get("end_time"), "last_activity": raw.get("last_activity"),
    }
    try:
        if raw_fen is not None and not valid_fen_text:
            raise ChessComError("Chess.com returned an invalid position FEN")
        if record["rules"] != "chess":
            raise ChessComError("Only standard chess games can be replayed")
        parsed = parse_pgn(pgn, require_finished=finished, allow_empty=not finished)
        if record["fen"]:
            try:
                preview = chess.Board(record["fen"])
            except (ValueError, TypeError) as exc:
                raise ChessComError("Chess.com returned an invalid position FEN") from exc
            if not preview.is_valid() or preview.fen() != parsed["positions"][-1]:
                raise ChessComError("Chess.com position and PGN move history disagree; refresh before replay")
        else:
            record["fen"] = parsed["positions"][-1]
        record.update({key: parsed[key] for key in ("moves", "positions", "result")})
        record["replay_supported"] = True
    except ChessComError as exc:
        record["unsupported_reason"] = str(exc)
    return record


class ChessComClient:
    """Small serial, bounded, cached client for public player game data."""

    def __init__(self, *, timeout: float = 10.0, opener=None, clock=None):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError("Chess.com timeout must be greater than zero and at most 30 seconds")
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirect())
        self._clock = clock or time.monotonic
        self._cache = {}
        self._retry_at = 0.0

    def _get(self, path: str, *, default_ttl: int = 60, refresh: bool = False) -> tuple[dict, dict]:
        # All callers construct paths from validated components, never response URLs.
        url = API_BASE + path
        with _HTTP_LOCK:
            now = self._clock()
            cached = self._cache.get(path)
            if cached and cached.expires > now and not refresh:
                return copy.deepcopy(cached.data), {"cached": True, "fetched_at": cached.fetched_at,
                                                    "cache_seconds": max(0, int(cached.expires - now))}
            if self._retry_at > now:
                raise ChessComError(f"Chess.com rate limit: try again in {max(1, int(self._retry_at - now))} seconds", 429)
            headers = {"Accept": "application/json", "Accept-Encoding": "identity",
                       "User-Agent": "chess-table/1.0 (Chess.com PubAPI integration)"}
            if cached:
                if cached.headers.get("etag"):
                    headers["If-None-Match"] = cached.headers["etag"]
                if cached.headers.get("last-modified"):
                    headers["If-Modified-Since"] = cached.headers["last-modified"]
            try:
                with self._opener.open(Request(url, headers=headers), timeout=self.timeout) as response:
                    status = response.status
                    response_headers = _header_dict(response.headers)
                    if status != 200:
                        raise ChessComError(f"Chess.com returned HTTP {status}", 502)
                    content_length = response_headers.get("content-length", "")
                    if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
                        raise ChessComError("Chess.com response exceeds the supported size", 502)
                    body = response.read(MAX_BODY_BYTES + 1)
                    if len(body) > MAX_BODY_BYTES:
                        raise ChessComError("Chess.com response exceeds the supported size", 502)
            except HTTPError as exc:
                response_headers = _header_dict(exc.headers or {})
                exc.close()
                if exc.code == 304 and cached:
                    merged = {**cached.headers, **response_headers}
                    cached.expires = now + _ttl(merged, default_ttl)
                    cached.headers = merged
                    return copy.deepcopy(cached.data), {"cached": True, "fetched_at": cached.fetched_at,
                                                        "cache_seconds": max(0, int(cached.expires - now))}
                if exc.code == 429:
                    retry = response_headers.get("retry-after", "60")
                    try:
                        wait = int(retry)
                    except ValueError:
                        try:
                            wait = int((parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            wait = 60
                    wait = min(86400, max(30, wait))
                    self._retry_at = now + wait
                    raise ChessComError(f"Chess.com rate limit: try again in {wait} seconds", 429) from exc
                if exc.code in {404, 410}:
                    raise ChessComError("Chess.com player or archive was not found", 404) from exc
                if 300 <= exc.code < 400:
                    raise ChessComError("Chess.com redirected the request; redirects are disabled", 502) from exc
                raise ChessComError(f"Chess.com returned HTTP {exc.code}; try again later", 502) from exc
            except (URLError, TimeoutError, socket.timeout, OSError) as exc:
                raise ChessComError("Could not reach Chess.com; check the connection and try again", 502) from exc
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise ChessComError("Chess.com returned invalid JSON", 502) from exc
            if not isinstance(data, dict):
                raise ChessComError("Chess.com returned an invalid JSON object", 502)
            fetched_at = datetime.now(timezone.utc).isoformat()
            ttl = _ttl(response_headers, default_ttl)
            if "no-store" not in response_headers.get("cache-control", "").lower():
                self._cache[path] = _Cached(copy.deepcopy(data), now + ttl, fetched_at, response_headers)
                # Bound memory even when many players/months are browsed.
                if len(self._cache) > 32:
                    del self._cache[next(iter(self._cache))]
            else:
                self._cache.pop(path, None)
                ttl = 0
            return data, {"cached": False, "fetched_at": fetched_at, "cache_seconds": int(ttl)}

    def daily(self, username: str = DEFAULT_USERNAME, *, refresh: bool = False) -> dict:
        username = _username(username)
        if type(refresh) is not bool:
            raise ChessComError("Daily refresh must be true or false")
        data, metadata = self._get(f"/player/{username}/games", refresh=refresh)
        if not isinstance(data.get("games"), list):
            raise ChessComError("Chess.com returned an invalid games list", 502)
        return {"source": "chess.com", "kind": "daily", "username": username,
                "games": [_normalize_game(game, finished=False) for game in data["games"]], **metadata}

    def archives(self, username: str = DEFAULT_USERNAME) -> dict:
        username = _username(username)
        data, metadata = self._get(f"/player/{username}/games/archives", default_ttl=3600)
        if not isinstance(data.get("archives"), list):
            raise ChessComError("Chess.com returned an invalid archive list", 502)
        months = []
        pattern = re.compile(re.escape(f"{API_BASE}/player/{username}/games/") + r"([1-9][0-9]{3})/(0[1-9]|1[0-2])\Z", re.I)
        for url in data["archives"]:
            match = pattern.fullmatch(url) if isinstance(url, str) else None
            if match is None:
                raise ChessComError("Chess.com returned an unexpected archive URL", 502)
            year, month = map(int, match.groups())
            months.append({"year": year, "month": month, "label": f"{year:04d}-{month:02d}"})
        return {"source": "chess.com", "kind": "archives", "username": username,
                "archives": months, **metadata}

    def finished(self, username: str, year: int | str, month: int | str, query: str = "") -> dict:
        username = _username(username)
        year, month = _period(year, month)
        if not isinstance(query, str) or len(query) > 100:
            raise ChessComError("Game search must be text of at most 100 characters")
        query = query.strip()
        data, metadata = self._get(f"/player/{username}/games/{year:04d}/{month:02d}", default_ttl=3600)
        if not isinstance(data.get("games"), list):
            raise ChessComError("Chess.com returned an invalid games list", 502)
        games = [_normalize_game(game, finished=True) for game in data["games"]]
        filtered = [game for game in games if query.casefold() in game["title"].casefold()]
        return {"source": "chess.com", "kind": "finished", "username": username,
                "year": year, "month": month, "query": query, "games": filtered,
                "total_games": len(games), **metadata}

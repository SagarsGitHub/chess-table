"""Offline contract and defensive parsing checks for the PubAPI foundation."""

from email.message import Message
from io import BytesIO
import json
import threading
import time
import unittest
from urllib.error import HTTPError, URLError

import chess

from chess_table_host.chesscom import (
    API_BASE, MAX_BODY_BYTES, ChessComClient, ChessComError, _NoRedirect, parse_pgn,
)


FINISHED = '[White "ofroni"]\n[Black "Opponent"]\n[Result "1-0"]\n\n1. e4 e5 2. Nf3 Nc6 1-0'
DAILY = FINISHED.replace('"1-0"', '"*"').replace("Nc6 1-0", "Nc6 *")
GAME_URL = "https://www.chess.com/game/daily/123456"


def game(*, pgn=FINISHED, **extra):
    record = {"url": GAME_URL, "pgn": pgn, "rules": "chess",
              "white": {"username": "ofroni", "result": "win"},
              "black": {"username": "Opponent", "result": "resigned"}}
    record.update(extra)
    return record


class Response(BytesIO):
    status = 200

    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode() if not isinstance(data, bytes) else data)
        self.headers = headers or {}


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def http_error(code, **headers):
    message = Message()
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    return HTTPError(API_BASE, code, "test", message, BytesIO(b""))


class PgnTests(unittest.TestCase):
    def test_replay_is_legal_full_fen_history(self):
        parsed = parse_pgn(FINISHED, require_finished=True)
        self.assertEqual(parsed["moves"], ["e2e4", "e7e5", "g1f3", "b8c6"])
        self.assertEqual(len(parsed["positions"]), 5)
        self.assertEqual(parsed["positions"][0], chess.STARTING_FEN)
        board = chess.Board()
        for index, move in enumerate(parsed["moves"], 1):
            board.push_uci(move)
            self.assertEqual(parsed["positions"][index], board.fen())
        self.assertEqual(parsed["result"], "1-0")

    def test_comments_nags_and_legal_variations_do_not_change_mainline(self):
        pgn = '1.e4 {clock [%clk 0:01]} $1 (1.d4 d5 (1...Nf6)) e5!? ; hello\n2.Nf3 Nc6 1-0'
        self.assertEqual(parse_pgn(pgn)["moves"], parse_pgn(FINISHED)["moves"])

    def test_castling_en_passant_and_promotions(self):
        castle = parse_pgn("1.e4 e5 2.Nf3 Nc6 3.Bc4 Nf6 4.O-O Be7 *")
        self.assertIn("e1g1", castle["moves"])
        en_passant = parse_pgn("1.e4 a6 2.e5 d5 3.exd6 *")
        self.assertEqual(chess.Board(en_passant["positions"][-1]).piece_at(chess.D5), None)
        promotion = parse_pgn("1.a4 h5 2.a5 h4 3.a6 h3 4.axb7 hxg2 5.bxa8=Q gxh1=Q *")
        self.assertEqual(promotion["moves"][-2:], ["b7a8q", "g2h1q"])

    def test_finished_result_is_required_for_archives(self):
        self.assertEqual(parse_pgn(DAILY)["result"], "*")
        with self.assertRaisesRegex(ChessComError, "finished"):
            parse_pgn(DAILY, require_finished=True)

    def test_rejects_malformed_illegal_incomplete_and_second_games(self):
        malformed = ["", "1.e4 garbage *", "1.e5 *", "1.e4 e5 2.Ke3 *", "1.e4 e5",
                     "1.e4e5 *", "1.e4 -- *", "1.e4 {comment *", "1.e4 (1.d4 *",
                     "1.e4 ) *", "1.e4 () *", "1.e4 (1.e5) *", "2.e4 *", "1...e4 *",
                     "1.e4 1-0 1.d4 0-1", "1.e4 1-0\n\n[Event \"second\"]\n1.d4 0-1",
                     "1.e4 * trailing", '[Result "0-1"]\n1.e4 1-0', '[Result "*"]\n[Result "*"]\n1.e4 *',
                     '[SetUp "1"]\n1.e4 *', '[White "broken]\n1.e4 *']
        for pgn in malformed:
            with self.subTest(pgn=pgn), self.assertRaises(ChessComError):
                parse_pgn(pgn)

    def test_rejects_variants_and_nonstandard_starts(self):
        for prefix in ['[Variant "Chess960"]', '[FEN "8/8/8/8/8/8/4K3/7k w - - 0 1"]']:
            with self.subTest(prefix=prefix), self.assertRaises(ChessComError):
                parse_pgn(prefix + "\n1.e4 *")


class ClientTests(unittest.TestCase):
    def test_daily_default_and_documented_player_url_shapes(self):
        raw = game(pgn=DAILY, white=API_BASE + "/player/ofroni", black=API_BASE + "/player/opponent",
                   turn="white", move_by=1234, fen=parse_pgn(DAILY)["positions"][-1])
        opener = Opener(Response({"games": [raw]}))
        result = ChessComClient(opener=opener).daily()
        self.assertEqual(opener.requests[0][0].full_url, API_BASE + "/player/ofroni/games")
        self.assertEqual(opener.requests[0][1], 10.0)
        self.assertEqual(result["games"][0]["id"], GAME_URL)
        self.assertEqual(result["games"][0]["white"], "ofroni")
        self.assertTrue(result["games"][0]["replay_supported"])
        self.assertEqual(result["games"][0]["move_by"], 1234)

    def test_daily_without_pgn_is_preview_only_and_never_invents_moves(self):
        raw = game(pgn="", fen=chess.STARTING_FEN)
        result = ChessComClient(opener=Opener(Response({"games": [raw]}))).daily()
        item = result["games"][0]
        self.assertFalse(item["replay_supported"])
        self.assertIn("preview only", item["unsupported_reason"])
        self.assertEqual(item["moves"], [])
        self.assertEqual(item["positions"], [])

    def test_daily_zero_ply_pgn_is_supported_for_sync_from_standard_setup(self):
        raw = game(pgn='[Result "*"]\n\n*', fen=chess.STARTING_FEN)
        item = ChessComClient(opener=Opener(Response({"games": [raw]}))).daily()["games"][0]
        self.assertTrue(item["replay_supported"])
        self.assertEqual(item["moves"], [])
        self.assertEqual(item["positions"], [chess.STARTING_FEN])
        self.assertEqual(item["result"], "*")
        for pgn in ["*", "1-0"]:
            with self.subTest(pgn=pgn), self.assertRaises(ChessComError):
                parse_pgn(pgn, require_finished=True, allow_empty=True)

    def test_position_mismatch_variant_and_unfinished_archive_are_not_replayed(self):
        for raw in [game(fen=chess.STARTING_FEN), game(rules="chess960"), game(pgn=DAILY),
                    game(pgn="1.e4 nonsense 1-0"), game(pgn="1.e5 1-0")]:
            with self.subTest(raw=raw):
                result = ChessComClient(opener=Opener(Response({"games": [raw]}))).finished("ofroni", 2026, 10)
                self.assertFalse(result["games"][0]["replay_supported"])
                self.assertEqual(result["games"][0]["moves"], [])
                self.assertTrue(result["games"][0]["unsupported_reason"])

    def test_non_scalar_fen_and_player_metadata_are_normalized(self):
        raw = game(fen={"invalid": "fen"}, white={"username": "x" * 1000}, black=123)
        item = ChessComClient(opener=Opener(Response({"games": [raw]}))).daily()["games"][0]
        self.assertEqual(item["fen"], "")
        self.assertEqual(item["white"], "Unknown")
        self.assertEqual(item["black"], "Unknown")
        self.assertFalse(item["replay_supported"])
        self.assertIn("invalid position FEN", item["unsupported_reason"])

    def test_archives_are_month_identifiers_and_never_fetched_urls(self):
        url = API_BASE + "/player/ofroni/games/2026/10"
        opener = Opener(Response({"archives": [url]}))
        result = ChessComClient(opener=opener).archives()
        self.assertEqual(result["archives"], [{"year": 2026, "month": 10, "label": "2026-10"}])
        self.assertEqual(len(opener.requests), 1)
        for hostile in ["https://example.com/2026/10", url + "?next=evil", url.replace("2026", "../2026")]:
            with self.subTest(hostile=hostile), self.assertRaises(ChessComError):
                ChessComClient(opener=Opener(Response({"archives": [hostile]}))).archives()

    def test_finished_search_uses_selected_month_and_case_insensitive_player_filter(self):
        opener = Opener(Response({"games": [game(), game(url=GAME_URL + "7", black={"username": "Another"})]}))
        result = ChessComClient(opener=opener).finished("OFRONI", "2026", "01", "OPPONENT")
        self.assertEqual(opener.requests[0][0].full_url, API_BASE + "/player/ofroni/games/2026/01")
        self.assertEqual(result["total_games"], 2)
        self.assertEqual(len(result["games"]), 1)

    def test_invalid_inputs_make_no_network_requests(self):
        opener = Opener()
        client = ChessComClient(opener=opener)
        for username in ["../evil", "a/b", "https://example.com", "ab", " ofroni", None]:
            with self.subTest(username=username), self.assertRaises(ChessComError):
                client.daily(username)
        for year, month in [(True, 1), ("2026/x", 1), ("26", 1), (2026, 13), (2026, "../1"), (2026, True)]:
            with self.subTest(year=year, month=month), self.assertRaises(ChessComError):
                client.finished("ofroni", year, month)
        self.assertEqual(opener.requests, [])

    def test_cache_control_and_copy_isolation(self):
        ticks = [0]
        opener = Opener(Response({"games": []}, {"Cache-Control": "public, max-age=120", "Age": "20"}),
                        Response({"games": []}))
        client = ChessComClient(opener=opener, clock=lambda: ticks[0])
        first = client.daily()
        first["games"].append("mutation")
        ticks[0] = 99
        second = client.daily()
        self.assertTrue(second["cached"])
        self.assertEqual(second["games"], [])
        self.assertEqual(len(opener.requests), 1)
        ticks[0] = 101
        client.daily()
        self.assertEqual(len(opener.requests), 2)

    def test_conditional_cache_revalidation_304(self):
        ticks = [0]
        opener = Opener(Response({"games": []}, {"ETag": '"v1"', "Last-Modified": "Mon, 19 Oct 2026 12:00:00 GMT", "Cache-Control": "max-age=30"}),
                        http_error(304, Cache_Control="max-age=60"))
        client = ChessComClient(opener=opener, clock=lambda: ticks[0])
        client.daily()
        ticks[0] = 31
        result = client.daily()
        self.assertTrue(result["cached"])
        self.assertEqual(opener.requests[1][0].get_header("If-none-match"), '"v1"')
        ticks[0] = 60
        client.daily()
        self.assertEqual(len(opener.requests), 2)

    def test_daily_refresh_queries_provider_using_cached_validators(self):
        opener = Opener(Response({"games": []}, {"ETag": '"v1"', "Cache-Control": "max-age=3600"}),
                        http_error(304, Cache_Control="max-age=3600"))
        client = ChessComClient(opener=opener)
        client.daily()
        self.assertTrue(client.daily()["cached"])
        self.assertEqual(len(opener.requests), 1)
        result = client.daily(refresh=True)
        self.assertTrue(result["cached"])
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[-1][0].get_header("If-none-match"), '"v1"')

    def test_no_store_is_not_cached(self):
        opener = Opener(Response({"games": []}, {"Cache-Control": "no-store"}), Response({"games": []}))
        client = ChessComClient(opener=opener)
        self.assertEqual(client.daily()["cache_seconds"], 0)
        client.daily()
        self.assertEqual(len(opener.requests), 2)

    def test_rate_limit_backoff_and_actionable_error(self):
        ticks = [0]
        opener = Opener(http_error(429, Retry_After="90"), Response({"games": []}))
        client = ChessComClient(opener=opener, clock=lambda: ticks[0])
        for _ in range(2):
            with self.assertRaisesRegex(ChessComError, "rate limit") as caught:
                client.daily()
            self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(opener.requests), 1)
        ticks[0] = 91
        self.assertEqual(client.daily()["games"], [])

    def test_http_and_transport_errors(self):
        for error, status in [(http_error(404), 404), (http_error(410), 404), (http_error(500), 502),
                              (http_error(302), 502), (URLError("offline"), 502), (TimeoutError(), 502)]:
            with self.subTest(error=error), self.assertRaises(ChessComError) as caught:
                ChessComClient(opener=Opener(error)).daily()
            self.assertEqual(caught.exception.status, status)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "test", {}, "https://example.com"))

    def test_malformed_or_oversized_response(self):
        responses = [Response(b"not json"), Response([]), Response({"games": {}}),
                     Response(b"{}", {"Content-Length": str(MAX_BODY_BYTES + 1)}),
                     Response(b" " * (MAX_BODY_BYTES + 1))]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ChessComError):
                ChessComClient(opener=Opener(response)).daily()

    def test_invalid_game_url_is_rejected(self):
        for url in ["https://example.com/game/daily/123", "http://www.chess.com/game/daily/123", GAME_URL + "?x=y"]:
            with self.subTest(url=url), self.assertRaises(ChessComError):
                ChessComClient(opener=Opener(Response({"games": [game(url=url)]}))).daily()

    def test_requests_are_serialized_across_client_instances(self):
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        class SlowOpener:
            def open(self, request, timeout):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                time.sleep(0.01)
                with lock:
                    state["active"] -= 1
                return Response({"games": []})

        threads = [threading.Thread(target=ChessComClient(opener=SlowOpener()).daily) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state["maximum"], 1)


if __name__ == "__main__":
    unittest.main()

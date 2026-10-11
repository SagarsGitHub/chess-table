"""Resolve explicit chess expressions using only legal moves in the host position."""
import re
import chess


def resolve_move(board: chess.Board, expression: str) -> str:
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 256:
        raise ValueError("provide a bounded chess move expression")
    text = expression.strip().rstrip(".!?")
    text = re.sub(r"^(?:please\s+)?(?:move|play)\s+(?:my\s+|the\s+)?", "", text, flags=re.I)
    text = normalize_move_transcription(text)
    if re.fullmatch(r"(?:2|to|two|too)\s*[a-h]\s*[1-8]", text, re.I):
        raise ValueError("incomplete move; specify a piece or source square, such as queen to d3 or d1 to d3")
    # Coordinate and standard algebraic notation already encode disambiguation,
    # captures, castling and promotion. python-chess validates them against the board.
    for notation in (text, text.replace("0", "O")):
        try:
            move = board.parse_san(notation)
            if move and move in board.legal_moves:
                return move.uci()
        except ValueError:
            pass
    text = text.casefold()
    castle = re.fullmatch(r"castl(?:e|ing)(?: (?:on |to )?(?:the )?(king|queen)(?: side|side))?", text)
    if castle:
        moves = [m for m in board.legal_moves if board.is_castling(m) and
                 (castle[1] is None or board.is_kingside_castling(m) == (castle[1] == "king"))]
    else:
        match = re.fullmatch(
            r"(?:the |my )?(pawn|knight|bishop|rook|queen|king)"
            r"(?: (?:from |on |at )?([a-h][1-8]))? "
            r"(to|takes|captures|take|capture) (?:on )?([a-h][1-8])"
            r"(?: (?:and )?(?:promote(?:s)? to|promoting to|equals) (queen|rook|bishop|knight))?", text)
        if not match:
            # Spoken coordinates, e.g. 'e2 to e4'.
            coordinate = re.fullmatch(r"([a-h][1-8]) (to|takes) ([a-h][1-8])", text)
            if coordinate:
                try:
                    move = board.parse_uci(coordinate[1] + coordinate[3])
                except ValueError:
                    pass
                else:
                    if coordinate[2] == "takes" and not board.is_capture(move):
                        raise ValueError("no legal move matches that expression in the current position")
                    return move.uci()
            raise ValueError("unrecognized or illegal move; use 'knight from b1 to c3' or chess notation")
        piece, source, verb, target, promotion = match.groups()
        kind = {"pawn": chess.PAWN, "knight": chess.KNIGHT, "bishop": chess.BISHOP,
                "rook": chess.ROOK, "queen": chess.QUEEN, "king": chess.KING}[piece]
        promo = {"queen": chess.QUEEN, "rook": chess.ROOK, "bishop": chess.BISHOP,
                 "knight": chess.KNIGHT}.get(promotion)
        moves = [m for m in board.legal_moves
                 if board.piece_type_at(m.from_square) == kind
                 and m.to_square == chess.parse_square(target)
                 and (source is None or m.from_square == chess.parse_square(source))
                 and (verb == "to" or board.is_capture(m))
                 and (promotion is None or m.promotion == promo)]
    if not moves:
        raise ValueError("no legal move matches that expression in the current position")
    if len(moves) != 1:
        choices = ", ".join(board.san(m) + " (" + m.uci() + ")" for m in moves)
        raise ValueError("ambiguous move; specify the source square or promotion piece: " + choices)
    return moves[0].uci()


def normalize_move_transcription(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    # An explicit coordinate source is never replaced by a named piece.
    coordinate = re.fullmatch(
        r"([a-h])\s*([1-8])(?:\s*(?:->|[, _:/>–—→-]|to|two|too|2)\s*|\s+)"
        r"([a-h])\s*([1-8])", text, re.I
    )
    if coordinate:
        return f"{coordinate[1].lower()}{coordinate[2]} to {coordinate[3].lower()}{coordinate[4]}"
    if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", text, re.I):
        return text.lower()
    spoken = re.fullmatch(
        r"((?:the |my )?(?:pawn|knight|bishop|rook|queen|king)"
        r"(?: (?:from |on |at )?[a-h][1-8])?) (?:2|two|too)\s*([a-h])\s*([1-8])", text, re.I)
    if spoken:
        return f"{spoken[1].lower()} to {spoken[2].lower()}{spoken[3]}"
    return text

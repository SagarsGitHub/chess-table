"""Remote binding, legal resolution, and position fences without worker or motor ownership.

The controller calls this policy under its guard, persists accepted state, and
hands execution to its existing single game worker. Wire parsing lives separately
in remote_contract; physical receipts remain in the atomic game checkpoint.
"""

from http import HTTPStatus
import chess

from .api_errors import ApiError
from .checkpoint import GameState, RemoteMoveReceipt, RemoteMoveResolution
from .move_expression import resolve_move
from .remote_contract import MoveRequest, ResolveMoveRequest, SessionStartRequest


class RemoteSession:
    @staticmethod
    def bind(state: GameState, request: SessionStartRequest, *, busy: bool) -> bool:
        if state.host_game_id != request.host_game_id:
            raise ApiError("host game identity has changed")
        if state.source_kind is not None:
            if (state.source_kind != request.source_kind or state.source_game_id != request.source_game_id or
                    state.remote_mode != request.mode or state.remote_human != request.human):
                raise ApiError("checkpoint is bound to another source game")
            return False
        if (state.pending is not None or state.reset is not None or state.moves or
                state.initial_fen != chess.Board().fen()):
            raise ApiError("bind a remote source only to a fresh standard game")
        if busy:
            raise ApiError("another game worker is already running")
        state.source_kind = request.source_kind
        state.source_game_id = request.source_game_id
        state.remote_mode = request.mode
        state.remote_human = request.human
        state.remote_start_request_id = request.request_id
        return True

    @staticmethod
    def resolve(
        state: GameState, request: ResolveMoveRequest, *, can_move: bool, source_kind: str = "aina",
    ) -> tuple[RemoteMoveResolution, bool]:
        saved = state.remote_resolutions.get(request.request_id)
        if saved is not None:
            if saved.expression != request.expression:
                raise ApiError("request_id was already used for another expression")
            return saved, False
        if not can_move or state.source_kind != source_kind:
            raise ApiError("remote game is not waiting for your move")
        try:
            uci = resolve_move(state.board(), request.expression)
        except ValueError as exc:
            raise ApiError(str(exc), HTTPStatus.UNPROCESSABLE_ENTITY) from exc
        command = MoveRequest(request.request_id, state.host_game_id, state.source_game_id,
                              len(state.moves), state.board_fen, uci).receipt()
        resolution = RemoteMoveResolution(expression=request.expression, command=command)
        state.remote_resolutions[request.request_id] = resolution
        return resolution, True

    @staticmethod
    def existing_receipt(state: GameState, request: MoveRequest) -> RemoteMoveReceipt | None:
        existing = state.remote_receipts.get(request.request_id)
        if existing is not None and not request.matches(existing):
            raise ApiError("request_id was already used for another move")
        return existing

    @staticmethod
    def check_position(state: GameState, request: MoveRequest) -> None:
        if (state.host_game_id != request.host_game_id or
                state.source_game_id != request.source_game_id):
            raise ApiError("remote game identity has changed")
        if request.expected_ply != len(state.moves) or request.expected_fen != state.board_fen:
            raise ApiError("move precondition differs from committed position")
        resolution = state.remote_resolutions.get(request.request_id)
        if resolution is not None and not request.matches(resolution.command):
            raise ApiError("move differs from its saved resolution")
        try:
            state.board().parse_uci(request.uci)
        except ValueError as exc:
            raise ApiError("illegal UCI move", HTTPStatus.UNPROCESSABLE_ENTITY) from exc

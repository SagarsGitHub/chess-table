"""Controller refusals shared by the local GUI and authenticated remote API."""

from http import HTTPStatus


class ApiError(ValueError):
    """An expected request refusal with a structured HTTP response."""

    def __init__(self, message: str, status: HTTPStatus = HTTPStatus.CONFLICT):
        super().__init__(message)
        self.status = status


class ResetNotAccepted(ApiError):
    """A fenced reset refusal known to precede durable registration."""

    def __init__(self, message: str, request: dict):
        super().__init__(message, HTTPStatus.CONFLICT)
        self.request = request

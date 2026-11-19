"""Structured application errors returned by the API as {"error": {"code", "message"}}."""

from __future__ import annotations

_DEFAULT_STATUS = {
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "duplicate": 409,
    "rate_limited": 429,
}


class BBSError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or _DEFAULT_STATUS.get(code, 400)

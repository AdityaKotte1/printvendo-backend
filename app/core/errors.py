"""One error envelope: {"detail": "<sentence a human can act on>"}.

printvendo-owner renders `detail` straight to the user, so these strings are
product copy, not debug output. Unexpected exceptions never reach the client —
they are logged and replaced with a generic sentence, because a stack trace or
an ORM message is an information leak.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base for every error that is safe to show a user."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class BadRequest(AppError):
    status_code = 400


class Unauthorized(AppError):
    status_code = 401


class Forbidden(AppError):
    status_code = 403


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    status_code = 409


class TooManyRequests(AppError):
    status_code = 429


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong. Please try again."},
        )

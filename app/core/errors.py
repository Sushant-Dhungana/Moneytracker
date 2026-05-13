from dataclasses import dataclass
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass
class ApiError(Exception):
    status_code: int
    message: str
    code: str = "api_error"


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.warning(
            "API error %s %s -> %s (%s): %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            exc.message,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
            }
        },
    )


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "Internal server error.",
                "details": str(exc),
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

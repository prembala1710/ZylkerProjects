"""API error types. Every ApiError maps to an HTTP status and a JSON body."""

from __future__ import annotations


class ApiError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message, *, code=None, details=None, headers=None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details
        self.headers = headers or {}

    def to_dict(self):
        err = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = self.details
        return {"error": err}


class BadRequest(ApiError):
    status = 400
    code = "bad_request"


class Unauthorized(ApiError):
    status = 401
    code = "unauthorized"


class Forbidden(ApiError):
    status = 403
    code = "forbidden"


class NotFound(ApiError):
    status = 404
    code = "not_found"


class MethodNotAllowed(ApiError):
    status = 405
    code = "method_not_allowed"


class Conflict(ApiError):
    """A business rule prevents the operation (workflow, dependencies, ...)."""

    status = 409
    code = "conflict"


class PayloadTooLarge(ApiError):
    status = 413
    code = "payload_too_large"


class UnsupportedMediaType(ApiError):
    status = 415
    code = "unsupported_media_type"


class ValidationError(ApiError):
    status = 422
    code = "validation_error"

    def __init__(self, details):
        super().__init__("Validation failed", details=details)

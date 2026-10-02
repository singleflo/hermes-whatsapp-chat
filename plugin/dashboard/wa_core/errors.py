"""Domain errors. Every error carries the HTTP status the API maps it to."""

from __future__ import annotations


class WaError(Exception):
    status = 500


class NotFound(WaError):
    status = 404


class Conflict(WaError):
    status = 409


class Invalid(WaError):
    status = 400


class Unavailable(WaError):
    status = 503


class BadGateway(WaError):
    status = 502


class TooLarge(WaError):
    status = 413

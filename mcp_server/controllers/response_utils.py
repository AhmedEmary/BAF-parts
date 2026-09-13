# Copyright 2026 AgenticBrains
# License OPL-1 (see LICENSE file for full text).
"""AgenticBrains MCP - shared REST response envelope helpers.

The legacy REST surface (``/mcp/health``, ``/mcp/system/info`` etc.) uses the
same ``{success, data|error, meta}`` shape everywhere; centralising it here
keeps the response ergonomics uniform and makes it a single place to stamp
identity headers or timestamps on the whole surface.
"""

from datetime import datetime, timezone

from odoo.http import request

# Numeric HTTP status -> short symbolic error code, exposed inside the error
# envelope so client SDKs can branch on stable strings instead of raw integers.
ERROR_CODES = {
    400: "E400",  # Bad Request
    401: "E401",  # Unauthorized
    403: "E403",  # Forbidden
    404: "E404",  # Not Found
    429: "E429",  # Too Many Requests
    500: "E500",  # Internal Server Error
    503: "E503",  # Service Unavailable
}

# Identity header stamped on every AgenticBrains REST response. Useful for
# tracing, monitoring, and letting an intermediary tell an AgenticBrains fork
# apart from other MCP servers behind the same host.
AB_IDENTITY_HEADER = "X-AgenticBrains-MCP"
AB_IDENTITY_VALUE = "AgenticBrains MCP Server for Odoo"


def _default_headers():
    """Return the default header set stamped on every REST response."""
    return {
        "Content-Type": "application/json",
        AB_IDENTITY_HEADER: AB_IDENTITY_VALUE,
    }


def get_timestamp():
    """Return the current instant, ISO-8601 formatted, in UTC."""
    return datetime.now(timezone.utc).isoformat()


def _coerce_data(data):
    """Normalise ``data`` into a JSON object suitable for the envelope."""
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    try:
        return dict(data)
    except (TypeError, ValueError):
        return {"result": data}


def _build_meta(extra):
    """Return the ``meta`` block, always stamped with a fresh UTC timestamp."""
    meta = {"timestamp": get_timestamp()}
    if extra and isinstance(extra, dict):
        meta.update(extra)
    return meta


def success_response(data, meta=None):
    """Format a successful REST response.

    Shape: ``{"success": true, "data": {...}, "meta": {"timestamp": ...}}``.
    """
    payload = {
        "success": True,
        "data": _coerce_data(data),
        "meta": _build_meta(meta),
    }
    return request.make_json_response(payload, headers=_default_headers())


def error_response(message, code=None, status=400, meta=None):
    """Format an error REST response.

    Shape: ``{"success": false, "error": {"message", "code"}, "meta": {...}}``.
    ``code`` defaults to the symbol derived from ``status`` when omitted.
    """
    payload = {
        "success": False,
        "error": {
            "message": str(message) if message else "Unknown error",
            "code": code or ERROR_CODES.get(status, f"E{status}"),
        },
        "meta": _build_meta(meta),
    }
    return request.make_json_response(
        payload,
        status=status,
        headers=_default_headers(),
    )

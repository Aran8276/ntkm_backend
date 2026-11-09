"""
Uniform error envelope for every API response:

    {"error": {"code": "<code>", "message": "<human message>", "details": {...}}}

Codes follow the frontend specs: validation_error, unauthenticated,
permission_denied, account_suspended, superadmin_required, not_found,
conflict, has_active_relations, rate_limited, server_error.
"""
import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions, status
from rest_framework.exceptions import ErrorDetail
from rest_framework.response import Response

logger = logging.getLogger("core.api")


class ApiError(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "validation_error"
    default_detail = "Permintaan tidak valid."

    def __init__(self, message=None, details=None, code=None, status_code=None):
        super().__init__(detail=message or self.default_detail, code=code or self.default_code)
        self.message = str(message or self.default_detail)
        self.error_code = code or self.default_code
        self.details = details or {}
        if status_code:
            self.status_code = status_code


class BadRequest(ApiError):
    """400 with optional field details, e.g. BadRequest("…", {"legal": "…"})."""


class Conflict(ApiError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "conflict"
    default_detail = "Data sudah berubah. Muat ulang lalu coba lagi."


class HasActiveRelations(ApiError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "has_active_relations"
    default_detail = "Masih ada data aktif yang terkait."


class SuperadminRequired(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "superadmin_required"
    default_detail = "Hanya superadmin yang bisa melakukan aksi ini."


class AccountSuspended(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "account_suspended"
    default_detail = "Akun Anda dinonaktifkan."


class Forbidden(ApiError):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = "permission_denied"
    default_detail = "Anda tidak memiliki akses ke data ini."


class NotFound(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "not_found"
    default_detail = "Data tidak ditemukan."


def flatten_details(detail, prefix=""):
    """{"legal": {"nib": ["…"]}, "items": [{}, {"sell_price": ["…"]}]} → {"legal.nib": "…", "items.1.sell_price": "…"}"""
    out = {}
    if isinstance(detail, dict):
        for key, value in detail.items():
            out.update(flatten_details(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(detail, list):
        if all(isinstance(x, (str, ErrorDetail)) for x in detail):
            if detail:
                out[prefix or "non_field_errors"] = str(detail[0])
        else:
            for i, value in enumerate(detail):
                if value:
                    out.update(flatten_details(value, f"{prefix}.{i}" if prefix else str(i)))
    elif detail not in (None, ""):
        out[prefix or "non_field_errors"] = str(detail)
    return out


def _envelope(code, message, details=None, http_status=400, headers=None):
    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=http_status,
        headers=headers,
    )


def api_exception_handler(exc, context):
    if isinstance(exc, Http404):
        exc = NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = Forbidden()

    if isinstance(exc, ApiError):
        return _envelope(exc.error_code, exc.message, exc.details, exc.status_code)

    if isinstance(exc, exceptions.ValidationError):
        details = flatten_details(exc.detail)
        message = details.get("non_field_errors") or "Periksa kembali isian Anda."
        return _envelope("validation_error", message, details, 400)

    if isinstance(exc, exceptions.ParseError):
        return _envelope("validation_error", "Format data tidak valid.", {}, 400)

    if isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        return _envelope("unauthenticated", "Sesi tidak valid atau sudah berakhir.", {}, 401,
                         headers={"WWW-Authenticate": 'Bearer realm="api"'})

    if isinstance(exc, exceptions.PermissionDenied):
        return _envelope("permission_denied", str(exc.detail) or Forbidden.default_detail, {}, 403)

    if isinstance(exc, exceptions.NotFound):
        return _envelope("not_found", NotFound.default_detail, {}, 404)

    if isinstance(exc, exceptions.Throttled):
        headers = {"Retry-After": str(int(exc.wait))} if exc.wait else None
        return _envelope("rate_limited", "Terlalu banyak permintaan. Coba lagi sebentar lagi.", {}, 429, headers)

    if isinstance(exc, exceptions.MethodNotAllowed):
        return _envelope("method_not_allowed", "Metode tidak didukung.", {}, 405)

    if isinstance(exc, exceptions.UnsupportedMediaType):
        return _envelope("unsupported_media_type", "Tipe konten tidak didukung.", {}, 415)

    if isinstance(exc, exceptions.APIException):
        return _envelope(getattr(exc, "default_code", "error"), str(exc.detail), {}, exc.status_code)

    # Unknown error: keep the traceback visible while developing.
    if settings.DEBUG:
        return None
    logger.exception("Unhandled API error", exc_info=exc)
    return _envelope("server_error", "Terjadi kesalahan di server. Coba beberapa saat lagi.", {}, 500)

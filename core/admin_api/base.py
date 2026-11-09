from datetime import datetime, time

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.response import Response

from ..api.common import ListView, parse_bool, parse_csv
from ..exceptions import BadRequest, NotFound
from ..pagination import AdminPagination
from ..permissions import IsPlatformAdmin


class AdminView(ListView):
    permission_classes = [IsPlatformAdmin]
    pagination_class = AdminPagination


def reason_from(request, required=True):
    """``admin_reason`` from the body (or query string for DELETE); min 5 characters when required."""
    raw = request.data.get("admin_reason") if hasattr(request.data, "get") else None
    if raw is None:
        raw = request.query_params.get("admin_reason")
    reason = (raw or "").strip()
    if required and len(reason) < 5:
        raise BadRequest("Alasan wajib diisi (minimal 5 karakter).", {"admin_reason": "Alasan wajib diisi (minimal 5 karakter)."})
    if len(reason) > 500:
        raise BadRequest("Alasan maksimal 500 karakter.", {"admin_reason": "Alasan maksimal 500 karakter."})
    return reason


def notify_from(request, default=True):
    v = request.data.get("notify_user") if hasattr(request.data, "get") else None
    if v is None:
        return default
    return bool(v) if isinstance(v, bool) else str(v).lower() in {"1", "true", "yes"}


def include_deleted(request):
    return bool(parse_bool(request.query_params.get("include_deleted")))


def base_qs(model, request):
    return model.all_objects.all() if include_deleted(request) else model.objects.all()


def get_any(model, pk, queryset=None):
    """Fetch including soft-deleted rows (admins can open deleted data directly)."""
    qs = queryset if queryset is not None else model.all_objects.all()
    obj = qs.filter(pk=pk).first()
    if obj is None:
        raise NotFound()
    return obj


def order(qs, request, allowed, default):
    """``ordering=<field>`` / ``-<field>`` where field is a key of ``allowed`` (field → db expression)."""
    value = request.query_params.get("ordering") or default
    desc = value.startswith("-")
    key = value.lstrip("-")
    if key not in allowed:
        raise BadRequest("Urutan tidak dikenal.", {"ordering": f"Pilih salah satu: {', '.join(allowed)}."})
    fields = allowed[key] if isinstance(allowed[key], (list, tuple)) else [allowed[key]]
    return qs.order_by(*[("-" + f if desc else f) for f in fields], "-pk" if desc else "pk")


def date_bound(value, end=False):
    """Accept a date (``2026-11-04``) or datetime; dates cover the whole day."""
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is not None:
        return dt if timezone.is_aware(dt) else timezone.make_aware(dt)
    d = parse_date(value)
    if d is None:
        raise BadRequest("Tanggal tidak valid.", {"date": f"Tanggal tidak valid: {value}"})
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


def ok(data, status=200):
    return Response(data, status=status)


__all__ = ["AdminView", "reason_from", "notify_from", "include_deleted", "base_qs", "get_any", "order", "date_bound", "ok", "parse_csv", "parse_bool"]

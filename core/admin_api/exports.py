"""GET /admin/exports/{resource}.csv — CSV downloads with the same filters as the admin lists."""
import csv
import io

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

from ..exceptions import BadRequest, NotFound
from ..models import (
    Application,
    ApplicationItem,
    ApplicationStatus as S,
    AuditAction,
    Decision,
    Opportunity,
    Product,
    StockMode,
    StoreProfile,
    TargetType,
    UmkmProfile,
)
from ..services.audit import audit
from ..services.legal import verifications
from .base import AdminView, date_bound, parse_bool, parse_csv

User = get_user_model()
MAX_ROWS = 50_000


def iso(v):
    return v.isoformat() if v else ""


def _apps_filtered(q):
    qs = Application.objects.select_related("opportunity__store", "umkm")
    if parse_csv(q.get("status")):
        qs = qs.filter(status__in=parse_csv(q["status"]))
    if q.get("store_id"):
        qs = qs.filter(opportunity__store_id=q["store_id"])
    if q.get("umkm_id"):
        qs = qs.filter(umkm_id=q["umkm_id"])
    if q.get("created_after"):
        qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
    if q.get("created_before"):
        qs = qs.filter(created_at__lte=date_bound(q["created_before"], end=True))
    return qs


def export_users(q):
    qs = User.objects.filter(deleted_at__isnull=True)
    roles = parse_csv(q.get("role"))
    if roles:
        cond = Q(pk__in=[])
        for r in roles:
            cond |= Q(roles=[]) if r == "none" else Q(roles__icontains=f'"{r}"')
        qs = qs.filter(cond)
    if parse_csv(q.get("status")):
        qs = qs.filter(status__in=parse_csv(q["status"]))
    header = ["id", "name", "email", "roles", "status", "is_admin", "admin_role", "last_login_at", "created_at"]
    rows = ([u.id, u.name, u.email, ",".join(u.roles or []), u.status, u.is_admin, u.admin_role or "", iso(u.last_login), iso(u.created_at)] for u in qs)
    return qs, header, rows


def export_stores(q):
    qs = StoreProfile.objects.select_related("user")
    if parse_csv(q.get("user_status")):
        qs = qs.filter(user__status__in=parse_csv(q["user_status"]))
    header = ["id", "owner_email", "name", "phone", "address", "lat", "lng", "owner_status", "created_at"]
    rows = ([s.id, s.user.email, s.name, s.phone, s.address, s.lat, s.lng, s.user.status, iso(s.created_at)] for s in qs)
    return qs, header, rows


def export_umkms(q):
    qs = UmkmProfile.objects.select_related("user").prefetch_related("legal_verifications")
    if parse_csv(q.get("legal_status")):
        qs = qs.filter(legal_verifications__status__in=parse_csv(q["legal_status"])).distinct()
    header = ["id", "owner_email", "business_name", "owner_name", "phone", "address", "nib", "nib_status",
              "pirt_number", "pirt_status", "halal_number", "halal_status", "created_at"]

    def rows():
        for u in qs:
            v = verifications(u)
            yield [u.id, u.user.email, u.business_name, u.owner_name, u.phone, u.address, u.nib, v["nib"].status,
                   u.pirt_number, v["pirt"].status, u.halal_number, v["halal"].status, iso(u.created_at)]

    return qs, header, rows()


def export_opportunities(q):
    qs = Opportunity.objects.select_related("store").prefetch_related("categories")
    if parse_csv(q.get("status")):
        qs = qs.filter(status__in=parse_csv(q["status"]))
    if q.get("store_id"):
        qs = qs.filter(store_id=q["store_id"])
    if q.get("created_after"):
        qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
    if q.get("created_before"):
        qs = qs.filter(created_at__lte=date_bound(q["created_before"], end=True))
    header = ["id", "store_name", "title", "categories", "commission_type", "commission_value", "quota", "quota_used",
              "max_products_per_umkm", "valid_until", "status", "created_at"]
    rows = ([o.id, o.store.name, o.title, ",".join(c.name for c in o.categories.all()), o.commission.get("type"), o.commission.get("value"),
             o.quota, o.quota_used, o.max_products_per_umkm, o.valid_until.isoformat(), o.status, iso(o.created_at)] for o in qs)
    return qs, header, rows


def export_products(q):
    qs = Product.objects.select_related("umkm", "category")
    if q.get("umkm_id"):
        qs = qs.filter(umkm_id=q["umkm_id"])
    if parse_csv(q.get("category")):
        qs = qs.filter(category__slug__in=parse_csv(q["category"]))
    active = parse_bool(q.get("is_active"))
    if active is not None:
        qs = qs.filter(is_active=active)
    header = ["id", "umkm_name", "name", "category", "base_price", "suggested_retail_price", "shelf_life_days", "expiry_date", "is_active", "created_at"]
    rows = ([p.id, p.umkm.business_name, p.name, p.category.name, p.base_price, p.suggested_retail_price, p.shelf_life_days or "",
             p.expiry_date.isoformat() if p.expiry_date else "", p.is_active, iso(p.created_at)] for p in qs)
    return qs, header, rows


def export_applications(q):
    qs = _apps_filtered(q).prefetch_related("items")
    header = ["id", "store_name", "umkm_name", "opportunity_title", "status", "items_count", "items_approved",
              "delivery_date", "delivery_start", "delivery_end", "created_at", "updated_at"]
    rows = ([a.id, a.opportunity.store.name, a.umkm.business_name, a.opportunity.title, a.status, len(a.items.all()),
             sum(1 for i in a.items.all() if i.decision == Decision.APPROVED), iso(a.delivery_date),
             a.delivery_start.strftime("%H:%M") if a.delivery_start else "", a.delivery_end.strftime("%H:%M") if a.delivery_end else "",
             iso(a.created_at), iso(a.updated_at)] for a in qs)
    return qs, header, rows


def export_application_items(q):
    apps = _apps_filtered(q)
    qs = ApplicationItem.objects.filter(application__in=apps).select_related("application__opportunity__store", "application__umkm")
    header = ["application_id", "item_id", "store_name", "umkm_name", "product_name", "decision", "approved_quantity", "sell_price",
              "commission_per_unit", "umkm_income_per_unit", "received_quantity", "stock_quantity", "units_sold", "status"]

    def rows():
        for i in qs:
            a = i.application
            sold = ""
            if a.stock_mode == StockMode.COUNT and i.received_quantity is not None and i.stock_quantity is not None:
                sold = i.received_quantity - i.stock_quantity
            yield [a.id, i.id, a.opportunity.store.name, a.umkm.business_name, i.product_name, i.decision, i.approved_quantity or "",
                   i.sell_price or "", i.commission_per_unit if i.commission_per_unit is not None else "",
                   i.umkm_income_per_unit if i.umkm_income_per_unit is not None else "", i.received_quantity if i.received_quantity is not None else "",
                   i.stock_quantity if i.stock_quantity is not None else "", sold, a.status]

    return qs, header, rows()


def export_stock(q):
    qs = ApplicationItem.objects.filter(decision=Decision.APPROVED, application__status=S.RECEIVED).select_related(
        "application__opportunity__store", "application__umkm"
    )
    if q.get("store_id"):
        qs = qs.filter(application__opportunity__store_id=q["store_id"])
    if q.get("umkm_id"):
        qs = qs.filter(application__umkm_id=q["umkm_id"])
    if q.get("stock_status"):
        qs = qs.filter(stock_status=q["stock_status"])
    header = ["application_id", "item_id", "store_name", "umkm_name", "product_name", "sell_price", "stock_mode",
              "received_quantity", "stock_quantity", "stock_status", "stock_updated_at"]
    rows = ([i.application_id, i.id, i.application.opportunity.store.name, i.application.umkm.business_name, i.product_name,
             i.sell_price, i.application.stock_mode, i.received_quantity, i.stock_quantity if i.stock_quantity is not None else "",
             i.stock_status, iso(i.stock_updated_at)] for i in qs)
    return qs, header, rows


EXPORTS = {
    "users": (export_users, TargetType.USER),
    "stores": (export_stores, TargetType.STORE),
    "umkms": (export_umkms, TargetType.UMKM),
    "opportunities": (export_opportunities, TargetType.OPPORTUNITY),
    "products": (export_products, TargetType.PRODUCT),
    "applications": (export_applications, TargetType.APPLICATION),
    "application-items": (export_application_items, TargetType.APPLICATION),
    "stock": (export_stock, TargetType.APPLICATION),
}


class ExportView(AdminView):
    pagination_class = None

    def get(self, request, resource):
        if resource not in EXPORTS:
            raise NotFound("Jenis ekspor tidak dikenal.")
        fn, target = EXPORTS[resource]
        qs, header, rows = fn(request.query_params)
        if qs.count() > MAX_ROWS:
            raise BadRequest("Data terlalu banyak. Persempit rentang tanggal atau filter.", {"rows": f"Maksimal {MAX_ROWS} baris."})
        buf = io.StringIO()
        buf.write("﻿")  # BOM so Excel opens UTF-8 correctly
        writer = csv.writer(buf)
        writer.writerow(header)
        count = 0
        for row in rows:
            writer.writerow(row)
            count += 1
        filters = {k: v for k, v in request.query_params.items()}
        audit(request, AuditAction.EXPORT, target, resource, f"Ekspor {resource}.csv ({count} baris)", "",
              [{"field": "filters", "before": None, "after": str(filters) if filters else "semua"}])
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{resource}-{stamp}.csv"'
        return response

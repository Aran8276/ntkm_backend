"""
Response shapes for the user app (PWA). Kept as plain functions so nested
objects match the frontend types exactly (see lib/types.ts in the frontend).
"""
from django.db.models import Count

from .models import (
    ApplicationStatus as S,
    Decision,
    StockStatus,
)
from .services.files import file_brief, file_url
from .services.formatting import hhmm
from .services.legal import legal_summary
from .services.workflow import available_actions


def iso(dt):
    return dt.isoformat() if dt else None


def category_out(c):
    return {"id": str(c.id), "name": c.name, "slug": c.slug}


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def store_out(s, request=None, distance_km=None, full=True):
    data = {
        "id": str(s.id),
        "name": s.name,
        "phone": s.phone,
        "address": s.address,
        "lat": s.lat,
        "lng": s.lng,
        "photo_url": file_url(s.photo, request),
        "operating_hours": s.operating_hours,
        "updated_at": iso(s.updated_at),
    }
    if distance_km is not None:
        data["distance_km"] = distance_km
    return data


def store_profile_out(s, request=None):
    return {
        **store_out(s, request),
        "photo_file_id": str(s.photo_id) if s.photo_id else None,
        "photo": file_brief(s.photo, request),
        "created_at": iso(s.created_at),
    }


def umkm_out(u, request=None):
    return {
        "id": str(u.id),
        "business_name": u.business_name,
        "owner_name": u.owner_name,
        "phone": u.phone,
        "description": u.description,
        "address": u.address,
        "lat": u.lat,
        "lng": u.lng,
        "logo_url": file_url(u.logo, request),
        "legal": legal_summary(u),
        "updated_at": iso(u.updated_at),
    }


def umkm_profile_out(u, request=None):
    docs = list(u.legal_documents.all())
    return {
        **umkm_out(u, request),
        "logo_file_id": str(u.logo_id) if u.logo_id else None,
        "logo": file_brief(u.logo, request),
        "legal_document_file_ids": [str(d.id) for d in docs],
        "legal_documents": [file_brief(d, request) for d in docs],
        "created_at": iso(u.created_at),
    }


def me_out(user, unread=0):
    store, umkm = user.store, user.umkm
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url or None,
        "roles": user.roles or [],
        "status": user.status,
        "has_password": user.has_password,
        "onboarding": {"store_completed": store is not None, "umkm_completed": umkm is not None},
        "store_profile": {"id": str(store.id), "name": store.name} if store else None,
        "umkm_profile": {"id": str(umkm.id), "business_name": umkm.business_name} if umkm else None,
        "is_admin": user.is_admin,
        "admin_role": user.admin_role if user.is_admin else None,
        "unread_notifications": unread,
    }


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def active_placements(product):
    """Number of stores currently selling the product (received applications, item approved)."""
    return (
        product.application_items.filter(decision=Decision.APPROVED, application__status=S.RECEIVED)
        .values("application__opportunity__store")
        .distinct()
        .count()
    )


def product_out(p, request=None):
    photos = [ph.file for ph in p.photos.all() if not ph.file.is_removed]
    return {
        "id": str(p.id),
        "name": p.name,
        "category": category_out(p.category),
        "description": p.description,
        "tags": p.tags,
        "base_price": p.base_price,
        "suggested_retail_price": p.suggested_retail_price,
        "shelf_life_days": p.shelf_life_days,
        "expiry_date": p.expiry_date.isoformat() if p.expiry_date else None,
        "active_placements": active_placements(p),
        "is_active": p.is_active,
        "photo_count": len(photos),
        "photo_url": file_url(photos[0], request) if photos else None,
        "photo_file_ids": [str(f.id) for f in photos],
        "photos": [file_brief(f, request) for f in photos],
        "created_at": iso(p.created_at),
        "updated_at": iso(p.updated_at),
    }


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


def application_counts(opportunity):
    rows = opportunity.applications.values("status").annotate(n=Count("id"))
    return {r["status"]: r["n"] for r in rows}


def opportunity_out(o, request=None, *, distance_km=None, match=None, my_application="omit", counts=True):
    data = {
        "id": str(o.id),
        "title": o.title,
        "description": o.description,
        "store": store_out(o.store, request, distance_km),
        "categories": [category_out(c) for c in o.categories.all()],
        "requirements": {
            "min_shelf_life_days": (o.requirements or {}).get("min_shelf_life_days"),
            "max_retail_price": (o.requirements or {}).get("max_retail_price"),
            "requires_nib": bool((o.requirements or {}).get("requires_nib")),
            "requires_pirt": bool((o.requirements or {}).get("requires_pirt")),
            "requires_halal": bool((o.requirements or {}).get("requires_halal")),
            "notes": (o.requirements or {}).get("notes", ""),
        },
        "quota": o.quota,
        "quota_used": o.quota_used,
        "quota_remaining": o.quota_remaining,
        "max_products_per_umkm": o.max_products_per_umkm,
        "commission": o.commission,
        "receiving_schedule": o.receiving_schedule,
        "valid_until": o.valid_until.isoformat(),
        "is_expired": o.is_expired,
        "status": o.status,
        "created_at": iso(o.created_at),
        "updated_at": iso(o.updated_at),
    }
    if counts:
        data["application_counts"] = application_counts(o)
    if match is not None:
        data["match"] = match
    if my_application != "omit":
        data["my_application"] = (
            {"id": str(my_application.id), "status": my_application.status} if my_application else None
        )
    return data


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


def item_product_out(item, request=None):
    return {
        "id": str(item.product_id),
        "name": item.product_name,
        "category": {"name": item.category_name},
        "base_price": item.base_price,
        "suggested_retail_price": item.suggested_retail_price,
        "shelf_life_days": item.shelf_life_days,
        "expiry_date": item.expiry_date.isoformat() if item.expiry_date else None,
        "photo_url": file_url(item.photo, request),
    }


def item_out(item, request=None):
    return {
        "id": item.id,
        "product": item_product_out(item, request),
        "decision": item.decision,
        "approved_quantity": item.approved_quantity,
        "sell_price": item.sell_price,
        "commission_per_unit": item.commission_per_unit,
        "umkm_income_per_unit": item.umkm_income_per_unit,
        "reject_reason": item.reject_reason or None,
        "received_quantity": item.received_quantity,
        "stock_quantity": item.stock_quantity,
        "stock_status": item.stock_status,
        "stock_updated_at": iso(item.stock_updated_at),
    }


def delivery_slot_out(app):
    if not app.delivery_date:
        return None
    from datetime import datetime

    from django.utils import timezone

    tz = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(app.delivery_date, app.delivery_start), tz)
    end_at = timezone.make_aware(datetime.combine(app.delivery_date, app.delivery_end), tz)
    return {
        "date": app.delivery_date.isoformat(),
        "start": hhmm(app.delivery_start),
        "end": hhmm(app.delivery_end),
        "start_at": start_at.isoformat(),
        "end_at": end_at.isoformat(),
    }


def counterpart_out(app, viewer, request=None):
    if viewer == "store":
        u = app.umkm
        return {"type": "umkm", "id": str(u.id), "name": u.business_name, "image_url": file_url(u.logo, request), "phone": u.phone}
    s = app.opportunity.store
    return {"type": "store", "id": str(s.id), "name": s.name, "image_url": file_url(s.photo, request), "phone": s.phone, "address": s.address}


def application_out(app, viewer, request=None):
    items = list(app.items.all())
    o = app.opportunity
    return {
        "id": str(app.id),
        "opportunity": {
            "id": str(o.id),
            "title": o.title,
            "commission": app.commission,
            "receiving_schedule": o.receiving_schedule,
        },
        "store": store_out(o.store, request),
        "umkm": umkm_out(app.umkm, request),
        "counterpart": counterpart_out(app, viewer, request),
        "status": app.status,
        "items": [item_out(i, request) for i in items],
        "items_count": len(items),
        "approved_count": sum(1 for i in items if i.decision == Decision.APPROVED),
        "note": app.note,
        "review_note": app.review_note,
        "stock_mode": app.stock_mode,
        "delivery_slot": delivery_slot_out(app),
        "created_at": iso(app.created_at),
        "updated_at": iso(app.updated_at),
        "viewer_role": viewer,
        "available_actions": available_actions(app, viewer),
    }


def event_out(e):
    return {
        "id": e.id,
        "from_status": e.from_status,
        "to_status": e.to_status,
        "label": e.label,
        "actor": {"name": e.actor_name, "role": e.actor_role},
        "note": e.note,
        "is_admin_override": e.is_admin_override,
        "admin_reason": e.admin_reason or None,
        "created_at": iso(e.created_at),
    }


def stock_row_out(item, viewer, request=None):
    app = item.application
    s = app.opportunity.store
    return {
        "application_id": str(app.id),
        "item_id": item.id,
        "product": item_product_out(item, request),
        "store": {"id": str(s.id), "name": s.name, "photo_url": file_url(s.photo, request), "phone": s.phone},
        "umkm": {"id": str(app.umkm.id), "business_name": app.umkm.business_name, "logo_url": file_url(app.umkm.logo, request), "phone": app.umkm.phone},
        "counterpart": counterpart_out(app, viewer, request),
        "sell_price": item.sell_price,
        "stock_mode": app.stock_mode,
        "received_quantity": item.received_quantity,
        "stock_quantity": item.stock_quantity,
        "stock_status": item.stock_status or StockStatus.AVAILABLE,
        "stock_updated_at": iso(item.stock_updated_at),
        "can_update": viewer == "store" and app.status == S.RECEIVED,
    }


def notification_out(n):
    return {
        "id": str(n.id),
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "is_read": n.read_at is not None,
        "read_at": iso(n.read_at),
        "created_at": iso(n.created_at),
        "data": n.data,
    }


def unread_count(user):
    return user.notifications.filter(read_at__isnull=True).count()


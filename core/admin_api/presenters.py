"""Response shapes for the admin panel (A01–A25)."""
from django.db.models import Q

from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    Application,
    ApplicationItem,
    ApplicationStatus as S,
    Decision,
    Opportunity,
    OpportunityStatus,
    Product,
    StockStatus,
    StoreProfile,
    UmkmProfile,
)
from ..presenters import (
    active_placements,
    application_counts,
    category_out,
    delivery_slot_out,
    event_out,
    item_out,
    iso,
    opportunity_out,
    product_out,
    store_profile_out,
    umkm_profile_out,
)
from ..services.files import file_brief, file_url
from ..services.legal import legal_summary, verifications, waiting_since
from ..services.workflow import ADMIN_TRANSITIONS


def person(u):
    return {"id": str(u.id), "name": u.name or u.email} if u else None


def owner_out(u):
    return {"id": str(u.id), "name": u.name, "email": u.email, "status": u.status}


def created_by_out(u):
    if u is None:
        return None
    return {"id": str(u.id), "name": u.name or u.email, "is_admin": bool(u.is_admin)}


def deleted_out(obj):
    return {"deleted_at": iso(obj.deleted_at), "deleted_by": person(obj.deleted_by)}


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def user_list_out(u):
    store = StoreProfile.all_objects.filter(user=u).first()
    umkm = UmkmProfile.all_objects.filter(user=u).first()
    return {
        "id": str(u.id),
        "name": u.name,
        "email": u.email,
        "avatar_url": u.avatar_url or None,
        "roles": u.roles or [],
        "status": u.status,
        "is_admin": u.is_admin,
        "admin_role": u.admin_role if u.is_admin else None,
        "store_profile": {"id": str(store.id), "name": store.name, "is_deleted": store.deleted_at is not None} if store else None,
        "umkm_profile": {"id": str(umkm.id), "business_name": umkm.business_name, "is_deleted": umkm.deleted_at is not None} if umkm else None,
        "last_login_at": iso(u.last_login),
        "created_at": iso(u.created_at),
        **deleted_out(u),
    }


def user_stats(u):
    store = StoreProfile.all_objects.filter(user=u).first()
    umkm = UmkmProfile.all_objects.filter(user=u).first()
    apps = Application.objects.filter(Q(opportunity__store=store) | Q(umkm=umkm)) if (store or umkm) else Application.objects.none()
    return {
        "opportunities": Opportunity.objects.filter(store=store).count() if store else 0,
        "products": Product.objects.filter(umkm=umkm).count() if umkm else 0,
        "applications_active": apps.filter(status__in=ACTIVE_APPLICATION_STATUSES).count(),
        "applications_total": apps.count(),
    }


def user_detail_out(u):
    return {
        **user_list_out(u),
        "invited_by": person(u.invited_by),
        "suspended_at": iso(u.suspended_at),
        "suspended_reason": u.suspended_reason or None,
        "stats": user_stats(u),
    }


# ---------------------------------------------------------------------------
# Stores & UMKMs
# ---------------------------------------------------------------------------


def store_stats(s):
    apps = Application.objects.filter(opportunity__store=s)
    return {
        "opportunities_open": Opportunity.objects.filter(store=s, status=OpportunityStatus.OPEN).count(),
        "applications_active": apps.filter(status__in=ACTIVE_APPLICATION_STATUSES).count(),
        "items_on_shelf": ApplicationItem.objects.filter(
            application__opportunity__store=s, application__status=S.RECEIVED, decision=Decision.APPROVED, stock_status=StockStatus.AVAILABLE
        ).count(),
    }


def admin_store_out(s, request=None):
    return {
        **store_profile_out(s, request),
        "owner": owner_out(s.user),
        "stats": store_stats(s),
        **deleted_out(s),
    }


def umkm_stats(u):
    return {
        "products_active": Product.objects.filter(umkm=u, is_active=True).count(),
        "applications_active": Application.objects.filter(umkm=u, status__in=ACTIVE_APPLICATION_STATUSES).count(),
        "items_on_shelf": ApplicationItem.objects.filter(
            application__umkm=u, application__status=S.RECEIVED, decision=Decision.APPROVED, stock_status=StockStatus.AVAILABLE
        ).count(),
    }


def legal_verification_out(u):
    out = {}
    for kind, v in verifications(u).items():
        out[kind] = {
            "status": v.status,
            "number": u.legal_number(kind),
            "note": v.note or None,
            "decided_by": person(v.decided_by) if v.decided_by_id else None,
            "decided_at": iso(v.decided_at),
            "submitted_at": iso(v.submitted_at),
        }
    return out


def admin_umkm_out(u, request=None):
    return {
        **umkm_profile_out(u, request),
        "owner": owner_out(u.user),
        "legal_verification": legal_verification_out(u),
        "waiting_since": iso(waiting_since(u)),
        "stats": umkm_stats(u),
        **deleted_out(u),
    }


# ---------------------------------------------------------------------------
# Opportunities & products
# ---------------------------------------------------------------------------


def admin_opportunity_out(o, request=None):
    data = opportunity_out(o, request)
    data["store"] = {**data["store"], "owner_id": str(o.store.user_id), "is_deleted": o.store.deleted_at is not None}
    data["created_by"] = created_by_out(o.created_by)
    data["has_applications"] = o.applications.exists()
    data.update(deleted_out(o))
    return data


def product_history(p):
    rows = (
        ApplicationItem.objects.filter(product=p)
        .select_related("application__opportunity__store")
        .order_by("-application__created_at")
    )
    return [
        {
            "application_id": str(i.application_id),
            "item_id": i.id,
            "store": {"id": str(i.application.opportunity.store_id), "name": i.application.opportunity.store.name},
            "status": i.application.status,
            "decision": i.decision,
            "sell_price": i.sell_price,
            "stock_status": i.stock_status,
            "stock_quantity": i.stock_quantity,
            "created_at": iso(i.application.created_at),
        }
        for i in rows
    ]


def admin_product_out(p, request=None, detail=False):
    data = product_out(p, request)
    data["umkm"] = {"id": str(p.umkm_id), "business_name": p.umkm.business_name, "owner_id": str(p.umkm.user_id)}
    data["created_by"] = created_by_out(p.created_by)
    data["has_applications"] = p.application_items.exists()
    data.update(deleted_out(p))
    if detail:
        data["applications"] = product_history(p)
    return data


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


def admin_application_list_out(a):
    items = list(a.items.all())
    return {
        "id": str(a.id),
        "store": {"id": str(a.opportunity.store_id), "name": a.opportunity.store.name, "owner_id": str(a.opportunity.store.user_id)},
        "umkm": {"id": str(a.umkm_id), "business_name": a.umkm.business_name, "owner_id": str(a.umkm.user_id)},
        "opportunity": {"id": str(a.opportunity_id), "title": a.opportunity.title},
        "status": a.status,
        "items_count": len(items),
        "approved_count": sum(1 for i in items if i.decision == Decision.APPROVED),
        "delivery_slot": delivery_slot_out(a),
        "has_override": any(e.is_admin_override for e in a.events.all()),
        "created_at": iso(a.created_at),
        "updated_at": iso(a.updated_at),
    }


def admin_application_out(a, request=None):
    events = list(a.events.all())
    s, u, o = a.opportunity.store, a.umkm, a.opportunity
    overrides = sum(1 for e in events if e.is_admin_override)
    return {
        **admin_application_list_out(a),
        "store": {
            "id": str(s.id), "name": s.name, "phone": s.phone, "address": s.address, "owner_id": str(s.user_id),
            "photo_url": file_url(s.photo, request),
        },
        "umkm": {
            "id": str(u.id), "business_name": u.business_name, "owner_name": u.owner_name, "phone": u.phone,
            "owner_id": str(u.user_id), "logo_url": file_url(u.logo, request), "legal": legal_summary(u),
        },
        "opportunity": {
            "id": str(o.id), "title": o.title, "commission": a.commission,
            "current_commission": o.commission, "receiving_schedule": o.receiving_schedule,
        },
        "items": [item_out(i, request) for i in a.items.all()],
        "note": a.note,
        "review_note": a.review_note,
        "stock_mode": a.stock_mode,
        "timeline": [event_out(e) for e in events],
        "has_override": overrides > 0,
        "override_count": overrides,
        "admin_can_review": a.status == S.SUBMITTED,
        "admin_transitions": ADMIN_TRANSITIONS.get(a.status, []),
        "created_by": created_by_out(a.created_by),
    }


def admin_stock_out(i, request=None):
    a = i.application
    return {
        "application_id": str(a.id),
        "item_id": i.id,
        "product": {"id": str(i.product_id), "name": i.product_name, "photo_url": file_url(i.photo, request)},
        "umkm": {"id": str(a.umkm_id), "business_name": a.umkm.business_name},
        "store": {"id": str(a.opportunity.store_id), "name": a.opportunity.store.name},
        "sell_price": i.sell_price,
        "stock_mode": a.stock_mode,
        "received_quantity": i.received_quantity,
        "stock_quantity": i.stock_quantity,
        "stock_status": i.stock_status,
        "stock_updated_at": iso(i.stock_updated_at),
        "stock_updated_by": (
            {"id": str(i.stock_updated_by_id), "name": i.stock_updated_by.name or i.stock_updated_by.email, "is_admin": i.stock_updated_by.is_admin}
            if i.stock_updated_by_id else None
        ),
    }


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------


def admin_category_out(c):
    return {
        **category_out(c),
        "is_active": c.is_active,
        "sort_order": c.sort_order,
        "usage": category_usage(c),
    }


def file_usages(f):
    used = []
    for s in StoreProfile.all_objects.filter(photo=f):
        used.append({"type": "store", "id": str(s.id), "label": f"Profil toko · {s.name}", "owner_id": str(s.user_id)})
    for u in UmkmProfile.all_objects.filter(logo=f):
        used.append({"type": "umkm", "id": str(u.id), "label": f"Logo UMKM · {u.business_name}", "owner_id": str(u.user_id)})
    for u in f.legal_for.all():
        used.append({"type": "legal_document", "id": str(u.id), "label": f"Dokumen legalitas · {u.business_name}", "owner_id": str(u.user_id)})
    for ph in f.product_photos.select_related("product"):
        used.append({"type": "product", "id": str(ph.product_id), "label": f"Produk · {ph.product.name}"})
    return used


def admin_file_out(f, request=None):
    return {
        **file_brief(f, request),
        "owner": {"id": str(f.owner_id), "name": f.owner.name, "email": f.owner.email},
        "uploaded_by": person(f.uploaded_by),
        "used_in": file_usages(f),
        "removed_at": iso(f.removed_at),
        "removed_by": person(f.removed_by),
        "created_at": iso(f.created_at),
    }


def admin_notification_out(n):
    return {
        "id": str(n.id),
        "user": {"id": str(n.user_id), "name": n.user.name, "email": n.user.email},
        "type": n.type,
        "title": n.title,
        "body": n.body,
        "data": n.data,
        "email_status": n.email_status,
        "email_attempts": n.email_attempts,
        "email_error": n.email_error or None,
        "read_at": iso(n.read_at),
        "created_at": iso(n.created_at),
    }


def audit_out(log):
    return {
        "id": str(log.id),
        "admin": {"id": str(log.admin_id) if log.admin_id else None, "name": log.admin_name},
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "target_label": log.target_label,
        "reason": log.reason or None,
        "changes": log.changes,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "created_at": iso(log.created_at),
    }


def category_usage(c):
    """Usage counts include soft-deleted rows (they still reference the category)."""
    return {
        "products": Product.all_objects.filter(category=c).count(),
        "opportunities": Opportunity.all_objects.filter(categories=c).count(),
    }


__all__ = [
    "admin_store_out", "admin_umkm_out", "admin_opportunity_out", "admin_product_out",
    "admin_application_out", "admin_application_list_out", "admin_stock_out", "admin_category_out",
    "admin_file_out", "admin_notification_out", "audit_out", "user_list_out", "user_detail_out",
    "category_usage", "active_placements", "application_counts",
]

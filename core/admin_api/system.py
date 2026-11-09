import statistics
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from ..exceptions import BadRequest, Conflict, HasActiveRelations, NotFound
from ..models import (
    AccountStatus,
    AdminAuditLog,
    Application,
    ApplicationEvent,
    ApplicationItem,
    ApplicationStatus as S,
    AuditAction,
    Category,
    Decision,
    EmailStatus,
    Notification,
    NotificationType,
    Opportunity,
    OpportunityStatus,
    Product,
    ProductPhoto,
    StockStatus,
    StoreProfile,
    TargetType,
    UmkmProfile,
    UploadedFile,
    VerificationStatus,
)
from ..services.audit import audit, diff
from ..services.notify import deliver_email, notify
from .base import AdminView, date_bound, notify_from, ok, order, parse_bool, parse_csv, reason_from
from .presenters import admin_category_out, admin_file_out, admin_notification_out, audit_out, category_usage

User = get_user_model()


# ---------------------------------------------------------------------------
# Categories (A19)
# ---------------------------------------------------------------------------


class CategoryInput(serializers.Serializer):
    name = serializers.CharField(max_length=80)
    slug = serializers.SlugField(max_length=80, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False)


def unique_check(name, slug, exclude=None):
    qs = Category.objects.exclude(pk=exclude.pk) if exclude else Category.objects.all()
    if qs.filter(name__iexact=name).exists():
        raise BadRequest("Nama kategori sudah dipakai.", {"name": "Nama kategori sudah dipakai."})
    if qs.filter(slug=slug).exists():
        raise BadRequest("Slug sudah dipakai.", {"slug": "Slug sudah dipakai."})


class CategoryListView(AdminView):
    """GET|POST /admin/categories (no pagination)"""

    pagination_class = None

    def get(self, request):
        return ok([admin_category_out(c) for c in Category.objects.all()])

    def post(self, request):
        s = CategoryInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        slug = d.get("slug") or slugify(d["name"].replace("&", "dan"))
        unique_check(d["name"], slug)
        last = Category.objects.aggregate(m=Max("sort_order"))["m"] or 0
        c = Category.objects.create(name=d["name"].strip(), slug=slug, is_active=d["is_active"], sort_order=d.get("sort_order", last + 10))
        audit(request, AuditAction.CREATE, TargetType.CATEGORY, c.id, c.name, reason_from(request, required=False))
        return ok(admin_category_out(c), 201)


class CategoryDetailView(AdminView):
    """PATCH|DELETE /admin/categories/{id}"""

    def get_category(self, pk):
        c = Category.objects.filter(pk=pk).first()
        if c is None:
            raise NotFound()
        return c

    def patch(self, request, pk):
        c = self.get_category(pk)
        s = CategoryInput(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        before = {"name": c.name, "slug": c.slug, "is_active": c.is_active, "sort_order": c.sort_order}
        name = d.get("name", c.name).strip()
        slug = d.get("slug") or c.slug
        unique_check(name, slug, exclude=c)
        c.name, c.slug = name, slug
        if "is_active" in d:
            c.is_active = d["is_active"]
        if "sort_order" in d:
            c.sort_order = d["sort_order"]
        c.save()
        changes = diff(before, {"name": c.name, "slug": c.slug, "is_active": c.is_active, "sort_order": c.sort_order})
        if changes:
            audit(request, AuditAction.UPDATE, TargetType.CATEGORY, c.id, c.name, reason_from(request, required=False), changes)
        return ok(admin_category_out(c))

    def delete(self, request, pk):
        c = self.get_category(pk)
        usage = category_usage(c)
        if usage["products"] or usage["opportunities"]:
            raise HasActiveRelations("Nonaktifkan saja, kategori masih dipakai.", usage)
        audit(request, AuditAction.DELETE, TargetType.CATEGORY, c.id, c.name, reason_from(request, required=False))
        c.delete()
        return ok({"deleted": True})


# ---------------------------------------------------------------------------
# Files (A20)
# ---------------------------------------------------------------------------


class FileListView(AdminView):
    """GET /admin/files ?purpose=&owner_id=&is_removed=&created_after="""

    def get(self, request):
        qs = UploadedFile.objects.select_related("owner", "uploaded_by", "removed_by")
        q = request.query_params
        if parse_csv(q.get("purpose")):
            qs = qs.filter(purpose__in=parse_csv(q["purpose"]))
        if q.get("owner_id"):
            qs = qs.filter(owner_id=q["owner_id"])
        removed = parse_bool(q.get("is_removed"))
        if removed is not None:
            qs = qs.filter(is_removed=removed)
        if q.get("created_after"):
            qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
        qs = order(qs, request, {"created_at": "created_at", "size": "size"}, "-created_at")
        return self.paged(qs, lambda f: admin_file_out(f, request))


class FileDetailView(AdminView):
    """GET /admin/files/{id}"""

    def get(self, request, pk):
        f = UploadedFile.objects.filter(pk=pk).first()
        if f is None:
            raise NotFound()
        return ok(admin_file_out(f, request))


class FileRemoveView(AdminView):
    """POST /admin/files/{id}/remove {admin_reason, notify_user} — detaches the file everywhere."""

    def post(self, request, pk):
        f = UploadedFile.objects.filter(pk=pk).select_related("owner").first()
        if f is None:
            raise NotFound()
        reason = reason_from(request)
        if f.is_removed:
            raise Conflict("File sudah dihapus.")
        with transaction.atomic():
            StoreProfile.all_objects.filter(photo=f).update(photo=None)
            UmkmProfile.all_objects.filter(logo=f).update(logo=None)
            for u in f.legal_for.all():
                u.legal_documents.remove(f)
            ProductPhoto.objects.filter(file=f).delete()
            f.is_removed, f.removed_at, f.removed_by = True, timezone.now(), request.user
            f.save(update_fields=["is_removed", "removed_at", "removed_by"])
        audit(request, AuditAction.REMOVE_FILE, TargetType.FILE, f.id, f.original_name, reason)
        if notify_from(request):
            notify(f.owner, NotificationType.ADMIN_ACTION, "File Anda dihapus admin",
                   f"File {f.original_name} dihapus admin. Alasan: {reason} Unggah file pengganti bila diperlukan.")
        return ok(admin_file_out(f, request))


# ---------------------------------------------------------------------------
# Dashboard (A02)
# ---------------------------------------------------------------------------


class DashboardView(AdminView):
    """GET /admin/dashboard ?from=&to= (default: last 30 days)"""

    def get(self, request):
        now = timezone.now()
        q = request.query_params
        start = date_bound(q.get("from")) if q.get("from") else now - timedelta(days=30)
        end = date_bound(q.get("to"), end=True) if q.get("to") else now
        today = timezone.localdate()

        live_users = User.objects.filter(deleted_at__isnull=True)
        on_shelf = ApplicationItem.objects.filter(decision=Decision.APPROVED, application__status=S.RECEIVED)
        by_status = dict(Application.objects.values_list("status").annotate(n=Count("id")))

        in_period = Application.objects.filter(created_at__gte=start, created_at__lte=end)
        created = dict(in_period.values_list("id", "created_at"))
        first_decision = {}  # application id → (decided_at, approved|rejected), earliest decision only
        for e in ApplicationEvent.objects.filter(application_id__in=created, to_status__in=[S.APPROVED, S.REJECTED]).order_by("created_at"):
            first_decision.setdefault(e.application_id, (e.created_at, e.to_status))
        hours = [(t - created[a]).total_seconds() / 3600 for a, (t, _) in first_decision.items()]
        approved = sum(1 for _, st in first_decision.values() if st == S.APPROVED)
        decided = first_decision

        data = {
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "updated_at": now.isoformat(),
            "attention": {
                "legal_pending": UmkmProfile.objects.filter(legal_verifications__status=VerificationStatus.PENDING).distinct().count(),
                "applications_waiting_over_48h": Application.objects.filter(status=S.SUBMITTED, created_at__lt=now - timedelta(hours=48)).count(),
                "deliveries_today": Application.objects.filter(status__in=[S.APPROVED, S.IN_DELIVERY], delivery_date=today).count(),
                "emails_failed_24h": Notification.objects.filter(email_status=EmailStatus.FAILED, created_at__gte=now - timedelta(hours=24)).count(),
            },
            "users": {
                "total": live_users.count(),
                "store": StoreProfile.objects.filter(user__deleted_at__isnull=True).count(),
                "umkm": UmkmProfile.objects.filter(user__deleted_at__isnull=True).count(),
                "invited": live_users.filter(status=AccountStatus.INVITED).count(),
                "suspended": live_users.filter(status=AccountStatus.SUSPENDED).count(),
            },
            "opportunities": {s: Opportunity.objects.filter(status=s).count() for s in OpportunityStatus.values},
            "products": {"active": Product.objects.filter(is_active=True).count(), "archived": Product.objects.filter(is_active=False).count()},
            "stock": {
                "items_on_shelf": on_shelf.count(),
                "out_of_stock": on_shelf.filter(stock_status=StockStatus.OUT_OF_STOCK).count(),
            },
            "applications": {
                "by_status": {s: by_status.get(s, 0) for s in S.values},
                "created_in_period": in_period.count(),
                "approval_rate": round(100 * approved / len(decided)) if decided else None,
                "median_hours_to_review": round(statistics.median(hours), 1) if hours else None,
            },
            "recent_activity": recent_activity(),
        }
        return ok(data)


def recent_activity(limit=20):
    rows = []
    for e in ApplicationEvent.objects.select_related("application__umkm", "application__opportunity__store").order_by("-created_at")[:limit]:
        a = e.application
        rows.append({
            "kind": "application",
            "label": f"{a.umkm.business_name} → {a.opportunity.store.name}: {e.label.lower()}",
            "target": {"type": "application", "id": str(a.id)},
            "created_at": e.created_at,
        })
    for u in User.objects.filter(deleted_at__isnull=True).order_by("-created_at")[:limit]:
        verb = "diundang ke pilot" if u.status == AccountStatus.INVITED else "mendaftar"
        rows.append({"kind": "user", "label": f"{u.name or u.email} {verb}", "target": {"type": "user", "id": str(u.id)}, "created_at": u.created_at})
    for o in Opportunity.objects.select_related("store").order_by("-created_at")[:limit]:
        rows.append({"kind": "opportunity", "label": f"{o.store.name} memasang peluang {o.title}", "target": {"type": "opportunity", "id": str(o.id)}, "created_at": o.created_at})
    for p in Product.objects.select_related("umkm").order_by("-created_at")[:limit]:
        rows.append({"kind": "product", "label": f"{p.name} ditambahkan oleh {p.umkm.business_name}", "target": {"type": "product", "id": str(p.id)}, "created_at": p.created_at})
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows[:limit]]


# ---------------------------------------------------------------------------
# Notifications (A21)
# ---------------------------------------------------------------------------


class NotificationLogView(AdminView):
    """GET /admin/notifications ?user_id=&type=&email_status=&created_after="""

    def get(self, request):
        qs = Notification.objects.select_related("user")
        q = request.query_params
        if q.get("user_id"):
            qs = qs.filter(user_id=q["user_id"])
        if parse_csv(q.get("type")):
            qs = qs.filter(type__in=parse_csv(q["type"]))
        if parse_csv(q.get("email_status")):
            qs = qs.filter(email_status__in=parse_csv(q["email_status"]))
        if q.get("created_after"):
            qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
        qs = order(qs, request, {"created_at": "created_at"}, "-created_at")
        return self.paged(qs, admin_notification_out)


class ResendEmailView(AdminView):
    """POST /admin/notifications/{id}/resend-email"""

    def post(self, request, pk):
        n = Notification.objects.filter(pk=pk).select_related("user").first()
        if n is None:
            raise NotFound()
        if n.email_status != EmailStatus.FAILED:
            raise Conflict("Hanya email yang gagal yang bisa dikirim ulang.", {"email_status": n.email_status})
        n.email_status = EmailStatus.PENDING
        n.save(update_fields=["email_status"])
        audit(request, AuditAction.RESEND_EMAIL, TargetType.NOTIFICATION, n.id, f"{n.title} → {n.user.name or n.user.email}", "")
        deliver_email(n)
        return ok(admin_notification_out(n))


class BroadcastInput(serializers.Serializer):
    title = serializers.CharField(max_length=80)
    body = serializers.CharField(max_length=500)
    target = serializers.DictField()
    send_email = serializers.BooleanField(required=False, default=True)

    def validate_target(self, v):
        roles = v.get("roles") or []
        user_ids = v.get("user_ids") or []
        if not isinstance(roles, list) or not set(roles) <= {"store", "umkm"} or not isinstance(user_ids, list):
            raise serializers.ValidationError("Target tidak valid.")
        if not roles and not user_ids:
            raise serializers.ValidationError("Pilih minimal satu target.")
        return {"roles": roles, "user_ids": [str(x) for x in user_ids]}


class BroadcastView(AdminView):
    """POST /admin/notifications/broadcast {title, body, target:{roles[], user_ids[]}, send_email}"""

    def post(self, request):
        s = BroadcastInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        cond = Q(pk__in=d["target"]["user_ids"])
        if "store" in d["target"]["roles"]:
            cond |= Q(store_profile__isnull=False, store_profile__deleted_at__isnull=True)
        if "umkm" in d["target"]["roles"]:
            cond |= Q(umkm_profile__isnull=False, umkm_profile__deleted_at__isnull=True)
        recipients = User.objects.filter(cond, deleted_at__isnull=True, status=AccountStatus.ACTIVE).distinct()
        count = 0
        for u in recipients:
            notify(u, NotificationType.ANNOUNCEMENT, d["title"], d["body"], {"deeplink": "/notifikasi"}, send_email=d["send_email"])
            count += 1
        audit(request, AuditAction.BROADCAST, TargetType.NOTIFICATION, "broadcast", d["title"], d["body"][:200],
              [{"field": "recipients_count", "before": None, "after": str(count)}])
        return ok({"recipients_count": count}, 201)


# ---------------------------------------------------------------------------
# Audit log (A22, read-only)
# ---------------------------------------------------------------------------


class AuditLogListView(AdminView):
    """GET /admin/audit-logs ?admin_id=&action=&target_type=&target_id=&created_after=&created_before="""

    def get(self, request):
        qs = AdminAuditLog.objects.all()
        q = request.query_params
        if q.get("admin_id"):
            qs = qs.filter(admin_id=q["admin_id"])
        for param in ("action", "target_type"):
            if parse_csv(q.get(param)):
                qs = qs.filter(**{f"{param}__in": parse_csv(q[param])})
        if q.get("target_id"):
            qs = qs.filter(target_id=q["target_id"])
        if q.get("created_after"):
            qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
        if q.get("created_before"):
            qs = qs.filter(created_at__lte=date_bound(q["created_before"], end=True))
        qs = order(qs, request, {"created_at": "created_at"}, "-created_at")
        return self.paged(qs, audit_out)


class AuditLogDetailView(AdminView):
    """GET /admin/audit-logs/{id}"""

    def get(self, request, pk):
        log = AdminAuditLog.objects.filter(pk=pk).first()
        if log is None:
            raise NotFound()
        return ok(audit_out(log))


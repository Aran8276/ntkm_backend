from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_date

from ..exceptions import BadRequest, Conflict, HasActiveRelations
from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    Application,
    ApplicationItem,
    ApplicationStatus as S,
    AuditAction,
    Decision,
    NotificationType,
    Opportunity,
    OpportunityStatus,
    Product,
    StoreProfile,
    TargetType,
    UmkmProfile,
)
from ..permissions import require_superadmin
from ..serializers import OpportunityInput, ProductInput
from ..services.audit import audit, diff
from ..services.notify import notify
from ..services.profiles import save_product
from ..api.opportunities import apply_opportunity_data
from .base import AdminView, base_qs, get_any, notify_from, ok, order, parse_bool, parse_csv, reason_from
from .presenters import admin_opportunity_out, admin_product_out

OPP_FIELDS = ("title", "description", "requirements", "quota", "max_products_per_umkm", "commission", "receiving_schedule", "valid_until", "status", "store_id")
PRODUCT_FIELDS = ("name", "category_id", "description", "tags", "base_price", "suggested_retail_price", "shelf_life_days", "expiry_date", "is_active", "umkm_id")


def snap(obj, fields, extra=None):
    data = {f: getattr(obj, f) for f in fields}
    data.update(extra or {})
    return data


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


def opportunity_qs(request):
    return base_qs(Opportunity, request).select_related("store__user", "store__photo", "created_by", "deleted_by").prefetch_related("categories")


def opp_active_apps(opp):
    return list(Application.objects.filter(opportunity=opp, status__in=ACTIVE_APPLICATION_STATUSES).values_list("id", flat=True))


def notify_store(opp, request, title, body):
    if notify_from(request):
        notify(opp.store.user, NotificationType.ADMIN_ACTION, title, body, {"deeplink": f"/toko/peluang/{opp.id}", "role": "store"})


class OpportunityListView(AdminView):
    """GET|POST /admin/opportunities"""

    def get(self, request):
        qs = opportunity_qs(request)
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(Q(title__icontains=t) | Q(store__name__icontains=t))
        if q.get("store_id"):
            qs = qs.filter(store_id=q["store_id"])
        if parse_csv(q.get("status")):
            qs = qs.filter(status__in=parse_csv(q["status"]))
        if parse_csv(q.get("category")):
            qs = qs.filter(categories__slug__in=parse_csv(q["category"])).distinct()
        expired = parse_bool(q.get("expired"))
        if expired is not None:
            today = timezone.localdate()
            qs = qs.filter(valid_until__lt=today) if expired else qs.filter(valid_until__gte=today)
        qs = order(qs, request, {"title": "title", "valid_until": "valid_until", "created_at": "created_at", "updated_at": "updated_at"}, "-created_at")
        return self.paged(qs, lambda o: admin_opportunity_out(o, request))

    def post(self, request):
        store = StoreProfile.objects.filter(pk=request.data.get("store_id")).first() if request.data.get("store_id") else None
        if store is None:
            raise BadRequest("Pilih toko.", {"store_id": "Pilih toko."})
        s = OpportunityInput(data=request.data, operating_hours=store.operating_hours)
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            opp = Opportunity(store=store, created_by=request.user)
            apply_opportunity_data(opp, s.validated_data)
            opp.save()
            opp.categories.set(s.validated_data["category_ids"])
        reason = reason_from(request, required=False)
        audit(request, AuditAction.CREATE, TargetType.OPPORTUNITY, opp.id, opp.title, reason)
        notify_store(opp, request, "Peluang dibuat admin", f"Admin memasang peluang “{opp.title}” atas nama toko Anda.")
        return ok(admin_opportunity_out(Opportunity.all_objects.get(pk=opp.pk), request), 201)


class OpportunityDetailView(AdminView):
    """GET|PATCH|DELETE /admin/opportunities/{id}  (DELETE ?hard=true)"""

    def get(self, request, pk):
        return ok(admin_opportunity_out(get_any(Opportunity, pk), request))

    def patch(self, request, pk):
        opp = get_any(Opportunity, pk)
        store = opp.store
        new_store_id = request.data.get("store_id")
        if new_store_id and str(new_store_id) != str(opp.store_id):
            if opp.applications.exists():
                raise Conflict("Toko tidak bisa diganti karena sudah ada pengajuan.", {"store_id": "Toko tidak bisa diganti karena sudah ada pengajuan."})
            store = StoreProfile.objects.filter(pk=new_store_id).first()
            if store is None:
                raise BadRequest("Toko tidak ditemukan.", {"store_id": "Toko tidak ditemukan."})
        s = OpportunityInput(opp, data=request.data, partial=True, operating_hours=store.operating_hours)
        s.is_valid(raise_exception=True)
        before = snap(opp, OPP_FIELDS, {"category_ids": sorted(str(c.id) for c in opp.categories.all())})
        with transaction.atomic():
            opp.store = store
            apply_opportunity_data(opp, s.validated_data)
            opp.save()
            if "category_ids" in s.validated_data:
                opp.categories.set(s.validated_data["category_ids"])
        after = snap(opp, OPP_FIELDS, {"category_ids": sorted(str(c.id) for c in opp.categories.all())})
        reason = reason_from(request, required=False)
        changes = diff(before, after)
        audit(request, AuditAction.UPDATE, TargetType.OPPORTUNITY, opp.id, opp.title, reason, changes)
        if changes:
            notify_store(opp, request, "Peluang diperbarui admin", f"Admin memperbarui peluang “{opp.title}”." + (f" Alasan: {reason}" if reason else ""))
        return ok(admin_opportunity_out(get_any(Opportunity, pk), request))

    def delete(self, request, pk):
        opp = get_any(Opportunity, pk)
        reason = reason_from(request)
        if parse_bool(request.query_params.get("hard")):
            require_superadmin(request.user)
            if opp.applications.exists():
                raise HasActiveRelations("Peluang ini pernah punya pengajuan sehingga tidak bisa dihapus permanen.",
                                         {"application_ids": [str(i) for i in opp.applications.values_list("id", flat=True)]})
            audit(request, AuditAction.HARD_DELETE, TargetType.OPPORTUNITY, opp.id, opp.title, reason)
            opp.delete()
            return ok({"deleted": True, "hard": True})
        if opp.deleted_at:
            raise Conflict("Peluang sudah dihapus.")
        active = opp_active_apps(opp)
        if active:
            raise HasActiveRelations("Peluang masih punya pengajuan aktif.", {"application_ids": [str(i) for i in active]})
        opp.soft_delete(request.user)
        audit(request, AuditAction.DELETE, TargetType.OPPORTUNITY, opp.id, opp.title, reason)
        notify_store(opp, request, "Peluang dihapus admin", f"Peluang “{opp.title}” dihapus admin. Alasan: {reason}")
        return ok(admin_opportunity_out(opp, request))


class OpportunityCloseView(AdminView):
    """POST /admin/opportunities/{id}/close"""

    def post(self, request, pk):
        opp = get_any(Opportunity, pk, Opportunity.objects.all())
        reason = reason_from(request)
        if opp.status == OpportunityStatus.CLOSED:
            raise Conflict("Peluang sudah ditutup.", {"allowed": ["reopen"]})
        prev = opp.status
        opp.status = OpportunityStatus.CLOSED
        opp.save(update_fields=["status", "updated_at"])
        audit(request, AuditAction.CLOSE, TargetType.OPPORTUNITY, opp.id, opp.title, reason, [{"field": "status", "before": prev, "after": "closed"}])
        notify_store(opp, request, "Peluang ditutup admin", f"Peluang “{opp.title}” ditutup admin. Alasan: {reason}")
        return ok(admin_opportunity_out(opp, request))


class OpportunityReopenView(AdminView):
    """POST /admin/opportunities/{id}/reopen {valid_until?}"""

    def post(self, request, pk):
        opp = get_any(Opportunity, pk, Opportunity.objects.all())
        reason = reason_from(request)
        if opp.status != OpportunityStatus.CLOSED:
            raise Conflict("Peluang tidak sedang ditutup.", {"allowed": ["close"]})
        today = timezone.localdate()
        raw = request.data.get("valid_until")
        valid_until = parse_date(str(raw)) if raw else None
        if raw and valid_until is None:
            raise BadRequest("Tanggal tidak valid.", {"valid_until": "Tanggal tidak valid."})
        if valid_until is None and opp.valid_until < today:
            raise BadRequest("Masa berlaku sudah lewat; isi tanggal baru.", {"valid_until": "Masa berlaku sudah lewat; isi tanggal baru."})
        if valid_until is not None and valid_until < today:
            raise BadRequest("Tanggal tidak boleh sebelum hari ini.", {"valid_until": "Tanggal tidak boleh sebelum hari ini."})
        before = {"status": opp.status, "valid_until": opp.valid_until}
        if valid_until:
            opp.valid_until = valid_until
        opp.status = OpportunityStatus.OPEN
        opp.sync_status_with_quota()
        opp.save(update_fields=["status", "valid_until", "updated_at"])
        audit(request, AuditAction.REOPEN, TargetType.OPPORTUNITY, opp.id, opp.title, reason,
              diff(before, {"status": opp.status, "valid_until": opp.valid_until}))
        notify_store(opp, request, "Peluang dibuka ulang admin", f"Peluang “{opp.title}” dibuka ulang admin.")
        return ok(admin_opportunity_out(opp, request))


class OpportunityRestoreView(AdminView):
    """POST /admin/opportunities/{id}/restore"""

    def post(self, request, pk):
        opp = get_any(Opportunity, pk)
        reason = reason_from(request)
        if not opp.deleted_at:
            raise Conflict("Peluang tidak dalam keadaan terhapus.")
        opp.restore()
        audit(request, AuditAction.RESTORE, TargetType.OPPORTUNITY, opp.id, opp.title, reason)
        return ok(admin_opportunity_out(opp, request))


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def product_qs(request):
    return base_qs(Product, request).select_related("umkm__user", "category", "created_by", "deleted_by").prefetch_related("photos__file")


def notify_umkm(product, request, title, body):
    if notify_from(request):
        notify(product.umkm.user, NotificationType.ADMIN_ACTION, title, body, {"deeplink": f"/umkm/produk/{product.id}/ubah", "role": "umkm"})


class ProductListView(AdminView):
    """GET|POST /admin/products"""

    def get(self, request):
        qs = product_qs(request)
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(Q(name__icontains=t) | Q(tags__icontains=t) | Q(umkm__business_name__icontains=t))
        if q.get("umkm_id"):
            qs = qs.filter(umkm_id=q["umkm_id"])
        if parse_csv(q.get("category")):
            qs = qs.filter(category__slug__in=parse_csv(q["category"]))
        active = parse_bool(q.get("is_active"))
        if active is not None:
            qs = qs.filter(is_active=active)
        placed = parse_bool(q.get("has_placements"))
        if placed is not None:
            sub = ApplicationItem.objects.filter(product=OuterRef("pk"), decision=Decision.APPROVED, application__status=S.RECEIVED)
            qs = qs.filter(Exists(sub)) if placed else qs.exclude(Exists(sub))
        qs = order(qs, request, {"name": "name", "base_price": "base_price", "created_at": "created_at", "updated_at": "updated_at"}, "-created_at")
        return self.paged(qs, lambda p: admin_product_out(p, request))

    def post(self, request):
        umkm = UmkmProfile.objects.filter(pk=request.data.get("umkm_id")).first() if request.data.get("umkm_id") else None
        if umkm is None:
            raise BadRequest("Pilih UMKM.", {"umkm_id": "Pilih UMKM."})
        s = ProductInput(data=request.data)
        s.is_valid(raise_exception=True)
        product = save_product(umkm, s.validated_data, actor=request.user, uploader=request.user)
        audit(request, AuditAction.CREATE, TargetType.PRODUCT, product.id, product.name, reason_from(request, required=False))
        notify_umkm(product, request, "Produk ditambahkan admin", f"Admin menambahkan produk {product.name} untuk usaha Anda.")
        return ok(admin_product_out(Product.all_objects.get(pk=product.pk), request, detail=True), 201)


class ProductDetailView(AdminView):
    """GET|PATCH|DELETE /admin/products/{id}  (DELETE ?hard=true)"""

    def get(self, request, pk):
        return ok(admin_product_out(get_any(Product, pk), request, detail=True))

    def patch(self, request, pk):
        product = get_any(Product, pk)
        umkm = product.umkm
        new_umkm = request.data.get("umkm_id")
        if new_umkm and str(new_umkm) != str(product.umkm_id):
            if product.application_items.exists():
                raise Conflict("UMKM tidak bisa diganti karena produk sudah pernah diajukan.",
                               {"umkm_id": "UMKM tidak bisa diganti karena produk sudah pernah diajukan."})
            umkm = UmkmProfile.objects.filter(pk=new_umkm).first()
            if umkm is None:
                raise BadRequest("UMKM tidak ditemukan.", {"umkm_id": "UMKM tidak ditemukan."})
        s = ProductInput(product, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        before = snap(product, PRODUCT_FIELDS, {"photo_file_ids": [str(p.file_id) for p in product.photos.all()]})
        with transaction.atomic():
            product.umkm = umkm
            save_product(umkm, s.validated_data, product=product, uploader=request.user)
        product = get_any(Product, pk)
        after = snap(product, PRODUCT_FIELDS, {"photo_file_ids": [str(p.file_id) for p in product.photos.all()]})
        changes = diff(before, after)
        reason = reason_from(request, required=False)
        audit(request, AuditAction.UPDATE, TargetType.PRODUCT, product.id, product.name, reason, changes)
        if changes:
            if [c["field"] for c in changes] == ["is_active"]:
                title = "Produk diarsipkan admin" if not product.is_active else "Produk diaktifkan admin"
            else:
                title = "Produk diperbarui admin"
            notify_umkm(product, request, title, f"{title.replace(' admin', '')}: {product.name}." + (f" Alasan: {reason}" if reason else ""))
        return ok(admin_product_out(product, request, detail=True))

    def delete(self, request, pk):
        product = get_any(Product, pk)
        reason = reason_from(request)
        if parse_bool(request.query_params.get("hard")):
            require_superadmin(request.user)
            if product.application_items.exists():
                raise HasActiveRelations("Produk ini pernah diajukan sehingga tidak bisa dihapus permanen.",
                                         {"application_ids": [str(i) for i in product.application_items.values_list("application_id", flat=True).distinct()]})
            audit(request, AuditAction.HARD_DELETE, TargetType.PRODUCT, product.id, product.name, reason)
            product.delete()
            return ok({"deleted": True, "hard": True})
        if product.deleted_at:
            raise Conflict("Produk sudah dihapus.")
        active = (
            ApplicationItem.objects.filter(product=product, application__status__in=ACTIVE_APPLICATION_STATUSES)
            .exclude(decision=Decision.REJECTED).values_list("application_id", flat=True).distinct()
        )
        if active:
            raise HasActiveRelations("Produk masih ada di pengajuan aktif.", {"application_ids": [str(i) for i in active]})
        product.soft_delete(request.user)
        audit(request, AuditAction.DELETE, TargetType.PRODUCT, product.id, product.name, reason)
        notify_umkm(product, request, "Produk dihapus admin", f"Produk {product.name} dihapus admin. Alasan: {reason}")
        return ok(admin_product_out(product, request, detail=True))


class ProductRestoreView(AdminView):
    """POST /admin/products/{id}/restore"""

    def post(self, request, pk):
        product = get_any(Product, pk)
        reason = reason_from(request)
        if not product.deleted_at:
            raise Conflict("Produk tidak dalam keadaan terhapus.")
        product.restore()
        audit(request, AuditAction.RESTORE, TargetType.PRODUCT, product.id, product.name, reason)
        return ok(admin_product_out(product, request, detail=True))


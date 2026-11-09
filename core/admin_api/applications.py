from django.db import transaction
from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.utils.dateparse import parse_date
from rest_framework import serializers

from ..exceptions import BadRequest, Conflict, NotFound
from ..models import (
    Application,
    ApplicationEvent,
    ApplicationItem,
    ApplicationStatus as S,
    AuditAction,
    Decision,
    NotificationType,
    Opportunity,
    Product,
    StockMode,
    StockStatus,
    TargetType,
    UmkmProfile,
)
from ..permissions import require_superadmin
from ..serializers import ReviewInput
from ..services import workflow
from ..services.audit import audit, diff
from ..services.notify import application_link, notify
from .base import AdminView, date_bound, notify_from, ok, order, parse_bool, parse_csv, reason_from
from .presenters import admin_application_list_out, admin_application_out, admin_stock_out


def app_qs():
    return Application.objects.select_related(
        "opportunity__store__user", "opportunity__store__photo", "umkm__user", "umkm__logo", "created_by"
    ).prefetch_related("items__photo", "events", "umkm__legal_verifications")


def label(a):
    return f"{a.umkm.business_name} → {a.opportunity.store.name}"


def load(pk):
    a = app_qs().filter(pk=pk).first()
    if a is None:
        raise NotFound()
    return a


def notify_parties(a, request, title, body):
    if notify_from(request):
        notify(a.opportunity.store.user, NotificationType.ADMIN_ACTION, title, body, application_link(a, "store"))
        notify(a.umkm.user, NotificationType.ADMIN_ACTION, title, body, application_link(a, "umkm"))


class CreateInput(serializers.Serializer):
    umkm_id = serializers.UUIDField()
    opportunity_id = serializers.UUIDField()
    product_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, error_messages={"min_length": "Pilih minimal 1 produk."})
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")
    skip_requirement_check = serializers.BooleanField(required=False, default=False)


class ApplicationListView(AdminView):
    """GET|POST /admin/applications"""

    def get(self, request):
        qs = app_qs()
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(Q(opportunity__store__name__icontains=t) | Q(umkm__business_name__icontains=t) | Q(opportunity__title__icontains=t))
        if parse_csv(q.get("status")):
            qs = qs.filter(status__in=parse_csv(q["status"]))
        for param, field in (("store_id", "opportunity__store_id"), ("umkm_id", "umkm_id"), ("opportunity_id", "opportunity_id")):
            if q.get(param):
                qs = qs.filter(**{field: q[param]})
        if q.get("created_after"):
            qs = qs.filter(created_at__gte=date_bound(q["created_after"]))
        if q.get("created_before"):
            qs = qs.filter(created_at__lte=date_bound(q["created_before"], end=True))
        if q.get("delivery_date"):
            d = parse_date(q["delivery_date"])
            if d is None:
                raise BadRequest("Tanggal antar tidak valid.", {"delivery_date": "Tanggal tidak valid."})
            qs = qs.filter(delivery_date=d)
        override = parse_bool(q.get("has_override"))
        if override is not None:
            sub = ApplicationEvent.objects.filter(application=OuterRef("pk"), is_admin_override=True)
            qs = qs.filter(Exists(sub)) if override else qs.exclude(Exists(sub))
        if (q.get("ordering") or "").lstrip("-") == "delivery_slot_start":
            qs = qs.annotate(_no_slot=Case(When(delivery_date__isnull=True, then=Value(1)), default=Value(0), output_field=IntegerField()))
            desc = q["ordering"].startswith("-")
            qs = qs.order_by("_no_slot", *(("-delivery_date", "-delivery_start") if desc else ("delivery_date", "delivery_start")), "pk")
        else:
            qs = order(qs, request, {"created_at": "created_at", "updated_at": "updated_at"}, "-created_at")
        return self.paged(qs, admin_application_list_out)

    def post(self, request):
        reason = reason_from(request)
        s = CreateInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        umkm = UmkmProfile.objects.filter(pk=d["umkm_id"]).select_related("user").first()
        if umkm is None:
            raise BadRequest("UMKM tidak ditemukan.", {"umkm_id": "UMKM tidak ditemukan."})
        opp = Opportunity.objects.filter(pk=d["opportunity_id"]).first()
        if opp is None:
            raise BadRequest("Peluang tidak ditemukan.", {"opportunity_id": "Peluang tidak ditemukan."})
        ids = d["product_ids"]
        products = list(Product.objects.filter(pk__in=ids, umkm=umkm).select_related("category"))
        if len(products) != len(set(ids)):
            raise BadRequest("Produk bukan milik UMKM ini.", {"product_ids": "Produk tidak ditemukan atau bukan milik UMKM ini."})
        products.sort(key=lambda p: ids.index(p.id))
        a = workflow.create_application(
            actor=request.user, actor_role="admin", umkm=umkm, opportunity=opp, products=products,
            note=d["note"], skip_requirement_check=d["skip_requirement_check"],
        )
        ApplicationEvent.objects.filter(application=a).update(admin_reason=reason)
        audit(request, AuditAction.CREATE, TargetType.APPLICATION, a.id, label(a), reason,
              [{"field": "skip_requirement_check", "before": None, "after": "true" if d["skip_requirement_check"] else "false"}])
        if notify_from(request):
            notify(umkm.user, NotificationType.ADMIN_ACTION, "Pengajuan dibuat admin atas nama Anda",
                   f"Admin mengajukan {len(products)} produk ke peluang {opp.title}.", application_link(a, "umkm"))
        return ok(admin_application_out(load(a.pk), request), 201)


class PatchItemInput(serializers.Serializer):
    item_id = serializers.IntegerField()
    approved_quantity = serializers.IntegerField(min_value=1, required=False)
    sell_price = serializers.IntegerField(min_value=1, required=False)
    received_quantity = serializers.IntegerField(min_value=0, required=False)


class PatchInput(serializers.Serializer):
    delivery_slot = serializers.DictField(required=False)
    items = PatchItemInput(many=True, required=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)
    review_note = serializers.CharField(required=False, allow_blank=True, max_length=300)
    stock_mode = serializers.ChoiceField(choices=StockMode.choices, required=False)


class ApplicationDetailView(AdminView):
    """GET|PATCH|DELETE /admin/applications/{id}  (DELETE requires ?hard=true)"""

    def get(self, request, pk):
        return ok(admin_application_out(load(pk), request))

    def patch(self, request, pk):
        a = load(pk)
        s = PatchInput(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        # Slot and number corrections affect both parties: reason is required.
        reason = reason_from(request, required=("delivery_slot" in d or "items" in d))
        changes = []
        with transaction.atomic():
            a = Application.objects.select_for_update().get(pk=a.pk)
            if "delivery_slot" in d:
                if a.status not in (S.APPROVED, S.IN_DELIVERY):
                    raise Conflict("Slot hanya bisa diubah saat disetujui atau dalam pengiriman.", {"allowed": [S.APPROVED, S.IN_DELIVERY]})
                before = f"{a.delivery_date} {a.delivery_start}-{a.delivery_end}"
                a.delivery_date, a.delivery_start, a.delivery_end = workflow.validate_slot(a, d["delivery_slot"], allow_today=True)
                changes.append({"field": "delivery_slot", "before": before, "after": f"{a.delivery_date} {a.delivery_start}-{a.delivery_end}"})
            if "items" in d:
                if a.status not in (S.APPROVED, S.IN_DELIVERY, S.RECEIVED, S.COMPLETED):
                    raise Conflict("Angka produk hanya bisa dikoreksi setelah disetujui.")
                items = {i.id: i for i in a.items.filter(decision=Decision.APPROVED)}
                for idx, row in enumerate(d["items"]):
                    item = items.get(row["item_id"])
                    if item is None:
                        raise BadRequest("Item tidak dikenal atau tidak disetujui.", {f"items.{idx}.item_id": "Item tidak dikenal atau tidak disetujui."})
                    if "received_quantity" in row and a.status not in (S.RECEIVED, S.COMPLETED):
                        raise BadRequest("Jumlah diterima hanya bisa dikoreksi setelah barang diterima.",
                                         {f"items.{idx}.received_quantity": "Belum ada penerimaan barang."})
                    old = {"approved_quantity": item.approved_quantity, "sell_price": item.sell_price, "received_quantity": item.received_quantity}
                    if "approved_quantity" in row:
                        item.approved_quantity = row["approved_quantity"]
                    if "sell_price" in row:
                        if row["sell_price"] < item.base_price:
                            raise BadRequest("Harga jual di bawah harga setor.", {f"items.{idx}.sell_price": f"Harga jual minimal {item.base_price}."})
                        item.sell_price = row["sell_price"]
                        item.commission_per_unit, item.umkm_income_per_unit = workflow.calc_commission(item.sell_price, a.commission)
                    if "received_quantity" in row:
                        if row["received_quantity"] > (item.approved_quantity or 0):
                            raise BadRequest("Jumlah diterima melebihi jumlah disetujui.", {f"items.{idx}.received_quantity": "Melebihi jumlah disetujui."})
                        item.received_quantity = row["received_quantity"]
                        if item.stock_quantity is not None and item.stock_quantity > item.received_quantity:
                            item.stock_quantity = item.received_quantity
                    item.save()
                    new = {"approved_quantity": item.approved_quantity, "sell_price": item.sell_price, "received_quantity": item.received_quantity}
                    changes += [{**c, "field": f"items[{item.id}].{c['field']}"} for c in diff(old, new)]
            for field in ("note", "review_note"):
                if field in d and d[field] != getattr(a, field):
                    changes.append({"field": field, "before": getattr(a, field), "after": d[field]})
                    setattr(a, field, d[field])
            if "stock_mode" in d and d["stock_mode"] != a.stock_mode:
                if a.status not in (S.APPROVED, S.IN_DELIVERY, S.RECEIVED):
                    raise Conflict("Mode stok hanya bisa diubah sebelum kerja sama selesai.")
                changes.append({"field": "stock_mode", "before": a.stock_mode, "after": d["stock_mode"]})
                a.stock_mode = d["stock_mode"]
            a.save()
            if changes:
                workflow.add_event(a, a.status, a.status, request.user, "admin", label="Data pengajuan dikoreksi admin",
                                   override=True, admin_reason=reason)
        a = load(pk)
        if changes:
            audit(request, AuditAction.UPDATE, TargetType.APPLICATION, a.id, label(a), reason, changes)
            notify_parties(a, request, "Pengajuan dikoreksi admin", f"Admin memperbarui data kerja sama {label(a)}." + (f" Alasan: {reason}" if reason else ""))
        return ok(admin_application_out(a, request))

    def delete(self, request, pk):
        a = load(pk)
        reason = reason_from(request)
        if not parse_bool(request.query_params.get("hard")):
            raise BadRequest("Pengajuan hanya bisa dihapus permanen (?hard=true).", {"hard": "Gunakan ?hard=true."})
        require_superadmin(request.user)
        text = label(a)
        with transaction.atomic():
            if a.status in workflow.QUOTA_HOLDING:
                workflow._release_quota(a.opportunity_id)
            from ..models import Notification

            Notification.objects.filter(data__application_id=str(a.id)).delete()
            audit(request, AuditAction.HARD_DELETE, TargetType.APPLICATION, a.id, text, reason)
            a.delete()
        return ok({"deleted": True, "hard": True})


class ApplicationReviewView(AdminView):
    """POST /admin/applications/{id}/review — same body as the store review + admin_reason, notify_user."""

    def post(self, request, pk):
        a = load(pk)
        reason = reason_from(request)
        s = ReviewInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        slot = d.get("delivery_slot")
        if slot:
            slot = {"date": slot["date"].isoformat(), "start": slot["start"], "end": slot["end"]}
        workflow.review(a, actor=request.user, actor_role="admin", items=[dict(i) for i in d["items"]],
                        delivery_slot=slot, stock_mode=d["stock_mode"], note=d["note"], admin_reason=reason)
        a = load(pk)
        audit(request, AuditAction.FORCE_STATUS, TargetType.APPLICATION, a.id, label(a), reason,
              [{"field": "status", "before": S.SUBMITTED, "after": a.status}])
        return ok(admin_application_out(a, request))


class ForceStatusInput(serializers.Serializer):
    to_status = serializers.ChoiceField(choices=S.choices)
    received_items = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class ApplicationForceStatusView(AdminView):
    """POST /admin/applications/{id}/force-status {to_status, received_items?, admin_reason, notify_user}"""

    def post(self, request, pk):
        a = load(pk)
        reason = reason_from(request)
        s = ForceStatusInput(data=request.data)
        s.is_valid(raise_exception=True)
        prev = a.status
        a = workflow.force_status(a, admin=request.user, to_status=s.validated_data["to_status"], reason=reason,
                                  received_items=s.validated_data["received_items"], notify_user=notify_from(request))
        audit(request, AuditAction.FORCE_STATUS, TargetType.APPLICATION, a.id, label(a), reason,
              [{"field": "status", "before": prev, "after": a.status}])
        return ok(admin_application_out(load(pk), request))


class AdminItemStockView(AdminView):
    """PATCH /admin/applications/{id}/items/{item_id}/stock {stock_quantity|stock_status, admin_reason, notify_user}"""

    def patch(self, request, pk, item_id):
        a = load(pk)
        reason = reason_from(request)
        item = a.items.filter(pk=item_id).first()
        if item is None:
            raise NotFound()
        before = {"stock_quantity": item.stock_quantity, "stock_status": item.stock_status}
        a, item = workflow.update_stock(a, item_id, actor=request.user, data=request.data, notify_user=notify_from(request), admin_reason=reason)
        audit(request, AuditAction.STOCK_CORRECTION, TargetType.APPLICATION, a.id, label(a), reason,
              [{**c, "field": f"items[{item.id}].{c['field']}"} for c in diff(before, {"stock_quantity": item.stock_quantity, "stock_status": item.stock_status})])
        item = ApplicationItem.objects.select_related("application__opportunity__store", "application__umkm", "photo", "stock_updated_by").get(pk=item.pk)
        return ok(admin_stock_out(item, request))


class AdminStockView(AdminView):
    """GET /admin/stock ?store_id=&umkm_id=&stock_status="""

    def get(self, request):
        qs = ApplicationItem.objects.filter(decision=Decision.APPROVED, application__status=S.RECEIVED).select_related(
            "application__opportunity__store", "application__umkm", "photo", "stock_updated_by"
        )
        q = request.query_params
        if q.get("store_id"):
            qs = qs.filter(application__opportunity__store_id=q["store_id"])
        if q.get("umkm_id"):
            qs = qs.filter(application__umkm_id=q["umkm_id"])
        if q.get("stock_status"):
            if q["stock_status"] not in StockStatus.values:
                raise BadRequest("Status stok tidak dikenal.", {"stock_status": "Pilih available atau out_of_stock."})
            qs = qs.filter(stock_status=q["stock_status"])
        qs = qs.order_by("-stock_updated_at", "id")
        response = self.paged(qs, lambda i: admin_stock_out(i, request))
        base = ApplicationItem.objects.filter(decision=Decision.APPROVED, application__status=S.RECEIVED)
        response.data["summary"] = {
            "items_on_shelf": base.count(),
            "out_of_stock": base.filter(stock_status=StockStatus.OUT_OF_STOCK).count(),
        }
        return response


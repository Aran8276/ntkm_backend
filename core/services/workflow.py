"""
Application (kerja sama) state machine.

    submitted ──review──▶ approved ──ship──▶ in_delivery ──receive──▶ received ──complete──▶ completed
        │                    │                                          │
        ├──review (all rejected)──▶ rejected                            │
        └──cancel──▶ cancelled ◀──cancel── approved                     │

Quota: an opportunity's ``quota`` counts approved UMKMs. Approving takes one
slot; cancelling (or reverting) an approved application gives it back.
All transitions lock the application row and raise 409 if the status moved.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from ..exceptions import BadRequest, Conflict
from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    Application,
    ApplicationEvent,
    ApplicationItem,
    ApplicationStatus as S,
    Decision,
    NotificationType as N,
    Opportunity,
    OpportunityStatus,
    StockMode,
    StockStatus,
)
from .formatting import WEEKDAY_KEY, fmt_slot, parse_hhmm, rupiah
from .legal import has_legal
from .matching import product_eligibility
from .notify import application_link, notify

EVENT_LABEL = {
    S.SUBMITTED: "Pengajuan dikirim",
    S.APPROVED: "Disetujui toko",
    S.IN_DELIVERY: "Barang dikirim",
    S.RECEIVED: "Barang diterima toko",
    S.COMPLETED: "Kerja sama selesai",
    S.REJECTED: "Ditolak toko",
    S.CANCELLED: "Pengajuan dibatalkan",
}

ACTIONS = {
    S.SUBMITTED: {"store": ["review"], "umkm": ["cancel"]},
    S.APPROVED: {"store": ["cancel"], "umkm": ["ship", "cancel"]},
    S.IN_DELIVERY: {"store": ["receive"], "umkm": []},
    S.RECEIVED: {"store": ["update_stock", "complete"], "umkm": ["complete"]},
    S.COMPLETED: {"store": [], "umkm": []},
    S.REJECTED: {"store": [], "umkm": []},
    S.CANCELLED: {"store": [], "umkm": []},
}

# Admin "force status" transitions and whether they give back a quota slot.
ADMIN_TRANSITIONS = {
    S.SUBMITTED: [S.CANCELLED],
    S.APPROVED: [S.SUBMITTED, S.CANCELLED],
    S.IN_DELIVERY: [S.APPROVED, S.RECEIVED, S.CANCELLED],
    S.RECEIVED: [S.IN_DELIVERY, S.COMPLETED, S.CANCELLED],
    S.COMPLETED: [S.RECEIVED],
    S.REJECTED: [S.SUBMITTED],
    S.CANCELLED: [S.SUBMITTED],
}
QUOTA_HOLDING = {S.APPROVED, S.IN_DELIVERY, S.RECEIVED, S.COMPLETED}


def available_actions(app, role):
    return list(ACTIONS.get(app.status, {}).get(role, []))


def calc_commission(sell_price, commission):
    if (commission or {}).get("type") == "fixed":
        c = int(commission.get("value") or 0)
    else:
        c = round(sell_price * float((commission or {}).get("value") or 0) / 100)
    return c, max(sell_price - c, 0)


def actor_name(user, role):
    if role == "store" and user.store:
        return user.store.name
    if role == "umkm" and user.umkm:
        return user.umkm.business_name
    return user.name or user.email


def add_event(app, from_status, to_status, actor, role, note="", label=None, override=False, admin_reason=""):
    return ApplicationEvent.objects.create(
        application=app,
        from_status=from_status,
        to_status=to_status,
        label=label or EVENT_LABEL[to_status],
        actor=actor,
        actor_name=actor_name(actor, role) if role != "admin" else (actor.name or actor.email),
        actor_role=role,
        note=note or "",
        is_admin_override=override,
        admin_reason=admin_reason or "",
    )


def _lock(app):
    return Application.objects.select_for_update().select_related("opportunity__store__user", "umkm__user").get(pk=app.pk)


def _expect(app, *statuses):
    if app.status not in statuses:
        raise Conflict("Status pengajuan sudah berubah. Muat ulang halaman.", {"status": app.status})


def _store_user(app):
    return app.opportunity.store.user


def _umkm_user(app):
    return app.umkm.user


def _take_quota(opp_id):
    opp = Opportunity.all_objects.select_for_update().get(pk=opp_id)
    if opp.quota_used >= opp.quota:
        raise Conflict(
            "Kuota peluang sudah penuh. Tambah kuota di peluang atau tolak pengajuan ini.",
            {"reason": "quota_full"},
        )
    opp.quota_used += 1
    opp.sync_status_with_quota()
    opp.save(update_fields=["quota_used", "status", "updated_at"])


def _release_quota(opp_id):
    opp = Opportunity.all_objects.select_for_update().get(pk=opp_id)
    opp.quota_used = max(opp.quota_used - 1, 0)
    opp.sync_status_with_quota()
    opp.save(update_fields=["quota_used", "status", "updated_at"])


def has_active_application(umkm, opportunity, exclude=None):
    qs = Application.objects.filter(umkm=umkm, opportunity=opportunity, status__in=ACTIVE_APPLICATION_STATUSES)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.exists()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_application(*, actor, actor_role, umkm, opportunity, products, note="", skip_requirement_check=False):
    """POST /applications (UMKM) and POST /admin/applications (admin on behalf of a UMKM)."""
    if len(note or "") > 300:
        raise BadRequest("Catatan maksimal 300 karakter.", {"note": "Maksimal 300 karakter."})
    if not products:
        raise BadRequest("Pilih minimal 1 produk.", {"product_ids": "Pilih minimal 1 produk."})
    if len({p.id for p in products}) != len(products):
        raise BadRequest("Produk tidak boleh duplikat.", {"product_ids": "Produk tidak boleh duplikat."})

    with transaction.atomic():
        opp = Opportunity.objects.select_for_update().select_related("store__user").get(pk=opportunity.pk)
        if opp.store.deleted_at or opp.status != OpportunityStatus.OPEN or opp.is_expired or opp.quota_remaining <= 0:
            raise Conflict("Peluang ini sudah penuh atau ditutup.", {"reason": "opportunity_unavailable"})
        if opp.store.user_id == umkm.user_id:
            raise BadRequest("Tidak bisa mengajukan ke peluang toko sendiri.", {"opportunity_id": "Tidak bisa mengajukan ke peluang toko sendiri."})
        if has_active_application(umkm, opp):
            raise Conflict("Anda sudah punya pengajuan aktif untuk peluang ini.", {"reason": "already_applied"})
        if len(products) > opp.max_products_per_umkm:
            msg = f"Maksimal {opp.max_products_per_umkm} produk per UMKM."
            raise BadRequest(msg, {"product_ids": msg})
        for p in products:
            if p.umkm_id != umkm.id or p.deleted_at or not p.is_active:
                raise BadRequest("Produk tidak ditemukan atau tidak aktif.", {"product_ids": "Produk tidak ditemukan atau tidak aktif."})

        if not skip_requirement_check:
            legal = has_legal(umkm)
            cat_ids = {c.id for c in opp.categories.all()}
            product_errors, legal_errors = [], []
            for p in products:
                ok, reasons, legal_reasons = product_eligibility(opp, p, legal, cat_ids)
                other = [r for r in reasons if r not in legal_reasons]
                if other:
                    product_errors.append(f"{p.name}: {', '.join(other)}.")
                legal_errors.extend(r for r in legal_reasons if r not in legal_errors)
            details = {}
            if product_errors:
                details["product_ids"] = " ".join(product_errors)
            if legal_errors:
                details["legal"] = "Toko ini mensyaratkan legalitas yang belum Anda miliki: " + ", ".join(
                    r.replace("Belum memiliki ", "") for r in legal_errors
                ) + "."
            if details:
                raise BadRequest(details.get("legal") or "Produk tidak memenuhi syarat peluang.", details)

        app = Application.objects.create(
            opportunity=opp,
            umkm=umkm,
            note=note or "",
            commission=opp.commission,
            created_by=actor,
        )
        for p in products:
            photo = p.photos.select_related("file").first()
            ApplicationItem.objects.create(
                application=app,
                product=p,
                product_name=p.name,
                category_name=p.category.name,
                base_price=p.base_price,
                suggested_retail_price=p.suggested_retail_price,
                shelf_life_days=p.shelf_life_days,
                expiry_date=p.expiry_date,
                photo=photo.file if photo else None,
            )
        if actor_role == "admin":
            add_event(app, None, S.SUBMITTED, actor, "admin", note, label="Pengajuan dibuat admin atas nama UMKM", override=True)
        else:
            add_event(app, None, S.SUBMITTED, actor, "umkm", note)

    notify(
        _store_user(app),
        N.APPLICATION_SUBMITTED,
        f"Pengajuan baru dari {umkm.business_name}",
        f"{umkm.business_name} mengajukan {len(products)} produk ke peluang {opp.title}.",
        application_link(app, "store"),
    )
    return app


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def upcoming_slots(schedule, start_day, days=14):
    out = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        for s in schedule or []:
            if s.get("day") == WEEKDAY_KEY[d.weekday()]:
                out.append((d, s.get("start"), s.get("end")))
    return out


def validate_slot(app, slot, allow_today=False):
    today = timezone.localdate()
    first = today if allow_today else today + timedelta(days=1)
    if not isinstance(slot, dict):
        raise BadRequest("Pilih slot antar.", {"delivery_slot": "Pilih slot antar."})
    try:
        from datetime import date

        d = date.fromisoformat(str(slot.get("date")))
    except ValueError:
        raise BadRequest("Tanggal antar tidak valid.", {"delivery_slot.date": "Tanggal antar tidak valid."})
    start, end = str(slot.get("start", ""))[:5], str(slot.get("end", ""))[:5]
    if not (first <= d <= first + timedelta(days=13)):
        raise BadRequest("Tanggal antar harus dalam 14 hari ke depan.", {"delivery_slot.date": "Tanggal antar harus dalam 14 hari ke depan."})
    options = upcoming_slots(app.opportunity.receiving_schedule, first)
    if not any(o[0] == d and o[1] == start and o[2] == end for o in options):
        raise BadRequest("Slot tidak ada di jadwal terima barang peluang.", {"delivery_slot": "Slot tidak ada di jadwal terima barang peluang."})
    return d, parse_hhmm(start), parse_hhmm(end)


def review(app, *, actor, actor_role, items, delivery_slot=None, stock_mode=StockMode.COUNT, note="", admin_reason=""):
    """Approve/reject every item. ≥ 1 approved → approved (takes quota), else rejected."""
    if len(note or "") > 300:
        raise BadRequest("Catatan maksimal 300 karakter.", {"note": "Maksimal 300 karakter."})
    with transaction.atomic():
        app = _lock(app)
        _expect(app, S.SUBMITTED)
        existing = {i.id: i for i in app.items.all()}
        by_id = {}
        for idx, raw in enumerate(items or []):
            try:
                item_id = int(raw.get("item_id", raw.get("id")))
            except (TypeError, ValueError):
                raise BadRequest("Item tidak dikenal.", {f"items.{idx}.item_id": "Item tidak dikenal."})
            if item_id not in existing:
                raise BadRequest("Item tidak dikenal.", {f"items.{idx}.item_id": "Item tidak dikenal."})
            by_id[item_id] = (idx, raw)
        if set(by_id) != set(existing):
            raise BadRequest("Semua produk wajib diputuskan.", {"items": "Semua produk wajib diputuskan."})

        errors = {}
        approved = 0
        for item_id, (idx, raw) in by_id.items():
            item = existing[item_id]
            decision = raw.get("decision")
            if decision == Decision.APPROVED:
                approved += 1
                qty, price = raw.get("approved_quantity"), raw.get("sell_price")
                try:
                    qty = int(qty)
                except (TypeError, ValueError):
                    qty = 0
                try:
                    price = int(price)
                except (TypeError, ValueError):
                    price = 0
                if qty < 1:
                    errors[f"items.{idx}.approved_quantity"] = "Jumlah minimal 1."
                if price < item.base_price:
                    errors[f"items.{idx}.sell_price"] = f"Harga jual minimal {rupiah(item.base_price)}."
                raw["_qty"], raw["_price"] = qty, price
            elif decision == Decision.REJECTED:
                reason = (raw.get("reject_reason") or "").strip()
                if not reason:
                    errors[f"items.{idx}.reject_reason"] = "Alasan tolak wajib diisi."
                raw["_reason"] = reason[:500]
            else:
                errors[f"items.{idx}.decision"] = "Pilih setujui atau tolak."
        if stock_mode not in StockMode.values:
            errors["stock_mode"] = "Pilih mode stok."
        if errors:
            raise BadRequest("Periksa kembali keputusan per produk.", errors)

        if approved:
            d, start, end = validate_slot(app, delivery_slot, allow_today=actor_role == "admin")
            _take_quota(app.opportunity_id)
            app.delivery_date, app.delivery_start, app.delivery_end = d, start, end
            app.stock_mode = stock_mode
        for item_id, (_idx, raw) in by_id.items():
            item = existing[item_id]
            if raw.get("decision") == Decision.APPROVED:
                c, income = calc_commission(raw["_price"], app.commission)
                item.decision = Decision.APPROVED
                item.approved_quantity = raw["_qty"]
                item.sell_price = raw["_price"]
                item.commission_per_unit = c
                item.umkm_income_per_unit = income
                item.reject_reason = ""
            else:
                item.decision = Decision.REJECTED
                item.reject_reason = raw["_reason"]
                item.approved_quantity = item.sell_price = item.commission_per_unit = item.umkm_income_per_unit = None
            item.save()

        prev = app.status
        app.status = S.APPROVED if approved else S.REJECTED
        app.review_note = note or ""
        app.save()
        override = actor_role == "admin"
        add_event(
            app, prev, app.status, actor, actor_role, note,
            label=("Diputuskan admin atas nama toko" if override else None),
            override=override, admin_reason=admin_reason,
        )

    store_name = app.opportunity.store.name
    if approved:
        notify(
            _umkm_user(app),
            N.APPLICATION_APPROVED,
            f"Pengajuan disetujui {store_name}",
            f"{approved} produk disetujui. Antar pada {fmt_slot(app.delivery_date, app.delivery_start, app.delivery_end)}.",
            application_link(app, "umkm"),
        )
    else:
        notify(
            _umkm_user(app),
            N.APPLICATION_REJECTED,
            f"Pengajuan ditolak {store_name}",
            "Lihat alasan penolakan di detail kerja sama.",
            application_link(app, "umkm"),
        )
    return app


# ---------------------------------------------------------------------------
# Ship / receive / complete / cancel
# ---------------------------------------------------------------------------


def ship(app, *, actor, note=""):
    with transaction.atomic():
        app = _lock(app)
        _expect(app, S.APPROVED)
        app.status = S.IN_DELIVERY
        app.save(update_fields=["status", "updated_at"])
        add_event(app, S.APPROVED, S.IN_DELIVERY, actor, "umkm", note)
    notify(
        _store_user(app),
        N.APPLICATION_STATUS_CHANGED,
        "Barang dalam perjalanan",
        f"{app.umkm.business_name} sedang mengantar barang ke toko Anda.",
        application_link(app, "store"),
    )
    return app


def _apply_received(app, quantities, actor):
    now = timezone.now()
    for item in app.items.all():
        if item.decision != Decision.APPROVED:
            continue
        qty = quantities.get(item.id, item.approved_quantity or 0)
        item.received_quantity = qty
        item.stock_quantity = qty
        item.stock_status = StockStatus.AVAILABLE if qty > 0 else StockStatus.OUT_OF_STOCK
        item.stock_updated_at = now
        item.stock_updated_by = actor
        item.save()


def parse_received(app, items):
    """[{item_id, received_quantity}] → {item_id: qty}, 0 ≤ qty ≤ approved_quantity."""
    approved = {i.id: i for i in app.items.all() if i.decision == Decision.APPROVED}
    out, errors = {}, {}
    for idx, raw in enumerate(items or []):
        try:
            item_id = int(raw.get("item_id", raw.get("id")))
            qty = int(raw.get("received_quantity"))
        except (TypeError, ValueError, AttributeError):
            errors[f"items.{idx}.received_quantity"] = "Jumlah tidak valid."
            continue
        item = approved.get(item_id)
        if item is None:
            errors[f"items.{idx}.item_id"] = "Item tidak dikenal atau tidak disetujui."
        elif not 0 <= qty <= (item.approved_quantity or 0):
            errors[f"items.{idx}.received_quantity"] = f"Jumlah diterima 0–{item.approved_quantity}."
        else:
            out[item_id] = qty
    if errors:
        raise BadRequest("Periksa jumlah barang diterima.", errors)
    return out


def receive(app, *, actor, items=None, note=""):
    with transaction.atomic():
        app = _lock(app)
        _expect(app, S.IN_DELIVERY)
        _apply_received(app, parse_received(app, items), actor)
        app.status = S.RECEIVED
        app.save(update_fields=["status", "updated_at"])
        add_event(app, S.IN_DELIVERY, S.RECEIVED, actor, "store", note)
    store_name = app.opportunity.store.name
    notify(
        _umkm_user(app),
        N.APPLICATION_STATUS_CHANGED,
        f"Barang diterima {store_name}",
        f"Produk Anda kini dijual di {store_name}.",
        application_link(app, "umkm"),
    )
    return app


def complete(app, *, actor, role, note=""):
    with transaction.atomic():
        app = _lock(app)
        _expect(app, S.RECEIVED)
        app.status = S.COMPLETED
        app.save(update_fields=["status", "updated_at"])
        add_event(app, S.RECEIVED, S.COMPLETED, actor, role, note)
    other, other_role = (_umkm_user(app), "umkm") if role == "store" else (_store_user(app), "store")
    notify(
        other,
        N.APPLICATION_STATUS_CHANGED,
        "Kerja sama selesai",
        f"Kerja sama {app.umkm.business_name} dengan {app.opportunity.store.name} diselesaikan.",
        application_link(app, other_role),
    )
    return app


def cancel(app, *, actor, role, reason):
    reason = (reason or "").strip()
    if not reason:
        raise BadRequest("Alasan pembatalan wajib diisi.", {"reason": "Alasan pembatalan wajib diisi."})
    with transaction.atomic():
        app = _lock(app)
        allowed = [S.SUBMITTED, S.APPROVED] if role == "umkm" else [S.APPROVED]
        _expect(app, *allowed)
        prev = app.status
        if prev in QUOTA_HOLDING:
            _release_quota(app.opportunity_id)
        app.status = S.CANCELLED
        app.save(update_fields=["status", "updated_at"])
        add_event(app, prev, S.CANCELLED, actor, role, reason[:500])
    other, other_role = (_umkm_user(app), "umkm") if role == "store" else (_store_user(app), "store")
    who = app.opportunity.store.name if role == "store" else app.umkm.business_name
    notify(
        other,
        N.APPLICATION_STATUS_CHANGED,
        "Pengajuan dibatalkan",
        f"{who} membatalkan kerja sama: {reason}",
        application_link(app, other_role),
    )
    return app


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


def update_stock(app, item_id, *, actor, data, notify_user=True, admin_reason=""):
    """Store (or admin) updates the shelf stock of one approved item while the application is received."""
    with transaction.atomic():
        app = _lock(app)
        _expect(app, S.RECEIVED)
        item = app.items.filter(pk=item_id, decision=Decision.APPROVED).first()
        if item is None:
            from ..exceptions import NotFound

            raise NotFound()
        before = item.stock_status
        if app.stock_mode == StockMode.COUNT:
            raw = data.get("stock_quantity")
            try:
                qty = int(raw)
            except (TypeError, ValueError):
                raise BadRequest("Jumlah stok wajib diisi.", {"stock_quantity": "Jumlah stok wajib diisi."})
            limit = item.received_quantity or 0
            if not 0 <= qty <= limit:
                raise BadRequest(f"Jumlah stok 0–{limit}.", {"stock_quantity": f"Jumlah stok 0–{limit} (jumlah diterima)."})
            item.stock_quantity = qty
            item.stock_status = StockStatus.AVAILABLE if qty > 0 else StockStatus.OUT_OF_STOCK
        else:
            status = data.get("stock_status")
            if status not in StockStatus.values:
                raise BadRequest("Pilih status stok.", {"stock_status": "Pilih Ada atau Habis."})
            item.stock_status = status
        item.stock_updated_at = timezone.now()
        item.stock_updated_by = actor
        item.save()
        if admin_reason:
            add_event(app, app.status, app.status, actor, "admin", label="Stok dikoreksi admin", override=True, admin_reason=admin_reason)
        else:
            app.save(update_fields=["updated_at"])

    if notify_user and before != StockStatus.OUT_OF_STOCK and item.stock_status == StockStatus.OUT_OF_STOCK:
        notify(
            _umkm_user(app),
            N.STOCK_OUT,
            f"Stok habis di {app.opportunity.store.name}",
            f"{item.product_name} habis di rak. Siapkan kiriman berikutnya.",
            application_link(app, "umkm"),
        )
    return app, item


# ---------------------------------------------------------------------------
# Admin force status
# ---------------------------------------------------------------------------


def force_status(app, *, admin, to_status, reason, received_items=None, notify_user=True):
    with transaction.atomic():
        app = _lock(app)
        prev = app.status
        if to_status not in ADMIN_TRANSITIONS.get(prev, []):
            raise Conflict(
                "Perubahan status ini tidak diizinkan dari status sekarang.",
                {"allowed": list(ADMIN_TRANSITIONS.get(prev, []))},
            )
        items = list(app.items.all())

        if to_status == S.SUBMITTED and has_active_application(app.umkm, app.opportunity, exclude=app):
            raise Conflict("UMKM ini sudah punya pengajuan aktif lain untuk peluang yang sama.")
        if prev in QUOTA_HOLDING and to_status in (S.SUBMITTED, S.CANCELLED):
            _release_quota(app.opportunity_id)

        if to_status == S.SUBMITTED:
            for it in items:
                it.decision = Decision.PENDING
                it.approved_quantity = it.sell_price = it.commission_per_unit = it.umkm_income_per_unit = None
                it.reject_reason = ""
                it.received_quantity = it.stock_quantity = None
                it.stock_status = None
                it.stock_updated_at = None
                it.save()
            app.delivery_date = app.delivery_start = app.delivery_end = None
        elif prev == S.IN_DELIVERY and to_status == S.RECEIVED:
            _apply_received(app, parse_received(app, received_items), admin)
        elif prev == S.RECEIVED and to_status == S.IN_DELIVERY:
            for it in items:
                it.received_quantity = it.stock_quantity = None
                it.stock_status = None
                it.stock_updated_at = None
                it.save()
        elif prev == S.RECEIVED and to_status == S.CANCELLED:
            now = timezone.now()
            for it in items:
                if it.decision == Decision.APPROVED:
                    it.stock_quantity = 0 if it.stock_quantity is not None else None
                    it.stock_status = StockStatus.OUT_OF_STOCK
                    it.stock_updated_at = now
                    it.stock_updated_by = admin
                    it.save()

        app.status = to_status
        app.save()
        add_event(app, prev, to_status, admin, "admin", label=f"Status diubah admin: {EVENT_LABEL[to_status].lower()}",
                  override=True, admin_reason=reason)

    if notify_user:
        text = f"Admin mengubah status kerja sama {app.umkm.business_name} – {app.opportunity.store.name} menjadi {S(to_status).label}."
        notify(_store_user(app), N.ADMIN_ACTION, "Status kerja sama diubah admin", text, application_link(app, "store"))
        notify(_umkm_user(app), N.ADMIN_ACTION, "Status kerja sama diubah admin", text, application_link(app, "umkm"))
    return app

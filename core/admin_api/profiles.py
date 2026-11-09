from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from ..exceptions import BadRequest, Conflict
from ..models import (
    AuditAction,
    LegalKind,
    LegalVerification,
    NotificationType,
    StoreProfile,
    TargetType,
    UmkmProfile,
    VerificationStatus,
)
from ..serializers import StoreProfileInput, UmkmProfileInput
from ..services.audit import audit, diff
from ..services.legal import KINDS, LABEL
from ..services.notify import notify
from ..services.profiles import save_store_profile, save_umkm_profile
from .base import AdminView, base_qs, get_any, notify_from, ok, order, parse_csv, reason_from
from .presenters import admin_store_out, admin_umkm_out

User = get_user_model()

STORE_FIELDS = ("name", "phone", "address", "lat", "lng", "operating_hours", "photo_id")
UMKM_FIELDS = ("business_name", "owner_name", "phone", "description", "address", "lat", "lng", "logo_id", "nib", "pirt_number", "halal_number")


def snapshot(obj, fields):
    return {f: getattr(obj, f) for f in fields}


def owner_for_create(request, kind):
    user_id = request.data.get("user_id")
    user = User.objects.filter(pk=user_id, deleted_at__isnull=True).first() if user_id else None
    if user is None:
        raise BadRequest("Pilih akun pemilik.", {"user_id": "Pilih akun pemilik."})
    model = StoreProfile if kind == "store" else UmkmProfile
    existing = model.all_objects.filter(user=user).first()
    if existing is not None:
        label = "toko" if kind == "store" else "UMKM"
        msg = f"Akun ini sudah punya profil {label}." if existing.deleted_at is None else f"Akun ini punya profil {label} yang dihapus. Pulihkan profil tersebut."
        raise Conflict(msg, {"user_id": msg, "profile_id": str(existing.id)})
    return user


def full_payload(instance_data, data):
    """PATCH: merge the request onto the current values so the full input serializer can validate."""
    merged = {**instance_data}
    merged.update({k: v for k, v in data.items() if k not in ("admin_reason", "notify_user", "user_id")})
    return merged


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def store_queryset(request):
    return base_qs(StoreProfile, request).select_related("user", "photo", "deleted_by")


def store_payload(s):
    return {"name": s.name, "phone": s.phone, "address": s.address, "lat": s.lat, "lng": s.lng,
            "operating_hours": s.operating_hours, "photo_file_id": str(s.photo_id) if s.photo_id else None}


class StoreListView(AdminView):
    """GET|POST /admin/stores"""

    def get(self, request):
        qs = store_queryset(request)
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(Q(name__icontains=t) | Q(address__icontains=t) | Q(user__email__icontains=t) | Q(user__name__icontains=t))
        if parse_csv(q.get("user_status")):
            qs = qs.filter(user__status__in=parse_csv(q["user_status"]))
        qs = order(qs, request, {"name": "name", "created_at": "created_at", "updated_at": "updated_at"}, "-created_at")
        return self.paged(qs, lambda s: admin_store_out(s, request))

    def post(self, request):
        user = owner_for_create(request, "store")
        s = StoreProfileInput(data=request.data)
        s.is_valid(raise_exception=True)
        profile = save_store_profile(user, s.validated_data, uploader=request.user)
        audit(request, AuditAction.CREATE, TargetType.STORE, profile.id, profile.name, reason_from(request, required=False))
        return ok(admin_store_out(profile, request), 201)


class StoreDetailView(AdminView):
    """GET|PATCH|DELETE /admin/stores/{id}"""

    def get(self, request, pk):
        return ok(admin_store_out(get_any(StoreProfile, pk), request))

    def patch(self, request, pk):
        store = get_any(StoreProfile, pk)
        s = StoreProfileInput(data=full_payload(store_payload(store), request.data))
        s.is_valid(raise_exception=True)
        before = snapshot(store, STORE_FIELDS)
        was_deleted = (store.deleted_at, store.deleted_by)
        store = save_store_profile(store.user, s.validated_data, uploader=request.user)
        if was_deleted[0]:  # editing must not silently restore a deleted profile
            store.deleted_at, store.deleted_by = was_deleted
            store.save(update_fields=["deleted_at", "deleted_by"])
        changes = diff(before, snapshot(store, STORE_FIELDS))
        reason = reason_from(request, required=False)
        audit(request, AuditAction.UPDATE, TargetType.STORE, store.id, store.name, reason, changes)
        if changes and notify_from(request):
            notify(store.user, NotificationType.ADMIN_ACTION, "Profil toko diperbarui admin",
                   "Admin memperbarui profil toko Anda." + (f" Alasan: {reason}" if reason else ""))
        return ok(admin_store_out(store, request))

    def delete(self, request, pk):
        store = get_any(StoreProfile, pk)
        reason = reason_from(request)
        if store.deleted_at:
            raise Conflict("Profil toko sudah dihapus.")
        store.soft_delete(request.user)
        audit(request, AuditAction.DELETE, TargetType.STORE, store.id, store.name, reason)
        if notify_from(request):
            notify(store.user, NotificationType.ADMIN_ACTION, "Profil toko dihapus admin",
                   f"Profil toko {store.name} dihapus admin. Alasan: {reason}")
        return ok(admin_store_out(store, request))


class StoreRestoreView(AdminView):
    """POST /admin/stores/{id}/restore"""

    def post(self, request, pk):
        store = get_any(StoreProfile, pk)
        reason = reason_from(request)
        if not store.deleted_at:
            raise Conflict("Profil toko tidak dalam keadaan terhapus.")
        store.restore()
        audit(request, AuditAction.RESTORE, TargetType.STORE, store.id, store.name, reason)
        return ok(admin_store_out(store, request))


# ---------------------------------------------------------------------------
# UMKMs
# ---------------------------------------------------------------------------


def umkm_queryset(request):
    return base_qs(UmkmProfile, request).select_related("user", "logo", "deleted_by").prefetch_related(
        "legal_verifications__decided_by", "legal_documents"
    )


def umkm_payload(u):
    return {
        "business_name": u.business_name, "owner_name": u.owner_name, "phone": u.phone,
        "description": u.description, "address": u.address, "lat": u.lat, "lng": u.lng,
        "logo_file_id": str(u.logo_id) if u.logo_id else None,
        "legal": {"nib": u.nib, "pirt_number": u.pirt_number, "halal_number": u.halal_number},
        "legal_document_file_ids": [str(d.id) for d in u.legal_documents.all()],
    }


class UmkmListView(AdminView):
    """GET|POST /admin/umkms"""

    def get(self, request):
        qs = umkm_queryset(request)
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(Q(business_name__icontains=t) | Q(owner_name__icontains=t) | Q(address__icontains=t) | Q(user__email__icontains=t))
        statuses = parse_csv(q.get("legal_status"))
        if statuses:
            cond = Q(legal_verifications__status__in=[s for s in statuses if s != "unverified"])
            if "unverified" in statuses:
                # No number at all (no verification row, or every row unverified).
                cond |= ~Q(legal_verifications__status__in=["pending", "verified", "rejected"])
            qs = qs.filter(cond).distinct()
        qs = order(qs, request, {"business_name": "business_name", "created_at": "created_at", "updated_at": "updated_at"}, "-created_at")
        return self.paged(qs, lambda u: admin_umkm_out(u, request))

    def post(self, request):
        user = owner_for_create(request, "umkm")
        data = dict(request.data.items()) if hasattr(request.data, "items") else request.data
        data.setdefault("owner_name", user.name)
        s = UmkmProfileInput(data=data)
        s.is_valid(raise_exception=True)
        profile = save_umkm_profile(user, s.validated_data, uploader=request.user, from_owner=False)
        audit(request, AuditAction.CREATE, TargetType.UMKM, profile.id, profile.business_name, reason_from(request, required=False))
        return ok(admin_umkm_out(profile, request), 201)


class UmkmDetailView(AdminView):
    """GET|PATCH|DELETE /admin/umkms/{id}"""

    def get(self, request, pk):
        return ok(admin_umkm_out(get_any(UmkmProfile, pk), request))

    def patch(self, request, pk):
        umkm = get_any(UmkmProfile, pk)
        s = UmkmProfileInput(data=full_payload(umkm_payload(umkm), request.data))
        s.is_valid(raise_exception=True)
        before = snapshot(umkm, UMKM_FIELDS)
        was_deleted = (umkm.deleted_at, umkm.deleted_by)
        umkm = save_umkm_profile(umkm.user, s.validated_data, uploader=request.user, from_owner=False)
        if was_deleted[0]:
            umkm.deleted_at, umkm.deleted_by = was_deleted
            umkm.save(update_fields=["deleted_at", "deleted_by"])
        changes = diff(before, snapshot(umkm, UMKM_FIELDS))
        reason = reason_from(request, required=False)
        audit(request, AuditAction.UPDATE, TargetType.UMKM, umkm.id, umkm.business_name, reason, changes)
        if changes and notify_from(request):
            notify(umkm.user, NotificationType.ADMIN_ACTION, "Profil UMKM diperbarui admin",
                   "Admin memperbarui profil usaha Anda." + (f" Alasan: {reason}" if reason else ""))
        return ok(admin_umkm_out(umkm, request))

    def delete(self, request, pk):
        umkm = get_any(UmkmProfile, pk)
        reason = reason_from(request)
        if umkm.deleted_at:
            raise Conflict("Profil UMKM sudah dihapus.")
        umkm.soft_delete(request.user)
        audit(request, AuditAction.DELETE, TargetType.UMKM, umkm.id, umkm.business_name, reason)
        if notify_from(request):
            notify(umkm.user, NotificationType.ADMIN_ACTION, "Profil UMKM dihapus admin",
                   f"Profil usaha {umkm.business_name} dihapus admin. Alasan: {reason}")
        return ok(admin_umkm_out(umkm, request))


class UmkmRestoreView(AdminView):
    """POST /admin/umkms/{id}/restore"""

    def post(self, request, pk):
        umkm = get_any(UmkmProfile, pk)
        reason = reason_from(request)
        if not umkm.deleted_at:
            raise Conflict("Profil UMKM tidak dalam keadaan terhapus.")
        umkm.restore()
        audit(request, AuditAction.RESTORE, TargetType.UMKM, umkm.id, umkm.business_name, reason)
        return ok(admin_umkm_out(umkm, request))


class DecisionInput(serializers.Serializer):
    status = serializers.ChoiceField(choices=[VerificationStatus.VERIFIED, VerificationStatus.REJECTED])
    note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class LegalDecisionInput(serializers.Serializer):
    nib = DecisionInput(required=False)
    pirt = DecisionInput(required=False)
    halal = DecisionInput(required=False)
    notify_user = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        if not any(k in attrs for k in KINDS):
            raise serializers.ValidationError("Putuskan minimal satu jenis legalitas.")
        for k in KINDS:
            if k in attrs and attrs[k]["status"] == VerificationStatus.REJECTED and not attrs[k]["note"].strip():
                raise serializers.ValidationError({k: {"note": "Catatan wajib diisi bila ditolak."}})
        return attrs


class LegalVerificationView(AdminView):
    """POST /admin/umkms/{id}/legal-verification {nib?:{status,note}, pirt?, halal?, notify_user}"""

    def post(self, request, pk):
        umkm = get_any(UmkmProfile, pk, UmkmProfile.objects.all())
        s = LegalDecisionInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        now = timezone.now()
        changes, verified, rejected = [], [], []
        for kind in KINDS:
            if kind not in d:
                continue
            if not umkm.legal_number(kind):
                raise BadRequest("Nomor belum diisi UMKM.", {kind: "Nomor belum diisi UMKM."})
            v, _ = LegalVerification.objects.get_or_create(umkm=umkm, kind=kind)
            changes.append({"field": f"legal_verification.{kind}.status", "before": v.status, "after": d[kind]["status"]})
            v.status = d[kind]["status"]
            v.note = d[kind]["note"].strip()
            v.decided_by, v.decided_at = request.user, now
            v.save()
            (verified if v.status == VerificationStatus.VERIFIED else rejected).append((kind, v.note))
        reason = "; ".join(f"{LegalKind(k).label}: {n}" for k, n in rejected) or "Legalitas terverifikasi"
        audit(request, AuditAction.VERIFY_LEGAL, TargetType.UMKM, umkm.id, umkm.business_name, reason, changes)
        if d["notify_user"]:
            link = {"deeplink": "/profil/umkm", "role": "umkm"}
            if verified:
                names = ", ".join(LABEL[k] for k, _ in verified)
                notify(umkm.user, NotificationType.LEGAL_VERIFIED, f"{names[0].upper() + names[1:]} terverifikasi",
                       f"{names[0].upper() + names[1:]} {umkm.business_name} sudah diverifikasi admin.", link)
            for kind, note in rejected:
                notify(umkm.user, NotificationType.LEGAL_REJECTED, f"{LABEL[kind][0].upper() + LABEL[kind][1:]} ditolak",
                       f"{note} Perbarui data legalitas Anda.", link)
        umkm = UmkmProfile.all_objects.get(pk=umkm.pk)
        return ok(admin_umkm_out(umkm, request))


class LegalQueueView(AdminView):
    """GET /admin/legal-verifications ?status=pending|verified|rejected — oldest waiting first."""

    def get(self, request):
        status = request.query_params.get("status", VerificationStatus.PENDING)
        if status not in (VerificationStatus.PENDING, VerificationStatus.VERIFIED, VerificationStatus.REJECTED):
            raise BadRequest("Status tidak dikenal.", {"status": "Pilih pending, verified, atau rejected."})
        rows = (
            LegalVerification.objects.filter(status=status, umkm__deleted_at__isnull=True)
            .select_related("umkm__user", "decided_by")
            .order_by("submitted_at", "decided_at")
        )
        grouped = {}
        for v in rows:
            g = grouped.setdefault(v.umkm_id, {"umkm": v.umkm, "kinds": [], "since": v.submitted_at, "decided_at": v.decided_at})
            g["kinds"].append(v.kind)
            if v.submitted_at and (g["since"] is None or v.submitted_at < g["since"]):
                g["since"] = v.submitted_at
        items = sorted(grouped.values(), key=lambda g: (g["since"] is None, g["since"] or timezone.now()))
        from ..presenters import iso

        def out(g):
            u = g["umkm"]
            return {
                "umkm": {"id": str(u.id), "business_name": u.business_name, "owner_name": u.owner_name, "owner_id": str(u.user_id)},
                "kinds": [k for k in KINDS if k in g["kinds"]],
                "waiting_since": iso(g["since"]) if status == VerificationStatus.PENDING else None,
                "decided_at": iso(g["decided_at"]),
            }

        return self.paged(items, out)

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from ..exceptions import Conflict, Forbidden, HasActiveRelations
from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    ROLES,
    AccountStatus,
    AdminLevel,
    Application,
    AuditAction,
    NotificationType,
    Opportunity,
    Product,
    StoreProfile,
    TargetType,
    UmkmProfile,
)
from ..permissions import require_superadmin
from ..services.audit import audit, diff
from ..services.notify import notify
from .base import AdminView, get_any, include_deleted, notify_from, ok, order, parse_bool, parse_csv, reason_from
from .presenters import user_detail_out, user_list_out, user_stats

User = get_user_model()
logger = logging.getLogger("core.admin")


def label(u):
    return u.name or u.email


def active_superadmins(exclude=None):
    qs = User.objects.filter(is_admin=True, admin_role=AdminLevel.SUPERADMIN, deleted_at__isnull=True, status=AccountStatus.ACTIVE)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs


def revoke_sessions(user):
    for t in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=t)


def active_application_ids(user, role=None):
    store = StoreProfile.all_objects.filter(user=user).first()
    umkm = UmkmProfile.all_objects.filter(user=user).first()
    q = Q(pk__in=[])
    if store and role in (None, "store"):
        q |= Q(opportunity__store=store)
    if umkm and role in (None, "umkm"):
        q |= Q(umkm=umkm)
    return [str(a) for a in Application.objects.filter(q, status__in=ACTIVE_APPLICATION_STATUSES).values_list("id", flat=True)]


class InviteInput(serializers.Serializer):
    email = serializers.EmailField(error_messages={"invalid": "Masukkan email yang valid.", "required": "Masukkan email yang valid."})
    name = serializers.CharField(max_length=150)
    roles = serializers.ListField(child=serializers.ChoiceField(choices=ROLES), min_length=1, error_messages={"min_length": "Pilih minimal satu peran."})
    send_invitation_email = serializers.BooleanField(required=False, default=True)


class UserPatchInput(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    roles = serializers.ListField(child=serializers.ChoiceField(choices=ROLES), required=False)
    is_admin = serializers.BooleanField(required=False)
    admin_role = serializers.ChoiceField(choices=AdminLevel.choices, required=False, allow_null=True)


class UserListView(AdminView):
    """GET|POST /admin/users"""

    def get(self, request):
        qs = User.objects.all() if include_deleted(request) else User.objects.filter(deleted_at__isnull=True)
        qs = qs.select_related("store_profile", "umkm_profile")
        q = request.query_params
        if q.get("search"):
            t = q["search"].strip()
            qs = qs.filter(
                Q(name__icontains=t) | Q(email__icontains=t) | Q(store_profile__name__icontains=t) | Q(umkm_profile__business_name__icontains=t)
            )
        roles = parse_csv(q.get("role"))
        if roles:
            cond = Q(pk__in=[])
            for r in roles:
                cond |= Q(roles=[]) if r == "none" else Q(roles__icontains=f'"{r}"')
            qs = qs.filter(cond)
        statuses = parse_csv(q.get("status"))
        if statuses:
            qs = qs.filter(status__in=statuses)
        is_admin = parse_bool(q.get("is_admin"))
        if is_admin is not None:
            qs = qs.filter(is_admin=is_admin)
        qs = order(qs, request, {"name": "name", "email": "email", "last_login_at": "last_login", "created_at": "created_at"}, "-created_at")
        return self.paged(qs, user_list_out)

    def post(self, request):
        s = InviteInput(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        email = d["email"].lower()
        existing = User.objects.filter(email__iexact=email).first()
        if existing:
            if existing.deleted_at:
                raise Conflict(
                    "Email ini milik akun yang dihapus. Aktifkan 'Tampilkan yang dihapus' di daftar Pengguna untuk memulihkannya.",
                    {"email": "Email ini milik akun yang dihapus.", "user_id": str(existing.id), "deleted": True},
                )
            raise Conflict("Email ini sudah terdaftar.", {"email": "Email ini sudah terdaftar.", "user_id": str(existing.id)})
        user = User.objects.create_user(
            email=email, name=d["name"], roles=[r for r in ROLES if r in d["roles"]],
            status=AccountStatus.INVITED, invited_by=request.user,
        )
        audit(request, AuditAction.INVITE, TargetType.USER, user.id, label(user), reason_from(request, required=False))
        if d["send_invitation_email"]:
            try:
                send_mail(
                    "Undangan bergabung di RitelHub",
                    f"Halo {user.name},\n\nAnda diundang mengikuti pilot RitelHub. Masuk dengan akun Google {email} di {settings.FRONTEND_URL}/masuk.",
                    settings.DEFAULT_FROM_EMAIL,
                    [email],
                )
            except Exception as exc:
                logger.warning("Invitation email to %s failed: %s", email, exc)
        return ok(user_detail_out(user), 201)


class UserDetailView(AdminView):
    """GET|PATCH|DELETE /admin/users/{id}  (DELETE ?hard=true for permanent deletion)"""

    def get(self, request, pk):
        return ok(user_detail_out(get_any(User, pk, User.objects.all())))

    def patch(self, request, pk):
        user = get_any(User, pk, User.objects.all())
        s = UserPatchInput(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        reason = reason_from(request, required=False)
        before = {"name": user.name, "roles": user.roles, "is_admin": user.is_admin, "admin_role": user.admin_role}

        if "roles" in d:
            for removed in set(user.roles or []) - set(d["roles"]):
                ids = active_application_ids(user, removed)
                if ids:
                    raise HasActiveRelations(
                        f"Peran {'toko' if removed == 'store' else 'UMKM'} masih punya pengajuan aktif.",
                        {"application_ids": ids},
                    )
            user.roles = [r for r in ROLES if r in d["roles"]]
        if "name" in d:
            user.name = d["name"]

        admin_change = None
        if "is_admin" in d or "admin_role" in d:
            require_superadmin(request.user)
            make_admin = d.get("is_admin", user.is_admin)
            level = d.get("admin_role", user.admin_role) if make_admin else None
            if make_admin and level is None:
                level = AdminLevel.STAFF
            losing_super = user.admin_role == AdminLevel.SUPERADMIN and level != AdminLevel.SUPERADMIN
            if losing_super and not active_superadmins(exclude=user).exists():
                raise Conflict("Harus ada minimal satu superadmin.")
            if user.pk == request.user.pk and not make_admin:
                raise Forbidden("Anda tidak bisa mencabut akses admin sendiri.")
            if make_admin and not user.is_admin:
                admin_change = AuditAction.GRANT_ADMIN
            elif not make_admin and user.is_admin:
                admin_change = AuditAction.REVOKE_ADMIN
            user.is_admin, user.admin_role = make_admin, level
        user.save()

        after = {"name": user.name, "roles": user.roles, "is_admin": user.is_admin, "admin_role": user.admin_role}
        changes = diff(before, after)
        if changes:
            audit(request, admin_change or AuditAction.UPDATE, TargetType.USER, user.id, label(user), reason, changes)
        return ok(user_detail_out(user))

    def delete(self, request, pk):
        user = get_any(User, pk, User.objects.all())
        reason = reason_from(request)
        if user.pk == request.user.pk:
            raise Forbidden("Anda tidak bisa menghapus akun sendiri.")
        if user.is_admin and user.admin_role == AdminLevel.SUPERADMIN and not active_superadmins(exclude=user).exists():
            raise Conflict("Harus ada minimal satu superadmin.")
        if parse_bool(request.query_params.get("hard")):
            require_superadmin(request.user)
            if user_stats(user)["applications_total"]:
                raise HasActiveRelations("Akun ini punya riwayat pengajuan sehingga tidak bisa dihapus permanen.", {"application_ids": active_application_ids(user)})
            audit(request, AuditAction.HARD_DELETE, TargetType.USER, user.id, label(user), reason)
            with transaction.atomic():
                user.delete()
            return ok({"deleted": True, "hard": True})
        if user.deleted_at:
            raise Conflict("Akun sudah dihapus.")
        now = timezone.now()
        with transaction.atomic():
            user.deleted_at, user.deleted_by = now, request.user
            user.save(update_fields=["deleted_at", "deleted_by"])
            store = StoreProfile.objects.filter(user=user).first()
            umkm = UmkmProfile.objects.filter(user=user).first()
            if store:
                Opportunity.objects.filter(store=store).update(deleted_at=now, deleted_by=request.user)
                StoreProfile.objects.filter(pk=store.pk).update(deleted_at=now, deleted_by=request.user)
            if umkm:
                Product.objects.filter(umkm=umkm).update(deleted_at=now, deleted_by=request.user)
                UmkmProfile.objects.filter(pk=umkm.pk).update(deleted_at=now, deleted_by=request.user)
            revoke_sessions(user)
        audit(request, AuditAction.DELETE, TargetType.USER, user.id, label(user), reason, [{"field": "deleted_at", "before": None, "after": now.isoformat()}])
        return ok(user_detail_out(user))


class UserSuspendView(AdminView):
    """POST /admin/users/{id}/suspend {admin_reason, notify_user}"""

    def post(self, request, pk):
        user = get_any(User, pk, User.objects.filter(deleted_at__isnull=True))
        reason = reason_from(request)
        if user.pk == request.user.pk:
            raise Forbidden("Anda tidak bisa menyuspend akun sendiri.")
        if user.status == AccountStatus.SUSPENDED:
            raise Conflict("Akun sudah disuspend.", {"allowed": ["activate"]})
        if user.is_superadmin:
            require_superadmin(request.user)
            if not active_superadmins(exclude=user).exists():
                raise Conflict("Harus ada minimal satu superadmin.")
        prev = user.status
        user.status, user.suspended_at, user.suspended_reason = AccountStatus.SUSPENDED, timezone.now(), reason
        user.save(update_fields=["status", "suspended_at", "suspended_reason"])
        revoke_sessions(user)
        audit(request, AuditAction.SUSPEND, TargetType.USER, user.id, label(user), reason, [{"field": "status", "before": prev, "after": "suspended"}])
        if notify_from(request):
            notify(user, NotificationType.ADMIN_ACTION, "Akun Anda disuspend",
                   "Akun Anda dinonaktifkan sementara oleh admin. Hubungi pengelola platform untuk informasi lebih lanjut.")
        return ok({**user_detail_out(user), "applications_active": user_stats(user)["applications_active"]})


class UserActivateView(AdminView):
    """POST /admin/users/{id}/activate {admin_reason?, notify_user}"""

    def post(self, request, pk):
        user = get_any(User, pk, User.objects.filter(deleted_at__isnull=True))
        reason = reason_from(request, required=False)
        if user.status != AccountStatus.SUSPENDED:
            raise Conflict("Akun tidak sedang disuspend.", {"allowed": ["suspend"]})
        user.status = AccountStatus.ACTIVE
        user.suspended_at, user.suspended_reason = None, ""
        user.save(update_fields=["status", "suspended_at", "suspended_reason"])
        audit(request, AuditAction.ACTIVATE, TargetType.USER, user.id, label(user), reason, [{"field": "status", "before": "suspended", "after": user.status}])
        if notify_from(request):
            notify(user, NotificationType.ADMIN_ACTION, "Akun Anda diaktifkan kembali", "Anda sudah bisa masuk dan memakai aplikasi lagi.")
        return ok(user_detail_out(user))


class UserRestoreView(AdminView):
    """POST /admin/users/{id}/restore {admin_reason}: brings back everything deleted together with the account."""

    def post(self, request, pk):
        user = get_any(User, pk, User.objects.all())
        reason = reason_from(request)
        if not user.deleted_at:
            raise Conflict("Akun tidak dalam keadaan terhapus.")
        when = user.deleted_at
        with transaction.atomic():
            store = StoreProfile.all_objects.filter(user=user, deleted_at=when).first()
            umkm = UmkmProfile.all_objects.filter(user=user, deleted_at=when).first()
            if store:
                Opportunity.all_objects.filter(store=store, deleted_at=when).update(deleted_at=None, deleted_by=None)
                StoreProfile.all_objects.filter(pk=store.pk).update(deleted_at=None, deleted_by=None)
            if umkm:
                Product.all_objects.filter(umkm=umkm, deleted_at=when).update(deleted_at=None, deleted_by=None)
                UmkmProfile.all_objects.filter(pk=umkm.pk).update(deleted_at=None, deleted_by=None)
            user.deleted_at, user.deleted_by = None, None
            user.save(update_fields=["deleted_at", "deleted_by"])
        audit(request, AuditAction.RESTORE, TargetType.USER, user.id, label(user), reason)
        return ok(user_detail_out(user))


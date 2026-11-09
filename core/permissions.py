from rest_framework.permissions import BasePermission

from .exceptions import SuperadminRequired
from .models import ROLE_STORE, ROLE_UMKM, AccountStatus


class IsStore(BasePermission):
    message = "Halaman ini khusus untuk pemilik toko."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and ROLE_STORE in (u.roles or []) and u.store is not None)


class IsUmkm(BasePermission):
    message = "Halaman ini khusus untuk pelaku UMKM."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and ROLE_UMKM in (u.roles or []) and u.umkm is not None)


class IsPlatformAdmin(BasePermission):
    message = "Akun ini tidak memiliki akses admin."

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u and u.is_authenticated and u.is_admin and u.admin_role
            and u.status == AccountStatus.ACTIVE and u.deleted_at is None
        )


def require_superadmin(user):
    if not user.is_superadmin:
        raise SuperadminRequired()

"""Django admin site (developer tool at /django-admin/). The product admin panel uses /api/v1/admin/."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import (
    AdminAuditLog,
    Application,
    ApplicationEvent,
    ApplicationItem,
    Category,
    LegalVerification,
    Notification,
    Opportunity,
    Product,
    ProductPhoto,
    StoreProfile,
    UmkmProfile,
    UploadedFile,
    User,
)


@admin.register(User)
class PlatformUserAdmin(UserAdmin):
    ordering = ("-created_at",)
    list_display = ("email", "name", "status", "is_admin", "admin_role", "is_staff", "deleted_at")
    list_filter = ("status", "is_admin", "admin_role", "is_staff")
    search_fields = ("email", "name")
    readonly_fields = ("created_at", "last_login")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profil", {"fields": ("name", "avatar_url", "google_sub", "roles")}),
        ("Status", {"fields": ("status", "suspended_at", "suspended_reason", "deleted_at", "invited_by")}),
        ("Admin panel", {"fields": ("is_admin", "admin_role")}),
        ("Django admin", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Waktu", {"fields": ("created_at", "last_login")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


class AllRowsAdmin(admin.ModelAdmin):
    """Show soft-deleted rows too."""

    def get_queryset(self, request):
        return self.model.all_objects.all()


@admin.register(StoreProfile)
class StoreProfileAdmin(AllRowsAdmin):
    list_display = ("name", "user", "phone", "deleted_at")
    search_fields = ("name", "user__email")


class LegalVerificationInline(admin.TabularInline):
    model = LegalVerification
    extra = 0


@admin.register(UmkmProfile)
class UmkmProfileAdmin(AllRowsAdmin):
    list_display = ("business_name", "owner_name", "user", "deleted_at")
    search_fields = ("business_name", "user__email")
    inlines = [LegalVerificationInline]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "sort_order")
    prepopulated_fields = {"slug": ("name",)}


class ProductPhotoInline(admin.TabularInline):
    model = ProductPhoto
    extra = 0


@admin.register(Product)
class ProductAdmin(AllRowsAdmin):
    list_display = ("name", "umkm", "category", "base_price", "is_active", "deleted_at")
    list_filter = ("is_active", "category")
    search_fields = ("name",)
    inlines = [ProductPhotoInline]


@admin.register(Opportunity)
class OpportunityAdmin(AllRowsAdmin):
    list_display = ("title", "store", "status", "quota_used", "quota", "valid_until", "deleted_at")
    list_filter = ("status",)
    search_fields = ("title", "store__name")


class ApplicationItemInline(admin.TabularInline):
    model = ApplicationItem
    extra = 0


class ApplicationEventInline(admin.TabularInline):
    model = ApplicationEvent
    extra = 0


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("id", "umkm", "opportunity", "status", "delivery_date", "updated_at")
    list_filter = ("status",)
    inlines = [ApplicationItemInline, ApplicationEventInline]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "type", "email_status", "read_at", "created_at")
    list_filter = ("type", "email_status")


@admin.register(UploadedFile)
class UploadedFileAdmin(admin.ModelAdmin):
    list_display = ("original_name", "purpose", "owner", "size", "is_removed", "created_at")
    list_filter = ("purpose", "is_removed")


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "admin_name", "action", "target_type", "target_label")
    list_filter = ("action", "target_type")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

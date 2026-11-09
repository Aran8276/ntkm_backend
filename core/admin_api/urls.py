from django.urls import re_path

from ..api.urls import route
from . import applications as apps
from . import catalog, exports, profiles, system, users

urlpatterns = [
    # Users (A03–A05, A24)
    *route("users", users.UserListView.as_view(), "admin-users"),
    *route("users/<uuid:pk>", users.UserDetailView.as_view(), "admin-user"),
    *route("users/<uuid:pk>/suspend", users.UserSuspendView.as_view(), "admin-user-suspend"),
    *route("users/<uuid:pk>/activate", users.UserActivateView.as_view(), "admin-user-activate"),
    *route("users/<uuid:pk>/restore", users.UserRestoreView.as_view(), "admin-user-restore"),
    # Stores & UMKMs (A06–A08, A18)
    *route("stores", profiles.StoreListView.as_view(), "admin-stores"),
    *route("stores/<uuid:pk>", profiles.StoreDetailView.as_view(), "admin-store"),
    *route("stores/<uuid:pk>/restore", profiles.StoreRestoreView.as_view(), "admin-store-restore"),
    *route("umkms", profiles.UmkmListView.as_view(), "admin-umkms"),
    *route("umkms/<uuid:pk>", profiles.UmkmDetailView.as_view(), "admin-umkm"),
    *route("umkms/<uuid:pk>/restore", profiles.UmkmRestoreView.as_view(), "admin-umkm-restore"),
    *route("umkms/<uuid:pk>/legal-verification", profiles.LegalVerificationView.as_view(), "admin-umkm-legal"),
    *route("legal-verifications", profiles.LegalQueueView.as_view(), "admin-legal-queue"),
    # Opportunities & products (A09–A14)
    *route("opportunities", catalog.OpportunityListView.as_view(), "admin-opportunities"),
    *route("opportunities/<uuid:pk>", catalog.OpportunityDetailView.as_view(), "admin-opportunity"),
    *route("opportunities/<uuid:pk>/close", catalog.OpportunityCloseView.as_view(), "admin-opportunity-close"),
    *route("opportunities/<uuid:pk>/reopen", catalog.OpportunityReopenView.as_view(), "admin-opportunity-reopen"),
    *route("opportunities/<uuid:pk>/restore", catalog.OpportunityRestoreView.as_view(), "admin-opportunity-restore"),
    *route("products", catalog.ProductListView.as_view(), "admin-products"),
    *route("products/<uuid:pk>", catalog.ProductDetailView.as_view(), "admin-product"),
    *route("products/<uuid:pk>/restore", catalog.ProductRestoreView.as_view(), "admin-product-restore"),
    # Applications & stock (A15–A17)
    *route("applications", apps.ApplicationListView.as_view(), "admin-applications"),
    *route("applications/<uuid:pk>", apps.ApplicationDetailView.as_view(), "admin-application"),
    *route("applications/<uuid:pk>/review", apps.ApplicationReviewView.as_view(), "admin-application-review"),
    *route("applications/<uuid:pk>/force-status", apps.ApplicationForceStatusView.as_view(), "admin-application-force"),
    *route("applications/<uuid:pk>/items/<int:item_id>/stock", apps.AdminItemStockView.as_view(), "admin-application-item-stock"),
    *route("stock", apps.AdminStockView.as_view(), "admin-stock"),
    # System (A02, A19–A23)
    *route("dashboard", system.DashboardView.as_view(), "admin-dashboard"),
    *route("categories", system.CategoryListView.as_view(), "admin-categories"),
    *route("categories/<uuid:pk>", system.CategoryDetailView.as_view(), "admin-category"),
    *route("files", system.FileListView.as_view(), "admin-files"),
    *route("files/<uuid:pk>", system.FileDetailView.as_view(), "admin-file"),
    *route("files/<uuid:pk>/remove", system.FileRemoveView.as_view(), "admin-file-remove"),
    *route("notifications", system.NotificationLogView.as_view(), "admin-notifications"),
    *route("notifications/broadcast", system.BroadcastView.as_view(), "admin-broadcast"),
    *route("notifications/<uuid:pk>/resend-email", system.ResendEmailView.as_view(), "admin-notification-resend"),
    *route("audit-logs", system.AuditLogListView.as_view(), "admin-audit-logs"),
    *route("audit-logs/<uuid:pk>", system.AuditLogDetailView.as_view(), "admin-audit-log"),
    re_path(r"^exports/(?P<resource>[a-z-]+)\.csv/?$", exports.ExportView.as_view(), name="admin-export"),
]


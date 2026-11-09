from django.urls import path

from . import applications as apps
from . import auth, me, notifications, opportunities as opps, products


def route(pattern, view, name):
    """Register ``pattern`` with and without a trailing slash (spec paths have none)."""
    return [path(pattern, view, name=name), path(pattern + "/", view)]


urlpatterns = [
    *route("auth/google", auth.GoogleLoginView.as_view(), "auth-google"),
    *route("auth/dev-login", auth.DevLoginView.as_view(), "auth-dev-login"),
    *route("auth/token/refresh", auth.RefreshView.as_view(), "auth-refresh"),
    *route("auth/logout", auth.LogoutView.as_view(), "auth-logout"),
    *route("me", me.MeView.as_view(), "me"),
    *route("me/roles", me.RolesView.as_view(), "me-roles"),
    *route("me/store-profile", me.StoreProfileView.as_view(), "me-store-profile"),
    *route("me/umkm-profile", me.UmkmProfileView.as_view(), "me-umkm-profile"),
    *route("uploads", me.UploadView.as_view(), "uploads"),
    *route("categories", me.CategoryListView.as_view(), "categories"),
    *route("me/products", products.MyProductListView.as_view(), "my-products"),
    *route("me/products/<uuid:pk>", products.MyProductDetailView.as_view(), "my-product"),
    *route("me/opportunities", opps.MyOpportunityListView.as_view(), "my-opportunities"),
    *route("me/opportunities/<uuid:pk>", opps.MyOpportunityDetailView.as_view(), "my-opportunity"),
    *route("me/opportunities/<uuid:pk>/close", opps.MyOpportunityCloseView.as_view(), "my-opportunity-close"),
    *route("opportunities", opps.FeedView.as_view(), "opportunities"),
    *route("opportunities/<uuid:pk>", opps.FeedDetailView.as_view(), "opportunity"),
    *route("applications", apps.ApplicationListView.as_view(), "applications"),
    *route("applications/<uuid:pk>", apps.ApplicationDetailView.as_view(), "application"),
    *route("applications/<uuid:pk>/timeline", apps.ApplicationTimelineView.as_view(), "application-timeline"),
    *[
        p
        for action in ("review", "ship", "receive", "complete", "cancel")
        for p in route(f"applications/<uuid:pk>/{action}", apps.ApplicationActionView.as_view(workflow_action=action), f"application-{action}")
    ],
    *route("applications/<uuid:pk>/items/<int:item_id>/stock", apps.ItemStockView.as_view(), "application-item-stock"),
    *route("stock", apps.StockListView.as_view(), "stock"),
    *route("notifications", notifications.NotificationListView.as_view(), "notifications"),
    *route("notifications/unread-count", notifications.UnreadCountView.as_view(), "notifications-unread-count"),
    *route("notifications/read-all", notifications.ReadAllView.as_view(), "notifications-read-all"),
    *route("notifications/<uuid:pk>/read", notifications.NotificationReadView.as_view(), "notification-read"),
]

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from ..exceptions import NotFound
from ..presenters import notification_out, unread_count
from .common import ListView, parse_bool


class NotificationListView(ListView):
    """GET /notifications ?unread=true"""

    def get(self, request):
        qs = request.user.notifications.all()
        if parse_bool(request.query_params.get("unread")):
            qs = qs.filter(read_at__isnull=True)
        return self.paged(qs, notification_out)


class UnreadCountView(ListView):
    """GET /notifications/unread-count → {count}"""

    def get(self, request):
        return Response({"count": unread_count(request.user)})


class NotificationReadView(ListView):
    """POST /notifications/{id}/read"""

    def post(self, request, pk):
        n = request.user.notifications.filter(pk=pk).first()
        if n is None:
            raise NotFound()
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at"])
        return Response(notification_out(n))


class ReadAllView(ListView):
    """POST /notifications/read-all → {updated}"""

    def post(self, request):
        updated = request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"updated": updated}, status=status.HTTP_200_OK)

"""In-app notifications with a best-effort email copy (status tracked per notification)."""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from ..models import EmailStatus, Notification

logger = logging.getLogger("core.notify")


def deliver_email(n: Notification):
    """Try to email a notification; records sent/failed and the attempt count."""
    n.email_attempts += 1
    try:
        link = n.data.get("deeplink") if isinstance(n.data, dict) else None
        body = n.body + (f"\n\nBuka: {settings.FRONTEND_URL}{link}" if link else "")
        send_mail(n.title, body, settings.DEFAULT_FROM_EMAIL, [n.user.email], fail_silently=False)
        n.email_status = EmailStatus.SENT
        n.email_error = ""
    except Exception as exc:  # SMTP errors, timeouts, bounces reported synchronously
        logger.warning("Email notification %s failed: %s", n.id, exc)
        n.email_status = EmailStatus.FAILED
        n.email_error = str(exc)[:1000]
    n.save(update_fields=["email_status", "email_attempts", "email_error"])
    return n


def notify(user, type, title, body, data=None, send_email=True):
    if user is None or user.deleted_at is not None:
        return None
    n = Notification.objects.create(
        user=user,
        type=type,
        title=title[:150],
        body=body,
        data=data or {},
        email_status=EmailStatus.PENDING if (send_email and settings.NOTIFICATION_EMAILS) else EmailStatus.SKIPPED,
        created_at=timezone.now(),
    )
    if n.email_status == EmailStatus.PENDING:
        deliver_email(n)
    return n


def application_link(application, role):
    return {"deeplink": f"/kerja-sama/{application.id}", "role": role, "application_id": str(application.id)}

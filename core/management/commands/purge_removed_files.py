"""python manage.py purge_removed_files [--days 30] — delete storage for files removed by admins."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import UploadedFile


class Command(BaseCommand):
    help = "Delete the stored bytes of files removed by admins more than N days ago (rows are kept for the audit trail)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, days=30, **opts):
        cutoff = timezone.now() - timedelta(days=days)
        count = 0
        for f in UploadedFile.objects.filter(is_removed=True, removed_at__lt=cutoff).exclude(file=""):
            f.file.delete(save=False)
            f.file = ""
            f.save(update_fields=["file"])
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Purged {count} file(s)."))

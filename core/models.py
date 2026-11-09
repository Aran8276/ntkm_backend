"""
Data model for the UMKM × retail-store platform.

Conventions
- Primary keys are UUIDs (the admin panel shows the first 8 characters).
- "Soft delete" models keep rows with ``deleted_at`` set. ``objects`` only
  returns live rows; ``all_objects`` includes deleted ones (admin panel).
- Money is stored as integer rupiah.
"""
import uuid

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

ROLE_STORE = "store"
ROLE_UMKM = "umkm"
ROLES = (ROLE_STORE, ROLE_UMKM)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class AccountStatus(models.TextChoices):
    INVITED = "invited", "Diundang"
    ACTIVE = "active", "Aktif"
    SUSPENDED = "suspended", "Disuspend"


class AdminLevel(models.TextChoices):
    SUPERADMIN = "superadmin", "Superadmin"
    STAFF = "staff", "Staff"


class VerificationStatus(models.TextChoices):
    UNVERIFIED = "unverified", "Belum diisi"
    PENDING = "pending", "Menunggu"
    VERIFIED = "verified", "Terverifikasi"
    REJECTED = "rejected", "Ditolak"


class LegalKind(models.TextChoices):
    NIB = "nib", "NIB"
    PIRT = "pirt", "PIRT"
    HALAL = "halal", "Halal"


class OpportunityStatus(models.TextChoices):
    OPEN = "open", "Dibuka"
    FULL = "full", "Kuota penuh"
    CLOSED = "closed", "Ditutup"


class ApplicationStatus(models.TextChoices):
    SUBMITTED = "submitted", "Diajukan"
    APPROVED = "approved", "Disetujui"
    IN_DELIVERY = "in_delivery", "Dalam pengiriman"
    RECEIVED = "received", "Diterima toko"
    COMPLETED = "completed", "Selesai"
    REJECTED = "rejected", "Ditolak"
    CANCELLED = "cancelled", "Dibatalkan"


ACTIVE_APPLICATION_STATUSES = (
    ApplicationStatus.SUBMITTED,
    ApplicationStatus.APPROVED,
    ApplicationStatus.IN_DELIVERY,
    ApplicationStatus.RECEIVED,
)


class Decision(models.TextChoices):
    PENDING = "pending", "Menunggu"
    APPROVED = "approved", "Disetujui"
    REJECTED = "rejected", "Ditolak"


class StockMode(models.TextChoices):
    COUNT = "count", "Catat jumlah"
    BOOLEAN = "boolean", "Ada/habis"


class StockStatus(models.TextChoices):
    AVAILABLE = "available", "Ada"
    OUT_OF_STOCK = "out_of_stock", "Habis"


class FilePurpose(models.TextChoices):
    PRODUCT_PHOTO = "product_photo", "Foto produk"
    STORE_PHOTO = "store_photo", "Foto toko"
    UMKM_LOGO = "umkm_logo", "Logo UMKM"
    LEGAL_DOCUMENT = "legal_document", "Dokumen legalitas"


class NotificationType(models.TextChoices):
    APPLICATION_SUBMITTED = "application_submitted", "Pengajuan baru"
    APPLICATION_APPROVED = "application_approved", "Pengajuan disetujui"
    APPLICATION_REJECTED = "application_rejected", "Pengajuan ditolak"
    APPLICATION_STATUS_CHANGED = "application_status_changed", "Status berubah"
    STOCK_OUT = "stock_out", "Stok habis"
    ADMIN_ACTION = "admin_action", "Tindakan admin"
    ANNOUNCEMENT = "announcement", "Pengumuman"
    LEGAL_VERIFIED = "legal_verified", "Legalitas terverifikasi"
    LEGAL_REJECTED = "legal_rejected", "Legalitas ditolak"


class EmailStatus(models.TextChoices):
    PENDING = "pending", "Menunggu"
    SENT = "sent", "Terkirim"
    FAILED = "failed", "Gagal"
    SKIPPED = "skipped", "Tanpa email"


class AuditAction(models.TextChoices):
    CREATE = "create", "Dibuat"
    UPDATE = "update", "Diubah"
    DELETE = "delete", "Dihapus"
    RESTORE = "restore", "Dipulihkan"
    HARD_DELETE = "hard_delete", "Dihapus permanen"
    INVITE = "invite", "Diundang"
    SUSPEND = "suspend", "Disuspend"
    ACTIVATE = "activate", "Diaktifkan"
    GRANT_ADMIN = "grant_admin", "Diangkat admin"
    REVOKE_ADMIN = "revoke_admin", "Akses admin dicabut"
    CLOSE = "close", "Ditutup"
    REOPEN = "reopen", "Dibuka ulang"
    FORCE_STATUS = "force_status", "Status diubah paksa"
    STOCK_CORRECTION = "stock_correction", "Stok dikoreksi"
    VERIFY_LEGAL = "verify_legal", "Legalitas diputuskan"
    REMOVE_FILE = "remove_file", "File dihapus"
    RESEND_EMAIL = "resend_email", "Email dikirim ulang"
    BROADCAST = "broadcast", "Pengumuman dikirim"
    EXPORT = "export", "Data diekspor"


class TargetType(models.TextChoices):
    USER = "user", "Pengguna"
    STORE = "store", "Toko"
    UMKM = "umkm", "UMKM"
    OPPORTUNITY = "opportunity", "Peluang"
    PRODUCT = "product", "Produk"
    APPLICATION = "application", "Pengajuan"
    CATEGORY = "category", "Kategori"
    FILE = "file", "File"
    NOTIFICATION = "notification", "Notifikasi"


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AllManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    pass


class SoftDeleteModel(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = AliveManager()
    all_objects = AllManager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def soft_delete(self, by=None):
        self.deleted_at = timezone.now()
        self.deleted_by = by
        self.save(update_fields=["deleted_at", "deleted_by"])

    def restore(self):
        self.deleted_at = None
        self.deleted_by = None
        self.save(update_fields=["deleted_at", "deleted_by"])


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserManager(BaseUserManager):
    use_in_migrations = True

    def get_queryset(self):
        return super().get_queryset()

    def alive(self):
        return self.get_queryset().filter(deleted_at__isnull=True)

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email wajib diisi.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_admin", True)
        extra.setdefault("admin_role", AdminLevel.SUPERADMIN)
        extra.setdefault("status", AccountStatus.ACTIVE)
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Platform account. Signs in with Google; ``is_staff`` only gates the Django admin site."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    first_name = None
    last_name = None
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    avatar_url = models.URLField(blank=True)
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    roles = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=10, choices=AccountStatus.choices, default=AccountStatus.ACTIVE)
    is_admin = models.BooleanField(default=False)
    admin_role = models.CharField(max_length=10, choices=AdminLevel.choices, null=True, blank=True)
    invited_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_reason = models.TextField(blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.email

    # Profiles (live only) ---------------------------------------------------
    @property
    def store(self):
        p = getattr(self, "store_profile", None)
        return p if p is not None and p.deleted_at is None else None

    @property
    def umkm(self):
        p = getattr(self, "umkm_profile", None)
        return p if p is not None and p.deleted_at is None else None

    @property
    def is_superadmin(self):
        return self.is_admin and self.admin_role == AdminLevel.SUPERADMIN

    @property
    def is_deleted(self):
        return self.deleted_at is not None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def upload_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"uploads/{instance.purpose}/{timezone.now():%Y/%m}/{instance.id}.{ext}"


class UploadedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="files")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    purpose = models.CharField(max_length=20, choices=FilePurpose.choices)
    file = models.FileField(upload_to=upload_path)
    original_name = models.CharField(max_length=255)
    mime = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    is_removed = models.BooleanField(default=False)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.original_name


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


class StoreProfile(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="store_profile")
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20)
    address = models.TextField()
    lat = models.FloatField()
    lng = models.FloatField()
    operating_hours = models.JSONField(default=list)
    photo = models.ForeignKey(UploadedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class UmkmProfile(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="umkm_profile")
    business_name = models.CharField(max_length=100)
    owner_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20)
    description = models.TextField(blank=True)
    address = models.TextField()
    lat = models.FloatField()
    lng = models.FloatField()
    logo = models.ForeignKey(UploadedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    nib = models.CharField(max_length=13, blank=True)
    pirt_number = models.CharField(max_length=80, blank=True)
    halal_number = models.CharField(max_length=80, blank=True)
    legal_documents = models.ManyToManyField(UploadedFile, blank=True, related_name="legal_for")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.business_name

    LEGAL_FIELDS = {"nib": "nib", "pirt": "pirt_number", "halal": "halal_number"}

    def legal_number(self, kind):
        return getattr(self, self.LEGAL_FIELDS[kind]) or None

    def verification(self, kind):
        v = next((x for x in self.legal_verifications.all() if x.kind == kind), None)
        if v is None:
            v = LegalVerification(umkm=self, kind=kind)
        return v


class LegalVerification(models.Model):
    """Admin decision for one legal document number (NIB, PIRT, Halal) of a UMKM."""

    umkm = models.ForeignKey(UmkmProfile, on_delete=models.CASCADE, related_name="legal_verifications")
    kind = models.CharField(max_length=5, choices=LegalKind.choices)
    status = models.CharField(max_length=10, choices=VerificationStatus.choices, default=VerificationStatus.UNVERIFIED)
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["umkm", "kind"], name="uniq_legal_verification")]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Product(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    umkm = models.ForeignKey(UmkmProfile, on_delete=models.CASCADE, related_name="products")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    base_price = models.PositiveIntegerField()
    suggested_retail_price = models.PositiveIntegerField()
    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ProductPhoto(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="photos")
    file = models.ForeignKey(UploadedFile, on_delete=models.CASCADE, related_name="product_photos")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position"]


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


class Opportunity(SoftDeleteModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(StoreProfile, on_delete=models.CASCADE, related_name="opportunities")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    categories = models.ManyToManyField(Category, related_name="opportunities")
    requirements = models.JSONField(default=dict, blank=True)
    quota = models.PositiveIntegerField(default=1)
    quota_used = models.PositiveIntegerField(default=0)
    max_products_per_umkm = models.PositiveIntegerField(default=3)
    commission = models.JSONField(default=dict)
    receiving_schedule = models.JSONField(default=list)
    status = models.CharField(max_length=10, choices=OpportunityStatus.choices, default=OpportunityStatus.OPEN)
    valid_until = models.DateField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "opportunities"

    def __str__(self):
        return self.title

    @property
    def quota_remaining(self):
        return max(self.quota - self.quota_used, 0)

    @property
    def is_expired(self):
        return self.valid_until < timezone.localdate()

    def sync_status_with_quota(self):
        """open ↔ full follows the quota; closed stays closed."""
        if self.status == OpportunityStatus.CLOSED:
            return
        self.status = OpportunityStatus.FULL if self.quota_used >= self.quota else OpportunityStatus.OPEN


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


class Application(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="applications")
    umkm = models.ForeignKey(UmkmProfile, on_delete=models.CASCADE, related_name="applications")
    status = models.CharField(max_length=12, choices=ApplicationStatus.choices, default=ApplicationStatus.SUBMITTED, db_index=True)
    note = models.TextField(blank=True)
    review_note = models.TextField(blank=True)
    stock_mode = models.CharField(max_length=8, choices=StockMode.choices, default=StockMode.COUNT)
    # Commission is frozen when the application is created (later edits only affect new ones).
    commission = models.JSONField(default=dict)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_start = models.TimeField(null=True, blank=True)
    delivery_end = models.TimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.umkm} → {self.opportunity}"

    @property
    def store(self):
        return self.opportunity.store


class ApplicationItem(models.Model):
    """One product inside an application, with a snapshot of the product as submitted."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="application_items")
    # Snapshot
    product_name = models.CharField(max_length=100)
    category_name = models.CharField(max_length=80)
    base_price = models.PositiveIntegerField()
    suggested_retail_price = models.PositiveIntegerField()
    shelf_life_days = models.PositiveIntegerField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    photo = models.ForeignKey(UploadedFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Decision
    decision = models.CharField(max_length=8, choices=Decision.choices, default=Decision.PENDING)
    approved_quantity = models.PositiveIntegerField(null=True, blank=True)
    sell_price = models.PositiveIntegerField(null=True, blank=True)
    commission_per_unit = models.PositiveIntegerField(null=True, blank=True)
    umkm_income_per_unit = models.PositiveIntegerField(null=True, blank=True)
    reject_reason = models.TextField(blank=True)
    # Delivery & stock
    received_quantity = models.PositiveIntegerField(null=True, blank=True)
    stock_quantity = models.PositiveIntegerField(null=True, blank=True)
    stock_status = models.CharField(max_length=12, choices=StockStatus.choices, null=True, blank=True)
    stock_updated_at = models.DateTimeField(null=True, blank=True)
    stock_updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["id"]


class ApplicationEvent(models.Model):
    """Timeline entry. Admin overrides are flagged so the UI can highlight them."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=12, choices=ApplicationStatus.choices, null=True, blank=True)
    to_status = models.CharField(max_length=12, choices=ApplicationStatus.choices)
    label = models.CharField(max_length=120)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    actor_name = models.CharField(max_length=150)
    actor_role = models.CharField(max_length=8)  # store | umkm | admin
    note = models.TextField(blank=True)
    is_admin_override = models.BooleanField(default=False)
    admin_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]


# ---------------------------------------------------------------------------
# Notifications & audit
# ---------------------------------------------------------------------------


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    type = models.CharField(max_length=30, choices=NotificationType.choices)
    title = models.CharField(max_length=150)
    body = models.TextField()
    data = models.JSONField(default=dict, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    email_status = models.CharField(max_length=8, choices=EmailStatus.choices, default=EmailStatus.PENDING)
    email_attempts = models.PositiveSmallIntegerField(default=0)
    email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class AdminAuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="audit_logs")
    admin_name = models.CharField(max_length=150)
    action = models.CharField(max_length=20, choices=AuditAction.choices)
    target_type = models.CharField(max_length=15, choices=TargetType.choices)
    target_id = models.CharField(max_length=64, db_index=True)
    target_label = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    changes = models.JSONField(default=list, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

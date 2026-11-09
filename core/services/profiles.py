"""Create/update store & UMKM profiles and products (shared by the user app and admin forms)."""
from django.db import transaction

from ..exceptions import HasActiveRelations
from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    ROLE_STORE,
    ROLE_UMKM,
    Application,
    ApplicationItem,
    ApplicationStatus as S,
    FilePurpose,
    LegalVerification,
    Product,
    ProductPhoto,
    StoreProfile,
    UmkmProfile,
)
from .files import resolve_file, resolve_files
from .legal import KINDS, sync_after_owner_edit


def add_role(user, role):
    if role not in (user.roles or []):
        user.roles = [*(user.roles or []), role]
        user.save(update_fields=["roles"])


@transaction.atomic
def save_store_profile(user, data, uploader=None):
    """Upsert the user's store profile. A previously deleted profile is restored and overwritten."""
    profile = StoreProfile.all_objects.filter(user=user).first() or StoreProfile(user=user)
    profile.name = data["name"]
    profile.phone = data["phone"]
    profile.address = data["address"]
    profile.lat = data["lat"]
    profile.lng = data["lng"]
    profile.operating_hours = data["operating_hours"]
    if "photo_file_id" in data:
        profile.photo = resolve_file(data["photo_file_id"], user, FilePurpose.STORE_PHOTO, "photo_file_id", uploader)
    profile.deleted_at = None
    profile.deleted_by = None
    profile.save()
    add_role(user, ROLE_STORE)
    return profile


@transaction.atomic
def save_umkm_profile(user, data, uploader=None, from_owner=True):
    """
    Upsert the user's UMKM profile. When the owner changes a legal number it goes
    back to "pending"; admin edits (``from_owner=False``) keep the verification status.
    """
    profile = UmkmProfile.all_objects.filter(user=user).first()
    is_new = profile is None
    if is_new:
        profile = UmkmProfile(user=user)
    before = {k: (profile.legal_number(k) if not is_new else "") for k in KINDS}
    for field in ("business_name", "owner_name", "phone", "address", "lat", "lng"):
        setattr(profile, field, data[field])
    profile.description = data.get("description", profile.description or "")
    legal = data.get("legal")
    if legal is not None:
        for key, attr in (("nib", "nib"), ("pirt_number", "pirt_number"), ("halal_number", "halal_number")):
            if key in legal:
                setattr(profile, attr, (legal[key] or "").strip())
    if "logo_file_id" in data:
        profile.logo = resolve_file(data["logo_file_id"], user, FilePurpose.UMKM_LOGO, "logo_file_id", uploader)
    profile.deleted_at = None
    profile.deleted_by = None
    profile.save()
    if "legal_document_file_ids" in data:
        docs = resolve_files(data["legal_document_file_ids"], user, FilePurpose.LEGAL_DOCUMENT, "legal_document_file_ids", max_count=5, uploader=uploader)
        profile.legal_documents.set(docs)
    if from_owner:
        sync_after_owner_edit(profile, before)
    else:
        for kind in KINDS:
            v, _ = LegalVerification.objects.get_or_create(umkm=profile, kind=kind)
            if not profile.legal_number(kind) and v.status != "unverified":
                v.status = "unverified"
                v.save(update_fields=["status"])
            elif profile.legal_number(kind) and v.status == "unverified":
                from django.utils import timezone

                v.status = "pending"
                v.submitted_at = timezone.now()
                v.save(update_fields=["status", "submitted_at"])
    add_role(user, ROLE_UMKM)
    return profile


def product_in_open_application(product, statuses=(S.SUBMITTED, S.APPROVED, S.IN_DELIVERY)):
    """Applications in ``statuses`` where this product is still pending or approved."""
    items = (
        ApplicationItem.objects.filter(product=product, application__status__in=statuses)
        .exclude(decision="rejected")
        .select_related("application")
    )
    return list({i.application_id: i.application for i in items}.values())


@transaction.atomic
def save_product(umkm, data, product=None, actor=None, uploader=None):
    """Create or update a product; ``data`` comes from ProductInput (category already resolved)."""
    creating = product is None
    if creating:
        product = Product(umkm=umkm, created_by=actor)
    mapping = {
        "name": "name",
        "description": "description",
        "tags": "tags",
        "base_price": "base_price",
        "suggested_retail_price": "suggested_retail_price",
        "shelf_life_days": "shelf_life_days",
        "expiry_date": "expiry_date",
    }
    for key, attr in mapping.items():
        if key in data:
            setattr(product, attr, data[key])
    if "category_id" in data:
        product.category = data["category_id"]
    if "is_active" in data:
        if not data["is_active"] and product.is_active and not creating:
            blocking = product_in_open_application(product, (S.SUBMITTED, S.APPROVED))
            if blocking:
                raise HasActiveRelations(
                    "Produk masih ada di pengajuan aktif.",
                    {"application_ids": [str(a.id) for a in blocking]},
                )
        product.is_active = data["is_active"]
    product.save()
    if "photo_file_ids" in data:
        files = resolve_files(data["photo_file_ids"], umkm.user, FilePurpose.PRODUCT_PHOTO, "photo_file_ids", max_count=5, min_count=1, uploader=uploader)
        product.photos.all().delete()
        ProductPhoto.objects.bulk_create([ProductPhoto(product=product, file=f, position=i) for i, f in enumerate(files)])
    return product


def role_has_active_applications(user, role):
    if role == ROLE_STORE and user.store:
        return Application.objects.filter(opportunity__store=user.store, status__in=ACTIVE_APPLICATION_STATUSES).exists()
    if role == ROLE_UMKM and user.umkm:
        return Application.objects.filter(umkm=user.umkm, status__in=ACTIVE_APPLICATION_STATUSES).exists()
    return False

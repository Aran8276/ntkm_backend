import io
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from core.authentication import issue_tokens
from core.models import (
    AccountStatus,
    AdminLevel,
    Category,
    LegalVerification,
    Opportunity,
    Product,
    StoreProfile,
    UmkmProfile,
    User,
    VerificationStatus,
)

HOURS = [{"day": d, "open": "07:00", "close": "21:00", "is_closed": d == "sun"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]
SCHEDULE = [{"day": d, "start": "08:00", "end": "10:00"} for d in ("mon", "tue", "wed", "thu", "fri", "sat")]


def png_bytes(color=(200, 100, 50)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, "PNG")
    return buf.getvalue()


def user(email, roles=(), **extra):
    return User.objects.create_user(email=email, name=email.split("@")[0].title(), roles=list(roles), status=AccountStatus.ACTIVE, **extra)


def admin(email="admin@test.id", level=AdminLevel.SUPERADMIN):
    return user(email, is_admin=True, admin_role=level)


def store(owner=None, name="Toko Uji", lat=-7.9467, lng=112.6159):
    owner = owner or user(f"store{StoreProfile.all_objects.count()}@test.id", ["store"])
    if "store" not in owner.roles:
        owner.roles = [*owner.roles, "store"]
        owner.save()
    return StoreProfile.objects.create(user=owner, name=name, phone="081234567890", address="Jl. Uji No. 1, Malang", lat=lat, lng=lng, operating_hours=HOURS)


def umkm(owner=None, name="UMKM Uji", nib="9120301234567", pirt="", halal="", verified=True, lat=-7.95, lng=112.62):
    owner = owner or user(f"umkm{UmkmProfile.all_objects.count()}@test.id", ["umkm"])
    if "umkm" not in owner.roles:
        owner.roles = [*owner.roles, "umkm"]
        owner.save()
    u = UmkmProfile.objects.create(user=owner, business_name=name, owner_name=owner.name, phone="081311122233",
                                   address="Jl. Produksi No. 2, Malang", lat=lat, lng=lng, nib=nib, pirt_number=pirt, halal_number=halal)
    for kind, number in (("nib", nib), ("pirt", pirt), ("halal", halal)):
        status = (VerificationStatus.VERIFIED if verified else VerificationStatus.PENDING) if number else VerificationStatus.UNVERIFIED
        LegalVerification.objects.create(umkm=u, kind=kind, status=status, submitted_at=timezone.now() if number else None)
    return u


def category(name="Camilan", slug=None, active=True):
    return Category.objects.get_or_create(name=name, defaults={"slug": slug or name.lower().replace(" ", "-").replace("&", "dan"), "is_active": active})[0]


def product(owner_umkm, cat=None, name="Keripik Tempe 100 g", base=9000, srp=12000, shelf=90, **extra):
    return Product.objects.create(umkm=owner_umkm, category=cat or category(), name=name, base_price=base,
                                  suggested_retail_price=srp, shelf_life_days=shelf, created_by=owner_umkm.user, **extra)


def opportunity(owner_store, cats=None, quota=3, requirements=None, commission=None, **extra):
    o = Opportunity.objects.create(
        store=owner_store, title=extra.pop("title", "Cari camilan lokal"), quota=quota,
        requirements={"requires_nib": False, "requires_pirt": False, "requires_halal": False, **(requirements or {})},
        commission=commission or {"type": "percent", "value": 15}, receiving_schedule=extra.pop("schedule", SCHEDULE),
        valid_until=timezone.localdate() + timedelta(days=30), created_by=owner_store.user, **extra,
    )
    o.categories.set(cats or [category()])
    return o


def client_for(u):
    c = APIClient()
    if u is not None:
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_tokens(u)['access']}")
    return c

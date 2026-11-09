"""
python manage.py seed_demo [--reset]

Creates demo data matching the frontend mocks: categories, a superadmin and a
staff admin, 4 stores, 5 UMKMs, products, opportunities and applications in
every status (driven through the real workflow so timelines, quota and
notifications are consistent).
"""
import io
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import (
    AccountStatus,
    AdminLevel,
    Application,
    Category,
    FilePurpose,
    LegalVerification,
    Opportunity,
    Product,
    ProductPhoto,
    StoreProfile,
    UmkmProfile,
    UploadedFile,
    User,
    VerificationStatus,
)
from core.services import workflow

CATEGORIES = ["Camilan", "Minuman", "Sambal & Bumbu", "Kue Kering", "Kopi & Teh", "Frozen Food", "Oleh-oleh"]


def hours(closed=("sun",), open_="07:00", close="21:00"):
    return [{"day": d, "open": open_, "close": close, "is_closed": d in closed} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")]


STORES = [
    ("rina.wulandari@contoh.id", "Rina Wulandari", "Toko Sumber Rejeki", "081234567890", "Jl. Soekarno Hatta No. 12, Lowokwaru, Malang", -7.9467, 112.6159, hours()),
    ("hendra.berkah@contoh.id", "Hendra Gunawan", "Minimarket Berkah Jaya", "081298765432", "Jl. MT Haryono No. 45, Dinoyo, Malang", -7.9400, 112.6100, hours((), "06:00", "22:00")),
    ("bu.sri.kelontong@contoh.id", "Sri Wahyuni", "Toko Kelontong Bu Sri", "085711223344", "Jl. Kalpataru No. 8, Jatimulyo, Malang", -7.9300, 112.6200, hours(("fri",))),
    ("yohanes.sinarmas@contoh.id", "Yohanes Kurniawan", "Swalayan Sinar Mas", "082199887766", "Jl. Borobudur No. 21, Blimbing, Malang", -7.9500, 112.6400, hours((), "08:00", "21:00")),
]

UMKMS = [
    ("rina.wulandari@contoh.id", "Dapur Mbok Darmi", "Rina Wulandari", "081234567890", "Camilan rumahan dari resep keluarga sejak 1998.",
     "Jl. Sanan Gg. 3 No. 17, Purwantoro, Malang", -7.9600, 112.6400, ("9120301234567", "", "ID35110000123450824")),
    ("agus.sanan@contoh.id", "Keripik Tempe Sanan", "Agus Salim", "081377788899", "Keripik tempe tipis renyah khas Sanan.",
     "Jl. Sanan No. 5, Malang", -7.9550, 112.6420, ("9120301234500", "2063573010123-25", "ID35110000123450001")),
    ("siti.rahma@contoh.id", "Sambal Bu Rudy KW", "Siti Rahma", "081355566677", "Sambal bawang dan sambal ijo dalam botol kaca.",
     "Jl. Ijen No. 30, Malang", -7.9700, 112.6200, ("9120301234511", "2063573010124-25", "")),
    ("bayu.arjuno@contoh.id", "Kopi Lereng Arjuno", "Bayu Pratama", "081322233344", "Kopi robusta petik merah dari lereng Arjuno.",
     "Jl. Raya Karangploso No. 2, Malang", -7.9000, 112.6000, ("9120301234522", "", "")),
    ("ummi.hana@contoh.id", "Kue Kering Ummi Hana", "Hana Lestari", "081311122233", "Kue kering mentega untuk camilan harian.",
     "Jl. Bandung No. 4, Malang", -7.9650, 112.6150, ("", "2063573010125-25", "ID35110000123450002")),
]

PRODUCTS = {
    "Dapur Mbok Darmi": [
        ("Keripik Tempe Pedas 100 g", "Camilan", 9500, 12500, 90, ["pedas", "oleh-oleh"]),
        ("Keripik Singkong Balado 150 g", "Camilan", 10000, 13500, 60, ["balado"]),
        ("Sambal Bawang Mbok Darmi 150 ml", "Sambal & Bumbu", 18000, 25000, 45, ["sambal"]),
        ("Nastar Keju 250 g", "Kue Kering", 42000, 55000, 60, ["lebaran"]),
    ],
    "Keripik Tempe Sanan": [
        ("Keripik Tempe Original 100 g", "Camilan", 9000, 12000, 90, []),
        ("Keripik Tempe Rumput Laut 100 g", "Camilan", 9500, 12500, 90, []),
    ],
    "Sambal Bu Rudy KW": [("Sambal Ijo 200 ml", "Sambal & Bumbu", 20000, 27000, 30, ["pedas"])],
    "Kopi Lereng Arjuno": [
        ("Kopi Robusta Bubuk 200 g", "Kopi & Teh", 32000, 42000, 180, ["kopi"]),
        ("Kopi Arabika Biji 250 g", "Kopi & Teh", 58000, 75000, 180, ["kopi", "arabika"]),
    ],
    "Kue Kering Ummi Hana": [("Kastengel 250 g", "Kue Kering", 45000, 60000, 60, ["keju"])],
}


def png(color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (480, 480), color).save(buf, "PNG")
    return buf.getvalue()


class Command(BaseCommand):
    help = "Seed demo data for local development."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing demo data first.")

    def handle(self, *args, reset=False, **opts):
        if User.objects.filter(email="admin@ritelhub.local").exists() and not reset:
            self.stdout.write(self.style.WARNING("Demo data already exists. Use --reset to recreate it."))
            return
        emails_on = settings.NOTIFICATION_EMAILS
        settings.NOTIFICATION_EMAILS = False
        try:
            with transaction.atomic():
                if reset:
                    self.reset()
                self.seed()
        finally:
            settings.NOTIFICATION_EMAILS = emails_on
        self.stdout.write(self.style.SUCCESS("Demo data ready. Admin: admin@ritelhub.local (password: admin12345)."))

    def reset(self):
        Application.objects.all().delete()
        Opportunity.all_objects.all().delete()
        Product.all_objects.all().delete()
        UploadedFile.objects.all().delete()
        User.objects.all().delete()
        Category.objects.all().delete()

    def user(self, email, name, **extra):
        u, _ = User.objects.get_or_create(email=email, defaults={"name": name, "status": AccountStatus.ACTIVE, **extra})
        if not u.last_login:
            u.last_login = timezone.now() - timedelta(hours=3)
            u.save(update_fields=["last_login"])
        return u

    def seed(self):
        cats = {}
        for i, name in enumerate(CATEGORIES):
            from django.utils.text import slugify

            cats[name], _ = Category.objects.get_or_create(name=name, defaults={"slug": slugify(name.replace("&", "dan")), "sort_order": (i + 1) * 10})
        Category.objects.get_or_create(name="Kerajinan", defaults={"slug": "kerajinan", "is_active": False, "sort_order": 80})

        admin = User.objects.create_superuser("admin@ritelhub.local", "admin12345", name="Aran Pratama")
        staff = self.user("nadia@ritelhub.local", "Nadia Putri", is_admin=True, admin_role=AdminLevel.STAFF)
        self.user("dewi.anggraini@contoh.id", "Dewi Anggraini", status=AccountStatus.INVITED, roles=["umkm"], invited_by=staff)

        stores = {}
        for email, name, store_name, phone, address, lat, lng, oh in STORES:
            u = self.user(email, name)
            u.roles = sorted({*u.roles, "store"})
            u.save(update_fields=["roles"])
            photo = UploadedFile(owner=u, uploaded_by=u, purpose=FilePurpose.STORE_PHOTO, original_name="toko.png", mime="image/png", size=1)
            photo.file.save("toko.png", ContentFile(png((40, 110, 140))), save=False)
            photo.size = photo.file.size
            photo.save()
            stores[store_name] = StoreProfile.objects.create(user=u, name=store_name, phone=phone, address=address, lat=lat, lng=lng, operating_hours=oh, photo=photo)

        umkms = {}
        for email, business, owner, phone, desc, address, lat, lng, (nib, pirt, halal) in UMKMS:
            u = self.user(email, owner)
            u.roles = sorted({*u.roles, "umkm"}, reverse=True)
            u.save(update_fields=["roles"])
            m = UmkmProfile.objects.create(user=u, business_name=business, owner_name=owner, phone=phone, description=desc,
                                           address=address, lat=lat, lng=lng, nib=nib, pirt_number=pirt, halal_number=halal)
            now = timezone.now()
            for kind, number in (("nib", nib), ("pirt", pirt), ("halal", halal)):
                status = VerificationStatus.UNVERIFIED
                if number:
                    status = VerificationStatus.PENDING if business in ("Dapur Mbok Darmi", "Kue Kering Ummi Hana") and kind != "nib" else VerificationStatus.VERIFIED
                LegalVerification.objects.create(
                    umkm=m, kind=kind, status=status,
                    submitted_at=now - timedelta(days=2) if number else None,
                    decided_by=admin if status == VerificationStatus.VERIFIED else None,
                    decided_at=now - timedelta(days=1) if status == VerificationStatus.VERIFIED else None,
                )
            umkms[business] = m

        palette = [(232, 170, 80), (200, 90, 60), (120, 160, 90), (150, 110, 70), (90, 70, 50)]
        products = {}
        for business, rows in PRODUCTS.items():
            m = umkms[business]
            for i, (name, cat, base, srp, shelf, tags) in enumerate(rows):
                p = Product.objects.create(umkm=m, category=cats[cat], name=name, tags=tags, base_price=base,
                                           suggested_retail_price=srp, shelf_life_days=shelf, created_by=m.user,
                                           description=f"{name} produksi {business}.")
                f = UploadedFile(owner=m.user, uploaded_by=m.user, purpose=FilePurpose.PRODUCT_PHOTO, original_name=f"produk-{i + 1}.png", mime="image/png", size=1)
                f.file.save(f"produk-{i + 1}.png", ContentFile(png(palette[i % len(palette)])), save=False)
                f.size = f.file.size
                f.save()
                ProductPhoto.objects.create(product=p, file=f, position=0)
                products[name] = p
        products["Rempeyek Kacang 200 g"] = Product.objects.create(
            umkm=umkms["Dapur Mbok Darmi"], category=cats["Camilan"], name="Rempeyek Kacang 200 g", base_price=11000,
            suggested_retail_price=15000, shelf_life_days=30, is_active=False, created_by=umkms["Dapur Mbok Darmi"].user,
        )

        valid = timezone.localdate() + timedelta(days=30)
        weekdays = ["mon", "tue", "wed", "thu", "fri", "sat"]

        def opp(store, title, categories, quota=3, commission=None, req=None, schedule=None):
            o = Opportunity.objects.create(
                store=stores[store], title=title, description=f"{title}. Rak dekat kasir, target pembeli keluarga dan pelajar.",
                requirements={"min_shelf_life_days": None, "max_retail_price": None, "requires_nib": False, "requires_pirt": False,
                              "requires_halal": False, "notes": "", **(req or {})},
                quota=quota, max_products_per_umkm=3, commission=commission or {"type": "percent", "value": 15},
                receiving_schedule=schedule or [{"day": d, "start": "08:00", "end": "10:00"} for d in weekdays],
                valid_until=valid, created_by=stores[store].user,
            )
            o.categories.set([cats[c] for c in categories])
            return o

        o1 = opp("Toko Sumber Rejeki", "Cari camilan lokal untuk rak dekat kasir", ["Camilan", "Kue Kering"], quota=5, req={"requires_nib": True})
        o2 = opp("Toko Sumber Rejeki", "Kopi bubuk & minuman kemasan lokal", ["Kopi & Teh", "Minuman"], quota=2, commission={"type": "fixed", "value": 3000})
        o3 = opp("Toko Sumber Rejeki", "Sambal botol untuk rak bumbu", ["Sambal & Bumbu"], quota=2, req={"min_shelf_life_days": 30})
        o4 = opp("Minimarket Berkah Jaya", "Camilan pedas untuk rak depan", ["Camilan"], quota=3, req={"max_retail_price": 15000})
        o5 = opp("Toko Kelontong Bu Sri", "Oleh-oleh khas Malang untuk wisatawan", ["Oleh-oleh", "Camilan"], quota=3, req={"requires_pirt": True})
        o6 = opp("Swalayan Sinar Mas", "Kue kering untuk rak premium", ["Kue Kering"], quota=2, req={"requires_halal": True})
        closed = opp("Minimarket Berkah Jaya", "Frozen food untuk freezer kasir", ["Frozen Food"], quota=2)
        closed.status = "closed"
        closed.save(update_fields=["status"])

        def first_slot(o):
            tomorrow = timezone.localdate() + timedelta(days=1)
            d, start, end = workflow.upcoming_slots(o.receiving_schedule, tomorrow)[0]
            return {"date": d.isoformat(), "start": start, "end": end}

        def apply(o, business, names, note=""):
            m = umkms[business]
            return workflow.create_application(actor=m.user, actor_role="umkm", umkm=m, opportunity=o, products=[products[n] for n in names], note=note)

        def approve(a, o, reject_rest=False):
            items = []
            for k, it in enumerate(a.items.all()):
                if reject_rest and k > 0:
                    items.append({"item_id": it.id, "decision": "rejected", "reject_reason": "Kategori sudah penuh"})
                else:
                    items.append({"item_id": it.id, "decision": "approved", "approved_quantity": 24, "sell_price": it.suggested_retail_price})
            return workflow.review(a, actor=o.store.user, actor_role="store", items=items, delivery_slot=first_slot(o), note="Bawa nota serah terima.")

        # submitted
        apply(o1, "Keripik Tempe Sanan", ["Keripik Tempe Original 100 g", "Keripik Tempe Rumput Laut 100 g"],
              "Kapasitas produksi 200 bungkus per minggu. Bisa antar Senin pagi.")
        # approved (one product rejected)
        a = apply(o2, "Kopi Lereng Arjuno", ["Kopi Robusta Bubuk 200 g", "Kopi Arabika Biji 250 g"])
        approve(a, o2, reject_rest=True)
        # in delivery
        a = apply(o4, "Dapur Mbok Darmi", ["Keripik Tempe Pedas 100 g", "Keripik Singkong Balado 150 g"])
        a = approve(a, o4)
        workflow.ship(a, actor=a.umkm.user, note="Dikirim lewat kurir Pak Joko.")
        # received, one item sold out
        a = apply(o3, "Sambal Bu Rudy KW", ["Sambal Ijo 200 ml"])
        a = approve(a, o3)
        workflow.ship(a, actor=a.umkm.user)
        a = workflow.receive(a, actor=o3.store.user)
        workflow.update_stock(a, a.items.first().id, actor=o3.store.user, data={"stock_quantity": 0})
        # received (stock available) then completed
        a = apply(o6, "Dapur Mbok Darmi", ["Nastar Keju 250 g"])
        a = approve(a, o6)
        workflow.ship(a, actor=a.umkm.user)
        workflow.receive(a, actor=o6.store.user)
        b = apply(o5, "Keripik Tempe Sanan", ["Keripik Tempe Original 100 g"])
        b = approve(b, o5)
        workflow.ship(b, actor=b.umkm.user)
        b = workflow.receive(b, actor=o5.store.user)
        workflow.complete(b, actor=o5.store.user, role="store", note="Penjualan pilot selesai.")
        # rejected
        a = apply(o4, "Keripik Tempe Sanan", ["Keripik Tempe Rumput Laut 100 g"])
        workflow.review(a, actor=o4.store.user, actor_role="store", items=[{"item_id": a.items.first().id, "decision": "rejected", "reject_reason": "Harga tidak sesuai"}])
        # cancelled after approval
        a = apply(o6, "Kue Kering Ummi Hana", ["Kastengel 250 g"])
        a = approve(a, o6)
        workflow.cancel(a, actor=a.umkm.user, role="umkm", reason="Produksi terhambat karena bahan baku terlambat.")

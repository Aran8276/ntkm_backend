"""
Input validation for the user app (and reused by the admin API).
Output shapes live in presenters.py.
"""
import re

from django.utils import timezone
from rest_framework import serializers

from .models import DAYS, Category, StockMode

PHONE_RE = re.compile(r"^(08\d{7,12}|\+62\d{8,13})$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def clean_phone(value):
    v = re.sub(r"[\s\-()]", "", value or "")
    if not PHONE_RE.match(v):
        raise serializers.ValidationError("Masukkan nomor yang diawali 08 atau +62.")
    return v


def validate_operating_hours(value):
    if not isinstance(value, list) or not value:
        raise serializers.ValidationError("Isi jam operasional 7 hari.")
    by_day, errors = {}, {}
    for i, row in enumerate(value):
        if not isinstance(row, dict) or row.get("day") not in DAYS:
            errors[str(i)] = "Hari tidak valid."
            continue
        closed = bool(row.get("is_closed"))
        open_, close = str(row.get("open") or "")[:5], str(row.get("close") or "")[:5]
        if not closed:
            if not (TIME_RE.match(open_) and TIME_RE.match(close)):
                errors[str(i)] = "Jam buka dan tutup wajib diisi (HH:MM)."
                continue
            if close <= open_:
                errors[str(i)] = "Jam tutup harus setelah jam buka."
                continue
        by_day[row["day"]] = {"day": row["day"], "open": open_ or "08:00", "close": close or "17:00", "is_closed": closed}
    if errors:
        raise serializers.ValidationError(errors)
    hours = [by_day.get(d, {"day": d, "open": "08:00", "close": "17:00", "is_closed": True}) for d in DAYS]
    if all(h["is_closed"] for h in hours):
        raise serializers.ValidationError("Minimal 1 hari buka.")
    return hours


def validate_schedule(value, operating_hours=None):
    if not isinstance(value, list) or not value:
        raise serializers.ValidationError("Tambahkan minimal 1 slot.")
    hours = {h["day"]: h for h in (operating_hours or [])}
    out, errors = [], {}
    for i, row in enumerate(value):
        if not isinstance(row, dict) or row.get("day") not in DAYS:
            errors[str(i)] = "Hari tidak valid."
            continue
        start, end = str(row.get("start") or "")[:5], str(row.get("end") or "")[:5]
        if not (TIME_RE.match(start) and TIME_RE.match(end)) or end <= start:
            errors[str(i)] = "Jam selesai harus setelah jam mulai."
            continue
        h = hours.get(row["day"])
        if operating_hours is not None and (h is None or h["is_closed"] or start < h["open"] or end > h["close"]):
            errors[str(i)] = "Toko tutup pada jam ini."
            continue
        out.append({"day": row["day"], "start": start, "end": end})
    if errors:
        raise serializers.ValidationError(errors)
    keys = [(s["day"], s["start"], s["end"]) for s in out]
    if len(keys) != len(set(keys)):
        raise serializers.ValidationError("Slot tidak boleh duplikat.")
    return sorted(out, key=lambda s: (DAYS.index(s["day"]), s["start"]))


def validate_commission(value):
    if not isinstance(value, dict) or value.get("type") not in ("percent", "fixed"):
        raise serializers.ValidationError({"type": "Pilih persentase atau nominal."})
    try:
        amount = int(value.get("value"))
    except (TypeError, ValueError):
        raise serializers.ValidationError({"value": "Isi besar komisi."})
    if value["type"] == "percent" and not 1 <= amount <= 50:
        raise serializers.ValidationError({"value": "Persentase 1–50%."})
    if value["type"] == "fixed" and amount <= 0:
        raise serializers.ValidationError({"value": "Nominal harus lebih dari 0."})
    return {"type": value["type"], "value": amount}


def resolve_category(value, active_only=True):
    qs = Category.objects.filter(is_active=True) if active_only else Category.objects.all()
    value = str(value or "")
    c = qs.filter(slug=value).first()
    if c is None:
        try:
            c = qs.filter(id=value).first()
        except Exception:
            c = None
    if c is None:
        raise serializers.ValidationError("Kategori tidak ditemukan atau tidak aktif.")
    return c


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


class LocationMixin(serializers.Serializer):
    address = serializers.CharField(max_length=500, error_messages={"blank": "Alamat wajib diisi.", "required": "Alamat wajib diisi."})
    lat = serializers.FloatField(min_value=-90, max_value=90, error_messages={"required": "Tandai lokasi di peta."})
    lng = serializers.FloatField(min_value=-180, max_value=180, error_messages={"required": "Tandai lokasi di peta."})


class StoreProfileInput(LocationMixin):
    name = serializers.CharField(min_length=3, max_length=100, error_messages={"min_length": "Nama toko 3–100 karakter."})
    phone = serializers.CharField(max_length=20)
    photo_file_id = serializers.UUIDField(required=False, allow_null=True)
    operating_hours = serializers.JSONField()

    def validate_phone(self, v):
        return clean_phone(v)

    def validate_operating_hours(self, v):
        return validate_operating_hours(v)


class LegalInput(serializers.Serializer):
    nib = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=13)
    pirt_number = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=80)
    halal_number = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=80)

    def validate_nib(self, v):
        v = (v or "").strip()
        if v and not re.fullmatch(r"\d{13}", v):
            raise serializers.ValidationError("NIB terdiri dari 13 angka.")
        return v


class UmkmProfileInput(LocationMixin):
    business_name = serializers.CharField(min_length=3, max_length=100, error_messages={"min_length": "Nama usaha 3–100 karakter."})
    owner_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20)
    description = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")
    logo_file_id = serializers.UUIDField(required=False, allow_null=True)
    legal = LegalInput(required=False)
    legal_document_file_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=5)

    def validate_phone(self, v):
        return clean_phone(v)


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def normalize_tags(tags):
    out = []
    for t in tags or []:
        t = re.sub(r"\s+", "-", str(t).strip().lower()).strip("-#")
        if not t:
            continue
        if len(t) > 30:
            raise serializers.ValidationError("Tagar maksimal 30 karakter.")
        if t not in out:
            out.append(t)
    if len(out) > 10:
        raise serializers.ValidationError("Maksimal 10 tagar.")
    return out


class ProductInput(serializers.Serializer):
    photo_file_ids = serializers.ListField(child=serializers.UUIDField(), max_length=5)
    name = serializers.CharField(min_length=3, max_length=100, error_messages={"min_length": "Nama produk 3–100 karakter."})
    category_id = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    tags = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    base_price = serializers.IntegerField(min_value=1, error_messages={"min_value": "Harga harus lebih dari 0."})
    suggested_retail_price = serializers.IntegerField(min_value=1, error_messages={"min_value": "Harga harus lebih dari 0."})
    shelf_life_days = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)

    def validate_photo_file_ids(self, v):
        if len(v) < 1:
            raise serializers.ValidationError("Tambahkan minimal 1 foto.")
        return v

    def validate_category_id(self, v):
        return resolve_category(v)

    def validate_tags(self, v):
        return normalize_tags(v)

    def validate(self, attrs):
        inst = self.instance
        base = attrs.get("base_price", getattr(inst, "base_price", None))
        srp = attrs.get("suggested_retail_price", getattr(inst, "suggested_retail_price", None))
        if base is not None and srp is not None and srp < base:
            raise serializers.ValidationError({"suggested_retail_price": "Harga jual tidak boleh lebih rendah dari harga setor."})
        if "shelf_life_days" in attrs or "expiry_date" in attrs or inst is None:
            days = attrs.get("shelf_life_days", getattr(inst, "shelf_life_days", None) if "expiry_date" not in attrs else None)
            exp = attrs.get("expiry_date", getattr(inst, "expiry_date", None) if "shelf_life_days" not in attrs else None)
            if bool(days) == bool(exp):
                raise serializers.ValidationError({"shelf_life_days": "Isi salah satu: masa simpan (hari) atau tanggal kedaluwarsa."})
            attrs["shelf_life_days"], attrs["expiry_date"] = (days, None) if days else (None, exp)
        return attrs


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------


class RequirementsInput(serializers.Serializer):
    min_shelf_life_days = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    max_retail_price = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    requires_nib = serializers.BooleanField(required=False, default=False)
    requires_pirt = serializers.BooleanField(required=False, default=False)
    requires_halal = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")


class OpportunityInput(serializers.Serializer):
    title = serializers.CharField(min_length=5, max_length=120, error_messages={"min_length": "Judul 5–120 karakter."})
    description = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")
    category_ids = serializers.ListField(child=serializers.CharField(), min_length=1, error_messages={"min_length": "Pilih minimal 1 kategori."})
    requirements = RequirementsInput(required=False)
    quota = serializers.IntegerField(min_value=1, max_value=50)
    max_products_per_umkm = serializers.IntegerField(min_value=1, max_value=10, required=False, default=3)
    commission = serializers.JSONField()
    receiving_schedule = serializers.JSONField()
    valid_until = serializers.DateField()

    def __init__(self, *args, operating_hours=None, **kwargs):
        self.operating_hours = operating_hours
        super().__init__(*args, **kwargs)

    def validate_category_ids(self, v):
        cats = []
        for x in v:
            c = resolve_category(x)
            if c not in cats:
                cats.append(c)
        return cats

    def validate_commission(self, v):
        return validate_commission(v)

    def validate_receiving_schedule(self, v):
        return validate_schedule(v, self.operating_hours)

    def validate_valid_until(self, v):
        inst = self.instance
        if v < timezone.localdate() and not (inst is not None and inst.valid_until == v):
            raise serializers.ValidationError("Tanggal tidak boleh sebelum hari ini.")
        return v

    def validate_quota(self, v):
        if self.instance is not None and v < self.instance.quota_used:
            raise serializers.ValidationError(f"Kuota tidak boleh kurang dari yang sudah terpakai ({self.instance.quota_used}).")
        return v


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


class ApplicationCreateInput(serializers.Serializer):
    opportunity_id = serializers.UUIDField()
    product_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, error_messages={"min_length": "Pilih minimal 1 produk."})
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")


class ReviewItemInput(serializers.Serializer):
    item_id = serializers.IntegerField()
    decision = serializers.ChoiceField(choices=["approved", "rejected"])
    approved_quantity = serializers.IntegerField(required=False, allow_null=True)
    sell_price = serializers.IntegerField(required=False, allow_null=True)
    reject_reason = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class SlotInput(serializers.Serializer):
    date = serializers.DateField()
    start = serializers.CharField()
    end = serializers.CharField()


class ReviewInput(serializers.Serializer):
    items = ReviewItemInput(many=True)
    delivery_slot = SlotInput(required=False, allow_null=True)
    stock_mode = serializers.ChoiceField(choices=StockMode.choices, required=False, default=StockMode.COUNT)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")

    def to_internal_value(self, data):
        # Accept "id" as an alias of "item_id" for each item.
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            data = {**data, "items": [({**i, "item_id": i.get("item_id", i.get("id"))} if isinstance(i, dict) else i) for i in data["items"]]}
        return super().to_internal_value(data)


class NoteInput(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")


class ReceiveItemInput(serializers.Serializer):
    item_id = serializers.IntegerField()
    received_quantity = serializers.IntegerField(min_value=0)


class ReceiveInput(NoteInput):
    items = ReceiveItemInput(many=True, required=False, default=list)


class CancelInput(serializers.Serializer):
    reason = serializers.CharField(max_length=500, error_messages={"blank": "Alasan pembatalan wajib diisi.", "required": "Alasan pembatalan wajib diisi."})

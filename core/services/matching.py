"""
Distance, product eligibility and the "badge kecocokan" (match score) shown in
the UMKM opportunity feed.

Score (0–100):
- 35  category: at least one active product is in a category the store wants
- 20  distance: ≤ 3 km full, within the chosen radius partial, beyond it low
- 25  legal: share of required documents (NIB/PIRT/Halal) the UMKM has
- 20  products: at least one product passes shelf-life / price / legal rules
Level: ≥ 75 high, ≥ 50 medium, otherwise low.
"""
import math
from datetime import date

from django.utils import timezone

from .legal import LABEL, has_legal


def haversine_km(lat1, lng1, lat2, lng2):
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def fmt_km(km):
    return f"{km:.1f}".replace(".", ",") + " km"


def shelf_days(product, today=None):
    """Remaining shelf life in days (from shelf_life_days, or days until expiry_date)."""
    if product.shelf_life_days is not None:
        return product.shelf_life_days
    if product.expiry_date:
        today = today or timezone.localdate()
        exp = product.expiry_date if isinstance(product.expiry_date, date) else date.fromisoformat(str(product.expiry_date))
        return (exp - today).days
    return None


def product_eligibility(opportunity, product, legal=None, category_ids=None):
    """(eligible, reasons, legal_reasons) for submitting ``product`` to ``opportunity``."""
    reasons, legal_reasons = [], []
    req = opportunity.requirements or {}
    if category_ids is None:
        category_ids = {c.id for c in opportunity.categories.all()}
    if not product.is_active:
        reasons.append("Produk diarsipkan")
    if product.category_id not in category_ids:
        reasons.append(f"Kategori {product.category.name} tidak dicari toko ini")
    min_shelf = req.get("min_shelf_life_days")
    if min_shelf:
        days = shelf_days(product)
        if days is not None and days < int(min_shelf):
            reasons.append(f"Masa simpan kurang dari {min_shelf} hari")
    max_price = req.get("max_retail_price")
    if max_price and product.suggested_retail_price > int(max_price):
        reasons.append("Harga jual melebihi batas toko")
    if legal is not None:
        for kind in ("nib", "pirt", "halal"):
            if req.get(f"requires_{kind}") and not legal[kind]:
                legal_reasons.append(f"Belum memiliki {LABEL[kind]}")
    all_reasons = reasons + legal_reasons
    return (not all_reasons, all_reasons, legal_reasons)


def compute_match(opportunity, umkm, products, distance_km, radius_km=None):
    req = opportunity.requirements or {}
    legal = has_legal(umkm)
    category_ids = {c.id for c in opportunity.categories.all()}
    categories = {c.id: c.name for c in opportunity.categories.all()}
    active = [p for p in products if p.is_active]
    reasons = []
    score = 0

    # Category
    matching = [p for p in active if p.category_id in category_ids]
    if matching:
        score += 35
        reasons.append({"ok": True, "label": f"Kategori {categories[matching[0].category_id]} sesuai produk Anda"})
    else:
        reasons.append({"ok": False, "label": "Kategori yang dicari belum sesuai produk Anda"})

    # Distance
    favourite = radius_km or 5
    if distance_km is None:
        score += 10
    elif distance_km <= 3:
        score += 20
        reasons.append({"ok": True, "label": f"Jarak {fmt_km(distance_km)} dari lokasi usaha"})
    elif distance_km <= favourite:
        score += 14
        reasons.append({"ok": True, "label": f"Jarak {fmt_km(distance_km)} dari lokasi usaha"})
    else:
        score += 6
        reasons.append({"ok": False, "label": f"Jarak {fmt_km(distance_km)}, sedikit di luar radius favorit"})

    # Legal
    required = [k for k in ("nib", "pirt", "halal") if req.get(f"requires_{k}")]
    if required:
        met = [k for k in required if legal[k]]
        score += round(25 * len(met) / len(required))
        for k in required:
            reasons.append(
                {"ok": True, "label": f"Anda memiliki {LABEL[k]}"} if legal[k] else {"ok": False, "label": f"Toko mensyaratkan {LABEL[k].upper() if k != 'halal' else LABEL[k]}"}
            )
    else:
        score += 25
        owned = [k for k in ("nib", "pirt", "halal") if legal[k]]
        if owned:
            reasons.append({"ok": True, "label": f"Anda memiliki {LABEL[owned[0]]}"})

    # Products that pass every rule
    results = [product_eligibility(opportunity, p, legal, category_ids) for p in active]
    eligible = sum(1 for ok, _, _ in results if ok)
    if eligible:
        score += 20
        reasons.append({"ok": True, "label": f"{eligible} produk memenuhi syarat toko"})
    elif active:
        flat = [r for _, rs, _ in results for r in rs]
        if req.get("min_shelf_life_days") and any(r.startswith("Masa simpan") for r in flat):
            reasons.append({"ok": False, "label": f"Masa simpan produk Anda di bawah {req['min_shelf_life_days']} hari"})
        elif req.get("max_retail_price") and "Harga jual melebihi batas toko" in flat:
            reasons.append({"ok": False, "label": "Harga jual produk Anda melebihi batas toko"})
        else:
            reasons.append({"ok": False, "label": "Belum ada produk yang memenuhi syarat"})
    else:
        reasons.append({"ok": False, "label": "Anda belum punya produk aktif"})

    score = max(0, min(100, score))
    level = "high" if score >= 75 else "medium" if score >= 50 else "low"
    return {"level": level, "score": score, "reasons": reasons}

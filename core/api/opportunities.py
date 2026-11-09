from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from ..exceptions import BadRequest, Conflict, NotFound
from ..models import (
    ACTIVE_APPLICATION_STATUSES,
    AccountStatus,
    Application,
    Opportunity,
    OpportunityStatus,
    Product,
)
from ..permissions import IsStore, IsUmkm
from ..presenters import opportunity_out, product_out
from ..serializers import OpportunityInput
from ..services.legal import has_legal
from ..services.matching import compute_match, haversine_km, product_eligibility
from .common import ListView, parse_csv


def opportunity_queryset():
    return Opportunity.objects.select_related("store__photo").prefetch_related("categories")


def apply_opportunity_data(opp, data):
    """Copy validated OpportunityInput data onto an opportunity (categories set after save)."""
    for key in ("title", "description", "quota", "max_products_per_umkm", "commission", "receiving_schedule", "valid_until"):
        if key in data:
            setattr(opp, key, data[key])
    if "requirements" in data:
        req = dict(data["requirements"])
        opp.requirements = {**(opp.requirements or {}), **req}
    opp.sync_status_with_quota()


# ---------------------------------------------------------------------------
# Store: /me/opportunities
# ---------------------------------------------------------------------------


class MyOpportunityListView(ListView):
    """GET|POST /me/opportunities  (?status=open|full|closed)"""

    permission_classes = [IsStore]

    def get(self, request):
        qs = opportunity_queryset().filter(store=request.user.store)
        statuses = parse_csv(request.query_params.get("status"))
        if statuses:
            qs = qs.filter(status__in=statuses)
        return self.paged(qs, lambda o: opportunity_out(o, request))

    def post(self, request):
        store = request.user.store
        s = OpportunityInput(data=request.data, operating_hours=store.operating_hours)
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            opp = Opportunity(store=store, created_by=request.user)
            apply_opportunity_data(opp, s.validated_data)
            opp.save()
            opp.categories.set(s.validated_data["category_ids"])
        return Response(opportunity_out(opportunity_queryset().get(pk=opp.pk), request), status=status.HTTP_201_CREATED)


class MyOpportunityDetailView(ListView):
    """GET|PATCH /me/opportunities/{id}"""

    permission_classes = [IsStore]

    def get_opp(self, request, pk):
        o = opportunity_queryset().filter(store=request.user.store, pk=pk).first()
        if o is None:
            raise NotFound()
        return o

    def get(self, request, pk):
        return Response(opportunity_out(self.get_opp(request, pk), request))

    def patch(self, request, pk):
        opp = self.get_opp(request, pk)
        if opp.status == OpportunityStatus.CLOSED:
            raise Conflict("Peluang yang sudah ditutup tidak bisa diubah.")
        s = OpportunityInput(opp, data=request.data, partial=True, operating_hours=request.user.store.operating_hours)
        s.is_valid(raise_exception=True)
        with transaction.atomic():
            apply_opportunity_data(opp, s.validated_data)
            opp.save()
            if "category_ids" in s.validated_data:
                opp.categories.set(s.validated_data["category_ids"])
        return Response(opportunity_out(self.get_opp(request, pk), request))


class MyOpportunityCloseView(ListView):
    """POST /me/opportunities/{id}/close"""

    permission_classes = [IsStore]

    def post(self, request, pk):
        opp = opportunity_queryset().filter(store=request.user.store, pk=pk).first()
        if opp is None:
            raise NotFound()
        if opp.status == OpportunityStatus.CLOSED:
            raise Conflict("Peluang sudah ditutup.")
        opp.status = OpportunityStatus.CLOSED
        opp.save(update_fields=["status", "updated_at"])
        return Response(opportunity_out(opp, request))


# ---------------------------------------------------------------------------
# UMKM feed: /opportunities
# ---------------------------------------------------------------------------


def visible_opportunities():
    """Opportunities whose store and owner are still live and active."""
    return opportunity_queryset().filter(
        store__deleted_at__isnull=True,
        store__user__deleted_at__isnull=True,
        store__user__status=AccountStatus.ACTIVE,
    )


def my_latest_application(umkm, opp_ids):
    out = {}
    for a in Application.objects.filter(umkm=umkm, opportunity_id__in=opp_ids).order_by("created_at"):
        # Prefer an active application; otherwise the most recent one.
        cur = out.get(a.opportunity_id)
        if cur is None or a.status in ACTIVE_APPLICATION_STATUSES or cur.status not in ACTIVE_APPLICATION_STATUSES:
            out[a.opportunity_id] = a
    return out


def parse_float(value, field):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        raise BadRequest("Angka tidak valid.", {field: "Angka tidak valid."})


class FeedView(ListView):
    """GET /opportunities ?lat=&lng=&radius_km=&category=&match=&sort=match|distance|newest"""

    permission_classes = [IsUmkm]

    def get(self, request):
        umkm = request.user.umkm
        q = request.query_params
        lat = parse_float(q.get("lat"), "lat")
        lng = parse_float(q.get("lng"), "lng")
        if lat is None or lng is None:
            lat, lng = umkm.lat, umkm.lng
        radius = parse_float(q.get("radius_km"), "radius_km")
        cats = parse_csv(q.get("category"))
        levels = set(parse_csv(q.get("match")))
        sort = q.get("sort", "match")
        if sort not in ("match", "distance", "newest"):
            raise BadRequest("Urutan tidak dikenal.", {"sort": "Pilih match, distance, atau newest."})

        qs = visible_opportunities().filter(status=OpportunityStatus.OPEN, valid_until__gte=timezone.localdate())
        qs = qs.exclude(store__user=request.user)
        if cats:
            qs = qs.filter(categories__slug__in=cats).distinct()
        products = list(Product.objects.filter(umkm=umkm, is_active=True).select_related("category"))

        rows = []
        for o in qs:
            d = haversine_km(lat, lng, o.store.lat, o.store.lng)
            if radius is not None and d is not None and d > radius:
                continue
            m = compute_match(o, umkm, products, d, radius)
            if levels and m["level"] not in levels:
                continue
            rows.append((o, d, m))

        if sort == "distance":
            rows.sort(key=lambda r: (r[1] is None, r[1] or 0))
        elif sort == "newest":
            rows.sort(key=lambda r: r[0].created_at, reverse=True)
        else:
            rows.sort(key=lambda r: (-r[2]["score"], r[1] or 0))

        page = self.paginate_queryset(rows)
        mine = my_latest_application(umkm, [o.id for o, _, _ in page])
        results = [opportunity_out(o, request, distance_km=d, match=m, my_application=mine.get(o.id), counts=False) for o, d, m in page]
        return self.get_paginated_response(results)


class FeedDetailView(ListView):
    """GET /opportunities/{id} — includes my_products with eligibility and my_application."""

    permission_classes = [IsUmkm]

    def get(self, request, pk):
        umkm = request.user.umkm
        o = visible_opportunities().filter(pk=pk).first()
        if o is None:
            raise NotFound()
        d = haversine_km(umkm.lat, umkm.lng, o.store.lat, o.store.lng)
        products = list(
            Product.objects.filter(umkm=umkm).select_related("category").prefetch_related("photos__file").order_by("-is_active", "name")
        )
        legal = has_legal(umkm)
        cat_ids = {c.id for c in o.categories.all()}
        my_products = []
        for p in products:
            ok, reasons, _ = product_eligibility(o, p, legal, cat_ids)
            my_products.append({**product_out(p, request), "eligible": ok, "ineligible_reasons": reasons})
        mine = my_latest_application(umkm, [o.id]).get(o.id)
        data = opportunity_out(o, request, distance_km=d, match=compute_match(o, umkm, products, d), my_application=mine, counts=False)
        data["my_products"] = my_products
        data["is_available"] = o.status == OpportunityStatus.OPEN and not o.is_expired and o.quota_remaining > 0
        return Response(data)

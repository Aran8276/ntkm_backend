from django.db.models import Case, IntegerField, Value, When
from rest_framework import status
from rest_framework.response import Response

from ..exceptions import BadRequest, Conflict, Forbidden, NotFound
from ..models import (
    ROLE_STORE,
    ROLE_UMKM,
    Application,
    ApplicationItem,
    ApplicationStatus,
    Decision,
    Opportunity,
    Product,
    StockStatus,
)
from ..permissions import IsUmkm
from ..presenters import application_out, event_out, stock_row_out
from ..serializers import ApplicationCreateInput, CancelInput, NoteInput, ReceiveInput, ReviewInput
from ..services import workflow
from .common import ListView, parse_csv, viewer_role

ORDERINGS = {
    "-updated_at": ["-updated_at"],
    "updated_at": ["updated_at"],
    "-created_at": ["-created_at"],
    "created_at": ["created_at"],
    "delivery_slot_start": ["delivery_date", "delivery_start", "-updated_at"],
    "-delivery_slot_start": ["-delivery_date", "-delivery_start", "-updated_at"],
}


def application_queryset():
    return Application.objects.select_related(
        "opportunity__store__photo", "opportunity__store__user", "umkm__logo", "umkm__user"
    ).prefetch_related("items__photo", "umkm__legal_verifications")


def applications_for(user, role):
    qs = application_queryset()
    if role == ROLE_STORE:
        return qs.filter(opportunity__store=user.store)
    return qs.filter(umkm=user.umkm)


def get_for_party(request, pk):
    """The application plus the caller's side of it; 404 when the caller is not a party."""
    app = application_queryset().filter(pk=pk).first()
    if app is None:
        raise NotFound()
    user = request.user
    wanted = request.query_params.get("as")
    sides = []
    if user.umkm is not None and app.umkm_id == user.umkm.id:
        sides.append(ROLE_UMKM)
    if user.store is not None and app.opportunity.store_id == user.store.id:
        sides.append(ROLE_STORE)
    if not sides:
        raise NotFound()
    role = wanted if wanted in sides else sides[0]
    return app, role


def require_action(app, role, action):
    """403 when this side can never do the action; 409 when it can, but not in the current status."""
    if action in workflow.available_actions(app, role):
        return
    if any(action in by_role.get(role, []) for by_role in workflow.ACTIONS.values()):
        raise Conflict("Status pengajuan sudah berubah. Muat ulang halaman.", {"status": app.status})
    raise Forbidden("Aksi ini tidak tersedia untuk peran Anda.")


class ApplicationListView(ListView):
    """GET /applications ?as=&status=a,b&opportunity_id=&ordering=  ·  POST /applications (UMKM)"""

    def get(self, request):
        role = viewer_role(request)
        qs = applications_for(request.user, role)
        q = request.query_params
        statuses = parse_csv(q.get("status"))
        if statuses:
            bad = [s for s in statuses if s not in ApplicationStatus.values]
            if bad:
                raise BadRequest("Status tidak dikenal.", {"status": f"Status tidak dikenal: {', '.join(bad)}."})
            qs = qs.filter(status__in=statuses)
        if q.get("opportunity_id"):
            qs = qs.filter(opportunity_id=q["opportunity_id"])
        ordering = q.get("ordering", "-updated_at")
        if ordering not in ORDERINGS:
            raise BadRequest("Urutan tidak dikenal.", {"ordering": "Urutan tidak dikenal."})
        if ordering.endswith("delivery_slot_start"):
            # Applications without a slot go last.
            qs = qs.annotate(_no_slot=Case(When(delivery_date__isnull=True, then=Value(1)), default=Value(0), output_field=IntegerField()))
            qs = qs.order_by("_no_slot", *ORDERINGS[ordering])
        else:
            qs = qs.order_by(*ORDERINGS[ordering])
        return self.paged(qs, lambda a: application_out(a, role, request))

    def post(self, request):
        if not IsUmkm().has_permission(request, self):
            raise Forbidden(IsUmkm.message)
        s = ApplicationCreateInput(data=request.data)
        s.is_valid(raise_exception=True)
        umkm = request.user.umkm
        opp = Opportunity.objects.filter(pk=s.validated_data["opportunity_id"], store__deleted_at__isnull=True).first()
        if opp is None:
            raise NotFound("Peluang tidak ditemukan.")
        ids = s.validated_data["product_ids"]
        products = list(Product.objects.filter(pk__in=ids, umkm=umkm).select_related("category"))
        if len(products) != len(set(ids)):
            raise BadRequest("Produk tidak ditemukan.", {"product_ids": "Produk tidak ditemukan atau bukan milik Anda."})
        products.sort(key=lambda p: ids.index(p.id))
        app = workflow.create_application(
            actor=request.user, actor_role=ROLE_UMKM, umkm=umkm, opportunity=opp, products=products, note=s.validated_data["note"]
        )
        return Response(application_out(application_queryset().get(pk=app.pk), ROLE_UMKM, request), status=status.HTTP_201_CREATED)


class ApplicationDetailView(ListView):
    """GET /applications/{id}"""

    def get(self, request, pk):
        app, role = get_for_party(request, pk)
        return Response(application_out(app, role, request))


class ApplicationActionView(ListView):
    """POST /applications/{id}/(review|ship|receive|complete|cancel)"""

    workflow_action = None

    def post(self, request, pk):
        app, role = get_for_party(request, pk)
        require_action(app, role, self.workflow_action)
        user = request.user
        if self.workflow_action == "review":
            s = ReviewInput(data=request.data)
            s.is_valid(raise_exception=True)
            d = s.validated_data
            slot = d.get("delivery_slot")
            if slot:
                slot = {"date": slot["date"].isoformat(), "start": slot["start"], "end": slot["end"]}
            workflow.review(app, actor=user, actor_role=role, items=[dict(i) for i in d["items"]], delivery_slot=slot,
                            stock_mode=d["stock_mode"], note=d["note"])
        elif self.workflow_action == "ship":
            s = NoteInput(data=request.data)
            s.is_valid(raise_exception=True)
            workflow.ship(app, actor=user, note=s.validated_data["note"])
        elif self.workflow_action == "receive":
            s = ReceiveInput(data=request.data)
            s.is_valid(raise_exception=True)
            workflow.receive(app, actor=user, items=[dict(i) for i in s.validated_data["items"]], note=s.validated_data["note"])
        elif self.workflow_action == "complete":
            s = NoteInput(data=request.data)
            s.is_valid(raise_exception=True)
            workflow.complete(app, actor=user, role=role, note=s.validated_data["note"])
        elif self.workflow_action == "cancel":
            s = CancelInput(data=request.data)
            s.is_valid(raise_exception=True)
            workflow.cancel(app, actor=user, role=role, reason=s.validated_data["reason"])
        return Response(application_out(application_queryset().get(pk=app.pk), role, request))


class ApplicationTimelineView(ListView):
    """GET /applications/{id}/timeline (oldest → newest)"""

    pagination_class = None

    def get(self, request, pk):
        app, _ = get_for_party(request, pk)
        return Response([event_out(e) for e in app.events.all()])


class ItemStockView(ListView):
    """PATCH /applications/{id}/items/{item_id}/stock {stock_quantity} | {stock_status}"""

    def patch(self, request, pk, item_id):
        app, role = get_for_party(request, pk)
        if role != ROLE_STORE:
            raise Forbidden("Stok hanya bisa diubah oleh toko.")
        require_action(app, role, "update_stock")
        _, item = workflow.update_stock(app, item_id, actor=request.user, data=request.data)
        item = ApplicationItem.objects.select_related(
            "application__opportunity__store__photo", "application__umkm__logo", "photo"
        ).get(pk=item.pk)
        return Response(stock_row_out(item, role, request))


class StockListView(ListView):
    """GET /stock ?as=store|umkm&stock_status=available|out_of_stock — items on shelves (received)."""

    def get(self, request):
        role = viewer_role(request)
        qs = ApplicationItem.objects.filter(
            decision=Decision.APPROVED, application__status=ApplicationStatus.RECEIVED
        ).select_related("application__opportunity__store__photo", "application__umkm__logo", "photo")
        if role == ROLE_STORE:
            qs = qs.filter(application__opportunity__store=request.user.store)
        else:
            qs = qs.filter(application__umkm=request.user.umkm)
        st = request.query_params.get("stock_status")
        if st:
            if st not in StockStatus.values:
                raise BadRequest("Status stok tidak dikenal.", {"stock_status": "Pilih available atau out_of_stock."})
            qs = qs.filter(stock_status=st)
        qs = qs.annotate(
            _out=Case(When(stock_status=StockStatus.OUT_OF_STOCK, then=Value(0)), default=Value(1), output_field=IntegerField())
        ).order_by("_out", "-stock_updated_at", "id")
        return self.paged(qs, lambda i: stock_row_out(i, role, request))

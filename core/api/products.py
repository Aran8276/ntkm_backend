from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from ..exceptions import HasActiveRelations, NotFound
from ..models import Product
from ..permissions import IsUmkm
from ..presenters import product_out
from ..serializers import ProductInput
from ..services.profiles import product_in_open_application, save_product
from .common import ListView, parse_bool, parse_csv


def product_queryset(umkm):
    return (
        Product.objects.filter(umkm=umkm)
        .select_related("category")
        .prefetch_related("photos__file")
    )


class MyProductListView(ListView):
    """GET|POST /me/products  (?search=&category=<slug,...>&is_active=)"""

    permission_classes = [IsUmkm]

    def get(self, request):
        qs = product_queryset(request.user.umkm)
        q = request.query_params
        if q.get("search"):
            term = q["search"].strip()
            qs = qs.filter(Q(name__icontains=term) | Q(description__icontains=term) | Q(tags__icontains=term))
        cats = parse_csv(q.get("category"))
        if cats:
            qs = qs.filter(Q(category__slug__in=cats) | Q(category__id__in=[c for c in cats if len(c) == 36]))
        active = parse_bool(q.get("is_active"))
        if active is not None:
            qs = qs.filter(is_active=active)
        return self.paged(qs, lambda p: product_out(p, request))

    def post(self, request):
        s = ProductInput(data=request.data)
        s.is_valid(raise_exception=True)
        product = save_product(request.user.umkm, s.validated_data, actor=request.user)
        return Response(product_out(product_queryset(request.user.umkm).get(pk=product.pk), request), status=status.HTTP_201_CREATED)


class MyProductDetailView(ListView):
    """GET|PATCH|DELETE /me/products/{id}  (DELETE archives the product)"""

    permission_classes = [IsUmkm]

    def get_product(self, request, pk):
        p = product_queryset(request.user.umkm).filter(pk=pk).first()
        if p is None:
            raise NotFound()
        return p

    def get(self, request, pk):
        return Response(product_out(self.get_product(request, pk), request))

    def patch(self, request, pk):
        p = self.get_product(request, pk)
        s = ProductInput(p, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        save_product(request.user.umkm, s.validated_data, product=p)
        return Response(product_out(self.get_product(request, pk), request))

    def delete(self, request, pk):
        p = self.get_product(request, pk)
        blocking = product_in_open_application(p)
        if blocking:
            raise HasActiveRelations("Produk masih ada di pengajuan aktif.", {"application_ids": [str(a.id) for a in blocking]})
        p.is_active = False
        p.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

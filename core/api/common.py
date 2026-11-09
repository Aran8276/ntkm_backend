from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from ..exceptions import BadRequest
from ..models import ROLE_STORE, ROLE_UMKM


class ListView(APIView):
    """APIView with the GenericAPIView pagination helpers, plus ``paged`` to paginate and map rows to dicts."""

    pagination_class = api_settings.DEFAULT_PAGINATION_CLASS

    @property
    def paginator(self):
        if not hasattr(self, "_paginator"):
            self._paginator = self.pagination_class() if self.pagination_class else None
        return self._paginator

    def paginate_queryset(self, rows):
        if self.paginator is None:
            return None
        return self.paginator.paginate_queryset(rows, self.request, view=self)

    def get_paginated_response(self, data):
        return self.paginator.get_paginated_response(data)

    def paged(self, rows, to_dict):
        page = self.paginate_queryset(rows)
        if page is not None:
            return self.get_paginated_response([to_dict(r) for r in page])
        return Response([to_dict(r) for r in rows])


def parse_csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def viewer_role(request):
    """``as=store|umkm``; defaults to the only role the user has."""
    value = request.query_params.get("as")
    roles = request.user.roles or []
    if value is None:
        if len(roles) == 1:
            value = roles[0]
        else:
            raise BadRequest("Parameter as wajib diisi (store atau umkm).", {"as": "Pilih store atau umkm."})
    if value not in (ROLE_STORE, ROLE_UMKM):
        raise BadRequest("Parameter as harus store atau umkm.", {"as": "Pilih store atau umkm."})
    profile = request.user.store if value == ROLE_STORE else request.user.umkm
    if value not in roles or profile is None:
        from ..exceptions import Forbidden

        raise Forbidden("Halaman ini khusus untuk " + ("pemilik toko." if value == ROLE_STORE else "pelaku UMKM."))
    return value


def parse_bool(value):
    if value is None:
        return None
    return str(value).lower() in {"1", "true", "yes"}

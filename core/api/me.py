from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from ..exceptions import BadRequest, Conflict, Forbidden, NotFound
from ..models import ROLE_STORE, ROLE_UMKM, ROLES, Category, UploadedFile
from ..presenters import category_out, me_out, store_profile_out, umkm_profile_out, unread_count
from ..serializers import StoreProfileInput, UmkmProfileInput
from ..services.files import file_brief, validate_upload
from ..services.profiles import role_has_active_applications, save_store_profile, save_umkm_profile


class MeView(APIView):
    """GET /me"""

    def get(self, request):
        return Response(me_out(request.user, unread_count(request.user)))


class RolesView(APIView):
    """PUT /me/roles {roles: ["store"] | ["umkm"] | ["store","umkm"]}"""

    def put(self, request):
        roles = request.data.get("roles")
        if not isinstance(roles, list) or not roles or not set(roles) <= set(ROLES):
            raise BadRequest("Pilih minimal satu peran.", {"roles": "Pilih store, umkm, atau keduanya."})
        user = request.user
        for removed in set(user.roles or []) - set(roles):
            if role_has_active_applications(user, removed):
                label = "toko" if removed == ROLE_STORE else "UMKM"
                raise Conflict(f"Peran {label} masih punya pengajuan aktif. Selesaikan atau batalkan dulu.", {"role": removed})
        user.roles = [r for r in ROLES if r in roles]
        user.save(update_fields=["roles"])
        return Response(me_out(user, unread_count(user)))


class StoreProfileView(APIView):
    """GET|PUT /me/store-profile"""

    def get(self, request):
        if request.user.store is None:
            raise NotFound("Profil toko belum dibuat.")
        return Response(store_profile_out(request.user.store, request))

    def put(self, request):
        if ROLE_STORE not in (request.user.roles or []):
            raise Forbidden("Pilih peran toko terlebih dahulu.")
        s = StoreProfileInput(data=request.data)
        s.is_valid(raise_exception=True)
        profile = save_store_profile(request.user, s.validated_data)
        return Response(store_profile_out(profile, request))


class UmkmProfileView(APIView):
    """GET|PUT /me/umkm-profile"""

    def get(self, request):
        if request.user.umkm is None:
            raise NotFound("Profil UMKM belum dibuat.")
        return Response(umkm_profile_out(request.user.umkm, request))

    def put(self, request):
        if ROLE_UMKM not in (request.user.roles or []):
            raise Forbidden("Pilih peran UMKM terlebih dahulu.")
        s = UmkmProfileInput(data=request.data)
        s.is_valid(raise_exception=True)
        profile = save_umkm_profile(request.user, s.validated_data)
        return Response(umkm_profile_out(profile, request))


class UploadView(APIView):
    """POST /uploads (multipart: file, purpose) → {id, url, name, mime, size, purpose}"""

    parser_classes = [MultiPartParser, FormParser]
    throttle_scope = "uploads"

    def post(self, request):
        purpose = request.data.get("purpose")
        uploaded = request.FILES.get("file")
        mime = validate_upload(uploaded, purpose)
        f = UploadedFile(
            owner=request.user,
            uploaded_by=request.user,
            purpose=purpose,
            original_name=uploaded.name[:255],
            mime=mime,
            size=uploaded.size,
        )
        f.file.save(uploaded.name, uploaded, save=False)
        f.save()
        return Response(file_brief(f, request), status=status.HTTP_201_CREATED)


class CategoryListView(APIView):
    """GET /categories (active only, in admin-defined order)"""

    def get(self, request):
        return Response([category_out(c) for c in Category.objects.filter(is_active=True)])

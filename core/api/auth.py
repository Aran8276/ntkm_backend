import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from ..authentication import issue_tokens
from ..exceptions import AccountSuspended, ApiError, BadRequest, Forbidden
from ..models import AccountStatus
from ..presenters import me_out, unread_count

User = get_user_model()
logger = logging.getLogger("core.auth")


def verify_google_token(token):
    """Returns Google's claims or raises ValueError. Isolated so tests can patch it."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    info = id_token.verify_oauth2_token(token, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    if not info.get("email_verified"):
        raise ValueError("Email Google belum diverifikasi.")
    return info


def sign_in(user_info):
    """Find or create the account for verified Google claims; returns (user, created)."""
    email = user_info["email"].lower()
    sub = user_info.get("sub")
    with transaction.atomic():
        user = (User.objects.filter(google_sub=sub).first() if sub else None) or User.objects.filter(email__iexact=email).first()
        created = False
        if user is None:
            user = User.objects.create_user(
                email=email,
                name=user_info.get("name", ""),
                avatar_url=user_info.get("picture", ""),
                google_sub=sub,
                status=AccountStatus.ACTIVE,
            )
            created = True
        if user.deleted_at is not None:
            raise Forbidden("Akun ini sudah dihapus. Hubungi admin untuk memulihkannya.")
        if user.status == AccountStatus.SUSPENDED:
            raise AccountSuspended()
        if user.google_sub and sub and user.google_sub != sub:
            raise ApiError("Identitas Google tidak cocok dengan akun ini.", code="unauthenticated", status_code=401)
        if sub and not user.google_sub:
            user.google_sub = sub
        if user.status == AccountStatus.INVITED:
            user.status = AccountStatus.ACTIVE  # first sign-in accepts the invitation
        if not user.name:
            user.name = user_info.get("name", "")
        if user_info.get("picture"):
            user.avatar_url = user_info["picture"]
        user.last_login = timezone.now()
        user.save()
    return user, created


def token_response(user, created):
    return Response({**issue_tokens(user), "is_new_user": created, "user": me_out(user, unread_count(user))})


class GoogleLoginView(APIView):
    """POST /auth/google {id_token}"""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        token = request.data.get("id_token")
        if not token:
            raise BadRequest("id_token wajib diisi.", {"id_token": "id_token wajib diisi."})
        if not settings.GOOGLE_CLIENT_ID:
            raise ApiError("GOOGLE_CLIENT_ID belum diatur di server.", code="configuration_error", status_code=503)
        try:
            info = verify_google_token(token)
        except Exception as exc:
            logger.info("Google token rejected: %s", exc)
            raise ApiError("Gagal masuk dengan Google. Silakan coba lagi.", code="unauthenticated", status_code=401)
        user, created = sign_in(info)
        return token_response(user, created)


class DevLoginView(APIView):
    """POST /auth/dev-login {email, name?} — only when DEBUG and AUTH_DEV_LOGIN are on."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        if not settings.AUTH_DEV_LOGIN:
            raise ApiError("Endpoint tidak tersedia.", code="not_found", status_code=404)
        email = (request.data.get("email") or "").strip().lower()
        if "@" not in email:
            raise BadRequest("Email tidak valid.", {"email": "Email tidak valid."})
        user, created = sign_in({"email": email, "name": request.data.get("name") or email.split("@")[0], "sub": None})
        return token_response(user, created)


class RefreshView(TokenRefreshView):
    """POST /auth/token/refresh {refresh} → {access, refresh}"""

    throttle_scope = "auth"


class LogoutView(APIView):
    """POST /auth/logout {refresh} — blacklists the refresh token."""

    def post(self, request):
        token = request.data.get("refresh")
        if token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass  # already invalid: logging out is idempotent
        return Response(status=status.HTTP_204_NO_CONTENT)

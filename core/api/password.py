"""Email + password accounts: register, login, change password and reset by email."""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..authentication import issue_tokens, revoke_sessions
from ..exceptions import AccountSuspended, ApiError, BadRequest, Conflict, Forbidden
from ..models import AccountStatus
from ..presenters import me_out, unread_count
from .auth import token_response

User = get_user_model()
logger = logging.getLogger("core.auth")

MAX_PASSWORD_LENGTH = 128  # bounds the hashing cost of a single request


def invalid_credentials():
    # One message for "no such account", "no password set" and "wrong password".
    return ApiError("Email atau kata sandi salah.", code="invalid_credentials", status_code=401)


def clean_email(value):
    email = value.strip().lower() if isinstance(value, str) else ""
    try:
        validate_email(email)
    except ValidationError:
        raise BadRequest("Email tidak valid.", {"email": "Email tidak valid."})
    return email


def clean_new_password(value, user, field):
    """Runs AUTH_PASSWORD_VALIDATORS; ``user`` lets the similarity check see email/name."""
    if not isinstance(value, str) or not value:
        raise BadRequest("Kata sandi wajib diisi.", {field: "Kata sandi wajib diisi."})
    if len(value) > MAX_PASSWORD_LENGTH:
        message = f"Kata sandi maksimal {MAX_PASSWORD_LENGTH} karakter."
        raise BadRequest(message, {field: message})
    try:
        validate_password(value, user)
    except ValidationError as exc:
        raise BadRequest(exc.messages[0], {field: " ".join(exc.messages)})
    return value


class RegisterView(APIView):
    """POST /auth/register {email, password, name?} → 201 {access, refresh, is_new_user, user}"""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        email = clean_email(request.data.get("email"))
        name = str(request.data.get("name") or "").strip()[:150] or email.split("@")[0]
        password = clean_new_password(request.data.get("password"), User(email=email, name=name), "password")
        taken = Conflict(
            "Email sudah terdaftar. Silakan masuk atau atur ulang kata sandi.",
            {"email": "Email sudah terdaftar."},
            code="email_taken",
        )
        # Existing accounts (Google, invited, deleted) never get a password from here:
        # only the reset email, which proves ownership of the address, can set one.
        if User.objects.filter(email__iexact=email).exists():
            raise taken
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email, password=password, name=name, status=AccountStatus.ACTIVE, last_login=timezone.now()
                )
        except IntegrityError:
            raise taken
        response = token_response(user, True)
        response.status_code = 201
        return response


class LoginView(APIView):
    """POST /auth/login {email, password} → {access, refresh, is_new_user, user}"""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        email, password = request.data.get("email"), request.data.get("password")
        if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
            raise BadRequest("Email dan kata sandi wajib diisi.", {"email": "Wajib diisi.", "password": "Wajib diisi."})
        if len(password) > MAX_PASSWORD_LENGTH:
            raise invalid_credentials()
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None or not user.has_password:
            make_password(password)  # same hashing work as a real check, so timing reveals nothing
            raise invalid_credentials()
        if not user.check_password(password):  # also re-hashes old hashes to current Argon2id parameters
            raise invalid_credentials()
        if user.deleted_at is not None:
            raise Forbidden("Akun ini sudah dihapus. Hubungi admin untuk memulihkannya.")
        if user.status == AccountStatus.SUSPENDED:
            raise AccountSuspended()
        if not user.is_active:
            raise invalid_credentials()
        if user.status == AccountStatus.INVITED:
            user.status = AccountStatus.ACTIVE
        user.last_login = timezone.now()
        user.save(update_fields=["status", "last_login"])
        return token_response(user, False)


class ChangePasswordView(APIView):
    """
    POST /me/password {current_password?, new_password} → {access, refresh, user}

    ``current_password`` is required when the account already has a password;
    Google-only accounts (``has_password: false``) can set their first one without it.
    Every other session is signed out (refresh tokens revoked).
    """

    throttle_scope = "auth"

    def post(self, request):
        user = request.user
        if user.has_password:
            current = request.data.get("current_password")
            if not isinstance(current, str) or len(current) > MAX_PASSWORD_LENGTH or not user.check_password(current):
                raise BadRequest("Kata sandi saat ini salah.", {"current_password": "Kata sandi saat ini salah."})
        new = clean_new_password(request.data.get("new_password"), user, "new_password")
        user.set_password(new)
        user.save(update_fields=["password"])
        revoke_sessions(user)
        return Response({**issue_tokens(user), "user": me_out(user, unread_count(user))})


def reset_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{settings.FRONTEND_URL}{settings.PASSWORD_RESET_PATH}?uid={uid}&token={token}"


class ForgotPasswordView(APIView):
    """POST /auth/password/forgot {email} → always 200, whether or not the account exists."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        email = clean_email(request.data.get("email"))
        user = (
            User.objects.filter(email__iexact=email, deleted_at__isnull=True, is_active=True)
            .exclude(status=AccountStatus.SUSPENDED)
            .first()
        )
        if user is not None:
            minutes = settings.PASSWORD_RESET_TIMEOUT // 60
            body = (
                f"Halo {user.name or user.email},\n\n"
                "Kami menerima permintaan untuk mengatur ulang kata sandi akun Anda. "
                f"Buka tautan berikut (berlaku {minutes} menit, sekali pakai):\n\n{reset_link(user)}\n\n"
                "Abaikan email ini jika Anda tidak memintanya."
            )
            try:
                send_mail("Atur ulang kata sandi", body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
            except Exception as exc:
                logger.warning("Password reset email to user %s failed: %s", user.pk, exc)
        return Response({"detail": "Jika email terdaftar, tautan atur ulang kata sandi sudah dikirim."})


class ResetPasswordView(APIView):
    """POST /auth/password/reset {uid, token, new_password} → 200; then sign in with the new password."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    def post(self, request):
        invalid = BadRequest(
            "Tautan atur ulang tidak valid atau sudah kedaluwarsa.",
            {"token": "Tautan tidak valid atau kedaluwarsa."},
            code="invalid_token",
        )
        try:
            pk = force_str(urlsafe_base64_decode(str(request.data.get("uid") or "")))
            user = User.objects.filter(pk=pk, deleted_at__isnull=True).first()
        except (ValueError, ValidationError, UnicodeDecodeError):
            user = None
        if user is None or not default_token_generator.check_token(user, str(request.data.get("token") or "")):
            raise invalid
        new = clean_new_password(request.data.get("new_password"), user, "new_password")
        user.set_password(new)  # changing the hash also invalidates this (and any other) reset token
        user.save(update_fields=["password"])
        revoke_sessions(user)
        return Response({"detail": "Kata sandi berhasil diubah. Silakan masuk."})

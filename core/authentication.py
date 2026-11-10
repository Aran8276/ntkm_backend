from django.contrib.auth import get_user_model
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from .exceptions import AccountSuspended
from .models import AccountStatus


def check_account(user):
    """Reject deleted (401) and suspended (403 account_suspended) accounts."""
    if user is None or user.deleted_at is not None or not user.is_active:
        raise AuthenticationFailed("Akun tidak ditemukan.", code="user_not_found")
    if user.status == AccountStatus.SUSPENDED:
        raise AccountSuspended()
    return user


class PlatformJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        return check_account(super().get_user(validated_token))


class PlatformTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = RefreshToken(attrs["refresh"])
        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM)
        user = get_user_model().objects.filter(**{api_settings.USER_ID_FIELD: user_id}).first()
        check_account(user)
        return super().validate(attrs)


def revoke_sessions(user):
    """Blacklist every outstanding refresh token of ``user`` (password changed or reset)."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    for token in OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True):
        BlacklistedToken.objects.get_or_create(token=token)


def issue_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}

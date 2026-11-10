import re
from unittest import mock

from django.contrib.auth.hashers import identify_hasher, make_password
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings

from core.models import AccountStatus, User

from . import factories as f

API = "/api/v1"
GOOD = "Rahasia-Toko-2026"


class Base(TestCase):
    def setUp(self):
        cache.clear()

    def post(self, path, body, client=None):
        return (client or self.client).post(f"{API}{path}", body, content_type="application/json")

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["error"]["code"], code)

    def register(self, email="baru@contoh.id", password=GOOD, **extra):
        return self.post("/auth/register", {"email": email, "password": password, **extra})


class HashingTests(Base):
    def test_passwords_are_stored_as_argon2id_with_rfc9106_parameters(self):
        self.assertEqual(self.register().status_code, 201)
        encoded = User.objects.get(email="baru@contoh.id").password
        # Django prefixes the standard PHC string with its algorithm label.
        self.assertTrue(encoded.startswith("argon2$argon2id$v=19$m=65536,t=3,p=4$"), encoded)
        self.assertNotIn(GOOD, encoded)

    def test_old_pbkdf2_hash_still_works_and_is_upgraded_on_login(self):
        u = f.user("lama@contoh.id")
        u.password = make_password(GOOD, hasher="pbkdf2_sha256")
        u.save()
        self.assertEqual(identify_hasher(u.password).algorithm, "pbkdf2_sha256")
        r = self.post("/auth/login", {"email": "lama@contoh.id", "password": GOOD})
        self.assertEqual(r.status_code, 200, r.content)
        u.refresh_from_db()
        self.assertTrue(u.password.startswith("argon2$argon2id$"), u.password)


class RegisterLoginTests(Base):
    def test_register_then_login(self):
        r = self.register(email="  Baru@Contoh.ID ", name="Bu Sari")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertTrue(body["is_new_user"])
        self.assertEqual(body["user"]["email"], "baru@contoh.id")
        self.assertEqual(body["user"]["name"], "Bu Sari")
        self.assertTrue(body["user"]["has_password"])
        r = self.post("/auth/login", {"email": "BARU@contoh.id", "password": GOOD})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["is_new_user"])
        me = f.client_for(User.objects.get(email="baru@contoh.id")).get(f"{API}/me")
        self.assertEqual(me.status_code, 200)

    def test_register_rejects_weak_passwords_and_bad_email(self):
        for password in ("pendek1", "12345678901", "password", "baru@contoh.id", "", "x" * 129):
            r = self.register(password=password)
            self.assertError(r, 400, "validation_error")
            self.assertIn("password", r.json()["error"]["details"])
        self.assertError(self.register(email="bukan-email"), 400, "validation_error")
        self.assertFalse(User.objects.exists())

    def test_register_never_touches_an_existing_account(self):
        google_user = f.user("ada@contoh.id")
        invited = f.user("undangan@contoh.id")
        invited.status = AccountStatus.INVITED
        invited.save()
        for email in ("ada@contoh.id", "ADA@contoh.id", "undangan@contoh.id"):
            self.assertError(self.register(email=email), 409, "email_taken")
        google_user.refresh_from_db()
        invited.refresh_from_db()
        self.assertFalse(google_user.has_usable_password())
        self.assertFalse(invited.has_usable_password())

    def test_login_failures_share_one_error(self):
        self.register()
        f.user("google@contoh.id")  # exists but has no password
        for email, password in (("baru@contoh.id", "salah-sandi-1"), ("tidakada@contoh.id", GOOD), ("google@contoh.id", GOOD), ("baru@contoh.id", "x" * 129)):
            r = self.post("/auth/login", {"email": email, "password": password})
            self.assertError(r, 401, "invalid_credentials")
            self.assertEqual(r.json()["error"]["message"], "Email atau kata sandi salah.")
        self.assertError(self.post("/auth/login", {"email": "baru@contoh.id"}), 400, "validation_error")

    def test_suspended_and_deleted_accounts_cannot_login(self):
        self.register()
        u = User.objects.get(email="baru@contoh.id")
        u.status = AccountStatus.SUSPENDED
        u.save()
        self.assertError(self.post("/auth/login", {"email": u.email, "password": GOOD}), 403, "account_suspended")
        from django.utils import timezone

        u.status = AccountStatus.ACTIVE
        u.deleted_at = timezone.now()
        u.save()
        self.assertError(self.post("/auth/login", {"email": u.email, "password": GOOD}), 403, "permission_denied")

    @override_settings(GOOGLE_CLIENT_ID="test-client")
    def test_first_google_link_drops_an_unverified_password(self):
        # Someone registers the victim's address before the victim ever signs in.
        attacker = self.register(email="korban@contoh.id").json()
        info = {"email": "korban@contoh.id", "sub": "g-9", "name": "Korban", "email_verified": True}
        with mock.patch("core.api.auth.verify_google_token", return_value=info):
            r = self.post("/auth/google", {"id_token": "x"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["user"]["has_password"])
        self.assertError(self.post("/auth/login", {"email": "korban@contoh.id", "password": GOOD}), 401, "invalid_credentials")
        self.assertError(self.post("/auth/token/refresh", {"refresh": attacker["refresh"]}), 401, "unauthenticated")


class ChangePasswordTests(Base):
    def test_change_requires_current_password_and_signs_out_other_sessions(self):
        old = self.register().json()
        u = User.objects.get(email="baru@contoh.id")
        c = f.client_for(u)
        new = "Kunci-Baru-9988"
        r = c.post(f"{API}/me/password", {"current_password": "salah", "new_password": new}, format="json")
        self.assertError(r, 400, "validation_error")
        self.assertIn("current_password", r.json()["error"]["details"])
        r = c.post(f"{API}/me/password", {"current_password": GOOD, "new_password": "pendek"}, format="json")
        self.assertIn("new_password", r.json()["error"]["details"])
        r = c.post(f"{API}/me/password", {"current_password": GOOD, "new_password": new}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        fresh = r.json()
        self.assertError(self.post("/auth/token/refresh", {"refresh": old["refresh"]}), 401, "unauthenticated")
        self.assertEqual(self.post("/auth/token/refresh", {"refresh": fresh["refresh"]}).status_code, 200)
        self.assertError(self.post("/auth/login", {"email": u.email, "password": GOOD}), 401, "invalid_credentials")
        self.assertEqual(self.post("/auth/login", {"email": u.email, "password": new}).status_code, 200)

    def test_google_only_account_can_set_first_password(self):
        u = f.user("google@contoh.id")
        c = f.client_for(u)
        self.assertFalse(c.get(f"{API}/me").json()["has_password"])
        r = c.post(f"{API}/me/password", {"new_password": GOOD}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["user"]["has_password"])
        self.assertEqual(self.post("/auth/login", {"email": u.email, "password": GOOD}).status_code, 200)

    def test_account_with_empty_password_field_counts_as_having_none(self):
        # Rows created without a password (older seeds) store "" rather than an unusable marker.
        u = f.user("lama@contoh.id")
        User.objects.filter(pk=u.pk).update(password="")
        c = f.client_for(u)
        self.assertFalse(c.get(f"{API}/me").json()["has_password"])
        self.assertError(self.post("/auth/login", {"email": u.email, "password": ""}), 400, "validation_error")
        self.assertEqual(c.post(f"{API}/me/password", {"new_password": GOOD}, format="json").status_code, 200)
        self.assertEqual(self.post("/auth/login", {"email": u.email, "password": GOOD}).status_code, 200)

    def test_requires_authentication(self):
        self.assertError(self.post("/me/password", {"new_password": GOOD}), 401, "unauthenticated")


@override_settings(FRONTEND_URL="https://app.contoh.id")
class ResetPasswordTests(Base):
    def link_parts(self):
        match = re.search(r"https://app\.contoh\.id/reset-password\?uid=([\w-]+)&token=([\w-]+)", mail.outbox[-1].body)
        self.assertIsNotNone(match, mail.outbox[-1].body)
        return match.group(1), match.group(2)

    def test_forgot_does_not_reveal_whether_the_account_exists(self):
        f.user("ada@contoh.id")
        a = self.post("/auth/password/forgot", {"email": "ada@contoh.id"})
        b = self.post("/auth/password/forgot", {"email": "tidakada@contoh.id"})
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(a.json(), b.json())
        self.assertEqual([m.to for m in mail.outbox], [["ada@contoh.id"]])

    def test_reset_sets_password_once_and_signs_out_sessions(self):
        old = self.register().json()
        self.post("/auth/password/forgot", {"email": "baru@contoh.id"})
        uid, token = self.link_parts()
        new = "Kunci-Baru-9988"
        r = self.post("/auth/password/reset", {"uid": uid, "token": token, "new_password": "pendek"})
        self.assertError(r, 400, "validation_error")
        r = self.post("/auth/password/reset", {"uid": uid, "token": token, "new_password": new})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.post("/auth/login", {"email": "baru@contoh.id", "password": new}).status_code, 200)
        self.assertError(self.post("/auth/token/refresh", {"refresh": old["refresh"]}), 401, "unauthenticated")
        # The link is single use.
        again = self.post("/auth/password/reset", {"uid": uid, "token": token, "new_password": "Lain-Lagi-7766"})
        self.assertError(again, 400, "invalid_token")

    def test_reset_lets_an_invited_account_set_a_password(self):
        u = f.user("undangan@contoh.id", ["umkm"])
        u.status = AccountStatus.INVITED
        u.save()
        self.post("/auth/password/forgot", {"email": u.email})
        uid, token = self.link_parts()
        self.assertEqual(self.post("/auth/password/reset", {"uid": uid, "token": token, "new_password": GOOD}).status_code, 200)
        self.assertEqual(self.post("/auth/login", {"email": u.email, "password": GOOD}).status_code, 200)
        u.refresh_from_db()
        self.assertEqual(u.status, AccountStatus.ACTIVE)

    def test_bad_or_forged_links_are_rejected(self):
        u = f.user("ada@contoh.id")
        other = f.user("lain@contoh.id")
        self.post("/auth/password/forgot", {"email": other.email})
        _, other_token = self.link_parts()
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(u.pk))
        for body in ({"uid": uid, "token": other_token}, {"uid": uid, "token": "abc-def"}, {"uid": "!!!", "token": other_token}, {}):
            self.assertError(self.post("/auth/password/reset", {**body, "new_password": GOOD}), 400, "invalid_token")
        u.refresh_from_db()
        self.assertFalse(u.has_usable_password())

    def test_suspended_account_gets_no_reset_email(self):
        u = f.user("ada@contoh.id")
        u.status = AccountStatus.SUSPENDED
        u.save()
        self.assertEqual(self.post("/auth/password/forgot", {"email": u.email}).status_code, 200)
        self.assertEqual(mail.outbox, [])

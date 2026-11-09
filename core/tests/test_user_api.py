import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (
    AccountStatus,
    Notification,
    Opportunity,
)

from . import factories as f

API = "/api/v1"
MEDIA = tempfile.mkdtemp()


def next_slot(opp):
    from core.services.workflow import upcoming_slots

    d, start, end = upcoming_slots(opp.receiving_schedule, timezone.localdate() + timedelta(days=1))[0]
    return {"date": d.isoformat(), "start": start, "end": end}


class Base(TestCase):
    def setUp(self):
        cache.clear()

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json()["error"]["code"], code)


@override_settings(GOOGLE_CLIENT_ID="test-client")
class AuthTests(Base):
    def google(self, **claims):
        info = {"email": "baru@contoh.id", "sub": "g-1", "name": "Pengguna Baru", "email_verified": True, **claims}
        return mock.patch("core.api.auth.verify_google_token", return_value=info)

    def test_google_login_creates_user_and_refresh_works(self):
        with self.google():
            r = self.client.post(f"{API}/auth/google", {"id_token": "x"}, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["is_new_user"])
        self.assertEqual(body["user"]["roles"], [])
        self.assertEqual(body["user"]["onboarding"], {"store_completed": False, "umkm_completed": False})
        r = self.client.post(f"{API}/auth/token/refresh", {"refresh": body["refresh"]}, content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("access", r.json())

    def test_invalid_google_token(self):
        with mock.patch("core.api.auth.verify_google_token", side_effect=ValueError("bad")):
            r = self.client.post(f"{API}/auth/google", {"id_token": "x"}, content_type="application/json")
        self.assertError(r, 401, "unauthenticated")

    def test_invited_user_becomes_active_on_first_login(self):
        u = f.user("baru@contoh.id", ["umkm"])
        u.status = AccountStatus.INVITED
        u.save()
        with self.google():
            r = self.client.post(f"{API}/auth/google", {"id_token": "x"}, content_type="application/json")
        self.assertFalse(r.json()["is_new_user"])
        u.refresh_from_db()
        self.assertEqual(u.status, AccountStatus.ACTIVE)
        self.assertEqual(u.google_sub, "g-1")

    def test_suspended_user_rejected_everywhere(self):
        u = f.user("baru@contoh.id")
        c = f.client_for(u)
        u.status = AccountStatus.SUSPENDED
        u.save()
        with self.google():
            r = self.client.post(f"{API}/auth/google", {"id_token": "x"}, content_type="application/json")
        self.assertError(r, 403, "account_suspended")
        self.assertError(c.get(f"{API}/me"), 403, "account_suspended")

    def test_logout_blacklists_refresh(self):
        u = f.user("a@contoh.id")
        from core.authentication import issue_tokens

        tokens = issue_tokens(u)
        c = f.client_for(u)
        self.assertEqual(c.post(f"{API}/auth/logout", {"refresh": tokens["refresh"]}, format="json").status_code, 204)
        r = self.client.post(f"{API}/auth/token/refresh", {"refresh": tokens["refresh"]}, content_type="application/json")
        self.assertError(r, 401, "unauthenticated")

    def test_unauthenticated_envelope(self):
        self.assertError(self.client.get(f"{API}/me"), 401, "unauthenticated")


class ProfileTests(Base):
    def test_roles_and_store_onboarding(self):
        u = f.user("toko@contoh.id")
        c = f.client_for(u)
        r = c.put(f"{API}/me/roles", {"roles": ["store", "umkm"]}, format="json")
        self.assertEqual(r.json()["roles"], ["store", "umkm"])
        self.assertError(c.get(f"{API}/me/store-profile"), 404, "not_found")
        bad = c.put(f"{API}/me/store-profile", {"name": "Ab", "phone": "12345", "address": "", "operating_hours": []}, format="json")
        self.assertError(bad, 400, "validation_error")
        self.assertEqual(bad.json()["error"]["details"]["phone"], "Masukkan nomor yang diawali 08 atau +62.")
        r = c.put(f"{API}/me/store-profile", {
            "name": "Toko Sumber Rejeki", "phone": "0812 3456 7890", "address": "Jl. Soekarno Hatta 12",
            "lat": -7.9, "lng": 112.6, "operating_hours": f.HOURS,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["phone"], "081234567890")
        me = c.get(f"{API}/me").json()
        self.assertTrue(me["onboarding"]["store_completed"])
        self.assertFalse(me["onboarding"]["umkm_completed"])

    def test_umkm_profile_legal_goes_pending(self):
        u = f.user("umkm@contoh.id", ["umkm"])
        c = f.client_for(u)
        payload = {"business_name": "Dapur Mbok Darmi", "owner_name": "Rina", "phone": "+6281234567890", "address": "Jl. Sanan",
                   "lat": -7.96, "lng": 112.64, "legal": {"nib": "123"}}
        self.assertEqual(c.put(f"{API}/me/umkm-profile", payload, format="json").json()["error"]["details"]["legal.nib"], "NIB terdiri dari 13 angka.")
        payload["legal"]["nib"] = "9120301234567"
        r = c.put(f"{API}/me/umkm-profile", payload, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        legal = r.json()["legal"]
        self.assertTrue(legal["has_nib"])
        self.assertFalse(legal["has_pirt"])
        self.assertEqual(legal["verification"]["nib"], "pending")

    def test_cannot_drop_role_with_active_application(self):
        s = f.store()
        m = f.umkm()
        p = f.product(m)
        o = f.opportunity(s)
        from core.services import workflow

        workflow.create_application(actor=m.user, actor_role="umkm", umkm=m, opportunity=o, products=[p])
        r = f.client_for(m.user).put(f"{API}/me/roles", {"roles": ["store"]}, format="json")
        self.assertError(r, 409, "conflict")


@override_settings(MEDIA_ROOT=MEDIA)
class UploadAndProductTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def upload(self, c, purpose="product_photo", data=None, name="a.png"):
        return c.post(f"{API}/uploads", {"purpose": purpose, "file": SimpleUploadedFile(name, data or f.png_bytes(), "image/png")}, format="multipart")

    def test_upload_validates_type(self):
        m = f.umkm()
        c = f.client_for(m.user)
        self.assertEqual(self.upload(c).status_code, 201)
        r = self.upload(c, data=b"not an image at all", name="x.png")
        self.assertError(r, 400, "validation_error")
        self.assertIn("file", r.json()["error"]["details"])

    def test_product_crud_and_archive(self):
        m = f.umkm()
        c = f.client_for(m.user)
        cat = f.category()
        photo = self.upload(c).json()["id"]
        base = {"name": "Keripik Tempe Pedas 100 g", "category_id": cat.slug, "base_price": 9500, "tags": ["Oleh Oleh", "pedas"]}
        r = c.post(f"{API}/me/products", {**base, "photo_file_ids": [photo], "suggested_retail_price": 9000, "shelf_life_days": 90}, format="json")
        self.assertEqual(r.json()["error"]["details"]["suggested_retail_price"], "Harga jual tidak boleh lebih rendah dari harga setor.")
        r = c.post(f"{API}/me/products", {**base, "photo_file_ids": [], "suggested_retail_price": 12500, "shelf_life_days": 90}, format="json")
        self.assertIn("photo_file_ids", r.json()["error"]["details"])
        r = c.post(f"{API}/me/products", {**base, "photo_file_ids": [photo], "suggested_retail_price": 12500, "shelf_life_days": 90, "expiry_date": "2027-01-01"}, format="json")
        self.assertIn("shelf_life_days", r.json()["error"]["details"])
        r = c.post(f"{API}/me/products", {**base, "photo_file_ids": [photo], "suggested_retail_price": 12500, "shelf_life_days": 90}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        pid = r.json()["id"]
        self.assertEqual(r.json()["tags"], ["oleh-oleh", "pedas"])
        self.assertEqual(r.json()["photo_count"], 1)
        r = c.patch(f"{API}/me/products/{pid}", {"expiry_date": "2027-03-01"}, format="json")
        self.assertEqual(r.json()["shelf_life_days"], None)
        self.assertEqual(c.get(f"{API}/me/products?is_active=true").json()["count"], 1)
        self.assertEqual(c.delete(f"{API}/me/products/{pid}").status_code, 204)
        self.assertEqual(c.get(f"{API}/me/products?is_active=false").json()["count"], 1)
        self.assertTrue(c.patch(f"{API}/me/products/{pid}", {"is_active": True}, format="json").json()["is_active"])

    def test_archive_blocked_by_active_application(self):
        s, m = f.store(), f.umkm()
        p = f.product(m)
        from core.services import workflow

        workflow.create_application(actor=m.user, actor_role="umkm", umkm=m, opportunity=f.opportunity(s), products=[p])
        self.assertError(f.client_for(m.user).delete(f"{API}/me/products/{p.id}"), 409, "has_active_relations")

    def test_other_users_product_is_404(self):
        a, b = f.umkm(), f.umkm()
        p = f.product(a)
        self.assertError(f.client_for(b.user).get(f"{API}/me/products/{p.id}"), 404, "not_found")


class OpportunityTests(Base):
    def test_store_creates_and_schedule_must_fit_hours(self):
        s = f.store()
        c = f.client_for(s.user)
        cat = f.category()
        payload = {
            "title": "Cari camilan lokal", "category_ids": [str(cat.id)], "quota": 3,
            "commission": {"type": "percent", "value": 15},
            "receiving_schedule": [{"day": "sun", "start": "08:00", "end": "10:00"}],
            "valid_until": (timezone.localdate() + timedelta(days=30)).isoformat(),
        }
        r = c.post(f"{API}/me/opportunities", payload, format="json")
        self.assertEqual(r.json()["error"]["details"]["receiving_schedule.0"], "Toko tutup pada jam ini.")
        payload["receiving_schedule"] = f.SCHEDULE[:2]
        r = c.post(f"{API}/me/opportunities", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        oid = r.json()["id"]
        self.assertEqual(r.json()["application_counts"], {})
        r = c.post(f"{API}/me/opportunities/{oid}/close")
        self.assertEqual(r.json()["status"], "closed")
        self.assertError(c.patch(f"{API}/me/opportunities/{oid}", {"title": "Judul baru"}, format="json"), 409, "conflict")

    def test_feed_match_distance_and_filters(self):
        cam, kopi = f.category("Camilan"), f.category("Kopi & Teh")
        near = f.store(name="Dekat", lat=-7.951, lng=112.621)
        far = f.store(name="Jauh", lat=-7.80, lng=112.40)
        m = f.umkm(nib="9120301234567")
        f.product(m, cam)
        o_near = f.opportunity(near, [cam], requirements={"requires_nib": True})
        f.opportunity(far, [cam])
        o_kopi = f.opportunity(near, [kopi], title="Kopi lokal")
        expired = f.opportunity(near, [cam], title="Kedaluwarsa")
        Opportunity.objects.filter(pk=expired.pk).update(valid_until=timezone.localdate() - timedelta(days=1))
        c = f.client_for(m.user)
        r = c.get(f"{API}/opportunities?radius_km=5")
        ids = [x["id"] for x in r.json()["results"]]
        self.assertEqual(ids[0], str(o_near.id))  # best match first
        self.assertNotIn(str(expired.id), ids)
        self.assertEqual(len(ids), 2)  # far store filtered out by radius
        top = r.json()["results"][0]
        self.assertEqual(top["match"]["level"], "high")
        self.assertIn("distance_km", top["store"])
        self.assertTrue(any(x["label"] == "Anda memiliki NIB" for x in top["match"]["reasons"]))
        r = c.get(f"{API}/opportunities?category=kopi-dan-teh")
        self.assertEqual([x["id"] for x in r.json()["results"]], [str(o_kopi.id)])
        self.assertEqual(r.json()["results"][0]["match"]["level"] in ("medium", "low"), True)

    def test_feed_detail_eligibility(self):
        cam = f.category("Camilan")
        s, m = f.store(), f.umkm(nib="", pirt="")
        ok = f.product(m, cam, name="Keripik")
        pricey = f.product(m, cam, name="Mahal", base=20000, srp=30000)
        o = f.opportunity(s, [cam], requirements={"max_retail_price": 15000, "requires_pirt": True})
        data = f.client_for(m.user).get(f"{API}/opportunities/{o.id}").json()
        by_id = {p["id"]: p for p in data["my_products"]}
        self.assertFalse(by_id[str(ok.id)]["eligible"])
        self.assertIn("Belum memiliki PIRT", by_id[str(ok.id)]["ineligible_reasons"])
        self.assertIn("Harga jual melebihi batas toko", by_id[str(pricey.id)]["ineligible_reasons"])
        self.assertIsNone(data["my_application"])


class ApplicationFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.cat = f.category("Camilan")
        self.s = f.store()
        self.m = f.umkm()
        self.p1 = f.product(self.m, self.cat, name="Keripik A")
        self.p2 = f.product(self.m, self.cat, name="Keripik B", base=10000, srp=13000)
        self.o = f.opportunity(self.s, [self.cat], quota=1, commission={"type": "percent", "value": 15})
        self.store_c = f.client_for(self.s.user)
        self.umkm_c = f.client_for(self.m.user)

    def apply(self):
        r = self.umkm_c.post(f"{API}/applications", {"opportunity_id": str(self.o.id), "product_ids": [str(self.p1.id), str(self.p2.id)], "note": "Bisa antar Senin"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_full_lifecycle(self):
        app = self.apply()
        self.assertEqual(app["available_actions"], ["cancel"])
        self.assertEqual(app["counterpart"]["name"], self.s.name)
        self.assertEqual(Notification.objects.filter(user=self.s.user, type="application_submitted").count(), 1)
        self.assertError(self.umkm_c.post(f"{API}/applications", {"opportunity_id": str(self.o.id), "product_ids": [str(self.p1.id)]}, format="json"), 409, "conflict")

        detail = self.store_c.get(f"{API}/applications/{app['id']}").json()
        self.assertEqual(detail["available_actions"], ["review"])
        items = detail["items"]
        # sell price below base price
        r = self.store_c.post(f"{API}/applications/{app['id']}/review", {
            "items": [{"item_id": items[0]["id"], "decision": "approved", "approved_quantity": 24, "sell_price": 100},
                      {"item_id": items[1]["id"], "decision": "rejected", "reject_reason": ""}],
            "delivery_slot": next_slot(self.o),
        }, format="json")
        details = r.json()["error"]["details"]
        self.assertIn("items.0.sell_price", details)
        self.assertIn("items.1.reject_reason", details)
        r = self.store_c.post(f"{API}/applications/{app['id']}/review", {
            "items": [{"item_id": items[0]["id"], "decision": "approved", "approved_quantity": 24, "sell_price": 12500},
                      {"item_id": items[1]["id"], "decision": "rejected", "reject_reason": "Harga tidak sesuai"}],
            "delivery_slot": next_slot(self.o), "stock_mode": "count", "note": "Bawa nota",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["status"], "approved")
        approved = body["items"][0]
        self.assertEqual((approved["commission_per_unit"], approved["umkm_income_per_unit"]), (1875, 10625))
        self.o.refresh_from_db()
        self.assertEqual((self.o.quota_used, self.o.status), (1, "full"))
        self.assertEqual(self.umkm_c.get(f"{API}/applications/{app['id']}").json()["available_actions"], ["ship", "cancel"])

        self.assertError(self.store_c.post(f"{API}/applications/{app['id']}/ship", {}, format="json"), 403, "permission_denied")
        self.assertEqual(self.umkm_c.post(f"{API}/applications/{app['id']}/ship", {"note": "Kurir Joko"}, format="json").json()["status"], "in_delivery")
        r = self.store_c.post(f"{API}/applications/{app['id']}/receive", {"items": [{"item_id": approved["id"], "received_quantity": 30}]}, format="json")
        self.assertIn("items.0.received_quantity", r.json()["error"]["details"])
        r = self.store_c.post(f"{API}/applications/{app['id']}/receive", {"items": [{"item_id": approved["id"], "received_quantity": 20}]}, format="json")
        self.assertEqual(r.json()["status"], "received")
        self.assertEqual(r.json()["items"][0]["stock_quantity"], 20)

        stock = self.store_c.get(f"{API}/stock?as=store").json()
        self.assertEqual(stock["count"], 1)
        row = stock["results"][0]
        url = f"{API}/applications/{app['id']}/items/{row['item_id']}/stock"
        self.assertIn("stock_quantity", self.store_c.patch(url, {"stock_quantity": 21}, format="json").json()["error"]["details"])
        self.assertEqual(self.store_c.patch(url, {"stock_quantity": 0}, format="json").json()["stock_status"], "out_of_stock")
        self.assertEqual(Notification.objects.filter(user=self.m.user, type="stock_out").count(), 1)
        self.assertEqual(self.umkm_c.get(f"{API}/stock?as=umkm&stock_status=out_of_stock").json()["count"], 1)

        self.assertEqual(self.umkm_c.post(f"{API}/applications/{app['id']}/complete", {}, format="json").json()["status"], "completed")
        self.assertError(self.store_c.patch(url, {"stock_quantity": 5}, format="json"), 409, "conflict")
        timeline = self.umkm_c.get(f"{API}/applications/{app['id']}/timeline").json()
        self.assertEqual([e["to_status"] for e in timeline], ["submitted", "approved", "in_delivery", "received", "completed"])

    def test_quota_full_and_cancel_releases(self):
        app = self.apply()
        other = f.umkm()
        p = f.product(other, self.cat)
        from core.services import workflow

        a2 = workflow.create_application(actor=other.user, actor_role="umkm", umkm=other, opportunity=self.o, products=[p])
        items = self.store_c.get(f"{API}/applications/{app['id']}").json()["items"]
        approve = lambda i: {"item_id": i["id"], "decision": "approved", "approved_quantity": 5, "sell_price": 13000}
        self.store_c.post(f"{API}/applications/{app['id']}/review", {"items": [approve(i) for i in items], "delivery_slot": next_slot(self.o)}, format="json")
        items2 = self.store_c.get(f"{API}/applications/{a2.id}").json()["items"]
        r = self.store_c.post(f"{API}/applications/{a2.id}/review", {"items": [approve(i) for i in items2], "delivery_slot": next_slot(self.o)}, format="json")
        self.assertError(r, 409, "conflict")
        r = self.store_c.post(f"{API}/applications/{app['id']}/cancel", {"reason": ""}, format="json")
        self.assertError(r, 400, "validation_error")
        r = self.store_c.post(f"{API}/applications/{app['id']}/cancel", {"reason": "Jadwal tidak cocok"}, format="json")
        self.assertEqual(r.json()["status"], "cancelled")
        self.o.refresh_from_db()
        self.assertEqual((self.o.quota_used, self.o.status), (0, "open"))

    def test_reject_all_and_listing(self):
        app = self.apply()
        items = self.store_c.get(f"{API}/applications/{app['id']}").json()["items"]
        r = self.store_c.post(f"{API}/applications/{app['id']}/review", {
            "items": [{"item_id": i["id"], "decision": "rejected", "reject_reason": "Kemasan belum memenuhi syarat"} for i in items]
        }, format="json")
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(Notification.objects.filter(user=self.m.user, type="application_rejected").count(), 1)
        lst = self.umkm_c.get(f"{API}/applications?as=umkm&status=rejected,completed,cancelled").json()
        self.assertEqual(lst["count"], 1)
        self.assertError(self.umkm_c.get(f"{API}/applications?as=store"), 403, "permission_denied")

    def test_legal_requirement_blocks_application(self):
        o = f.opportunity(self.s, [self.cat], requirements={"requires_halal": True}, title="Butuh halal")
        r = self.umkm_c.post(f"{API}/applications", {"opportunity_id": str(o.id), "product_ids": [str(self.p1.id)]}, format="json")
        self.assertError(r, 400, "validation_error")
        self.assertIn("legal", r.json()["error"]["details"])

    def test_outsider_cannot_see_application(self):
        app = self.apply()
        stranger = f.store()
        self.assertError(f.client_for(stranger.user).get(f"{API}/applications/{app['id']}"), 404, "not_found")


class NotificationTests(Base):
    def test_read_flow_and_email_status(self):
        u = f.user("n@contoh.id")
        from core.services.notify import notify

        n = notify(u, "announcement", "Halo", "Isi")
        self.assertEqual(n.email_status, "sent")
        self.assertEqual(len(mail.outbox), 1)
        notify(u, "announcement", "Dua", "Isi", send_email=False)
        c = f.client_for(u)
        self.assertEqual(c.get(f"{API}/notifications/unread-count").json(), {"count": 2})
        self.assertTrue(c.post(f"{API}/notifications/{n.id}/read").json()["is_read"])
        self.assertEqual(c.get(f"{API}/notifications?unread=true").json()["count"], 1)
        self.assertEqual(c.post(f"{API}/notifications/read-all").json()["updated"], 1)
        self.assertEqual(c.get(f"{API}/me").json()["unread_notifications"], 0)

    def test_failed_email_is_recorded(self):
        u = f.user("n@contoh.id")
        from core.services.notify import notify

        with mock.patch("core.services.notify.send_mail", side_effect=OSError("SMTP down")):
            n = notify(u, "announcement", "Halo", "Isi")
        self.assertEqual((n.email_status, n.email_attempts), ("failed", 1))
        self.assertIn("SMTP down", n.email_error)



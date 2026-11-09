from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AdminAuditLog,
    AdminLevel,
    Application,
    Notification,
    Opportunity,
    StoreProfile,
    UploadedFile,
    User,
)
from core.services import workflow

from . import factories as f

ADMIN = "/api/v1/admin"
REASON = {"admin_reason": "Pemeriksaan rutin pilot."}


def slot(opp):
    d, start, end = workflow.upcoming_slots(opp.receiving_schedule, timezone.localdate() + timedelta(days=1))[0]
    return {"date": d.isoformat(), "start": start, "end": end}


class AdminBase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = f.admin()
        self.c = f.client_for(self.admin)
        self.cat = f.category("Camilan")
        self.s = f.store()
        self.m = f.umkm()
        self.p = f.product(self.m, self.cat)
        self.o = f.opportunity(self.s, [self.cat], quota=2)

    def assertError(self, r, status, code):
        self.assertEqual(r.status_code, status, r.content)
        self.assertEqual(r.json()["error"]["code"], code)

    def apply(self):
        return workflow.create_application(actor=self.m.user, actor_role="umkm", umkm=self.m, opportunity=self.o, products=[self.p])


class AccessTests(AdminBase):
    def test_non_admin_and_staff_limits(self):
        self.assertError(f.client_for(self.s.user).get(f"{ADMIN}/users"), 403, "permission_denied")
        staff = f.admin("staff@test.id", AdminLevel.STAFF)
        sc = f.client_for(staff)
        self.assertEqual(sc.get(f"{ADMIN}/users").status_code, 200)
        r = sc.patch(f"{ADMIN}/users/{self.s.user.id}", {"is_admin": True, "admin_role": "staff"}, format="json")
        self.assertError(r, 403, "superadmin_required")
        r = sc.delete(f"{ADMIN}/opportunities/{self.o.id}?hard=true", REASON, format="json")
        self.assertError(r, 403, "superadmin_required")


class UserAdminTests(AdminBase):
    def test_invite_list_and_duplicate(self):
        r = self.c.post(f"{ADMIN}/users", {"email": "Baru@Contoh.id", "name": "Pengguna Baru", "roles": ["umkm"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "invited")
        self.assertEqual(r.json()["invited_by"]["id"], str(self.admin.id))
        self.assertError(self.c.post(f"{ADMIN}/users", {"email": "baru@contoh.id", "name": "X", "roles": ["umkm"]}, format="json"), 409, "conflict")
        r = self.c.get(f"{ADMIN}/users?status=invited&search=baru")
        self.assertEqual(r.json()["count"], 1)
        r = self.c.get(f"{ADMIN}/users?role=store&ordering=name")
        self.assertTrue(all("store" in u["roles"] for u in r.json()["results"]))
        self.assertTrue(AdminAuditLog.objects.filter(action="invite").exists())

    def test_suspend_activate_delete_restore(self):
        u = self.s.user
        self.assertError(self.c.post(f"{ADMIN}/users/{u.id}/suspend", {"admin_reason": "x"}, format="json"), 400, "validation_error")
        r = self.c.post(f"{ADMIN}/users/{u.id}/suspend", REASON, format="json")
        self.assertEqual(r.json()["status"], "suspended")
        self.assertError(f.client_for(u).get("/api/v1/me"), 403, "account_suspended")
        self.assertTrue(Notification.objects.filter(user=u, type="admin_action").exists())
        self.assertEqual(self.c.post(f"{ADMIN}/users/{u.id}/activate", {}, format="json").json()["status"], "active")
        r = self.c.delete(f"{ADMIN}/users/{u.id}", REASON, format="json")
        self.assertIsNotNone(r.json()["deleted_at"])
        self.assertFalse(StoreProfile.objects.filter(pk=self.s.pk).exists())
        self.assertFalse(Opportunity.objects.filter(pk=self.o.pk).exists())
        self.assertEqual(self.c.get(f"{ADMIN}/users?search={u.email}").json()["count"], 0)
        self.assertEqual(self.c.get(f"{ADMIN}/users?search={u.email}&include_deleted=true").json()["count"], 1)
        self.c.post(f"{ADMIN}/users/{u.id}/restore", REASON, format="json")
        self.assertTrue(Opportunity.objects.filter(pk=self.o.pk).exists())
        self.assertTrue(StoreProfile.objects.filter(pk=self.s.pk).exists())

    def test_hard_delete_rules_and_last_superadmin(self):
        self.apply()
        self.assertError(self.c.delete(f"{ADMIN}/users/{self.m.user.id}?hard=true", REASON, format="json"), 409, "has_active_relations")
        lonely = f.user("sendiri@test.id")
        self.assertEqual(self.c.delete(f"{ADMIN}/users/{lonely.id}?hard=true", REASON, format="json").json(), {"deleted": True, "hard": True})
        self.assertFalse(User.objects.filter(pk=lonely.pk).exists())
        r = self.c.patch(f"{ADMIN}/users/{self.admin.id}", {"admin_role": "staff"}, format="json")
        self.assertError(r, 409, "conflict")

    def test_grant_admin(self):
        r = self.c.patch(f"{ADMIN}/users/{self.m.user.id}", {"is_admin": True, "admin_role": "staff", "admin_reason": "Tim lapangan"}, format="json")
        self.assertEqual((r.json()["is_admin"], r.json()["admin_role"]), (True, "staff"))
        self.assertEqual(AdminAuditLog.objects.filter(action="grant_admin").count(), 1)


class ProfileAdminTests(AdminBase):
    def test_create_store_for_user_and_conflict(self):
        u = f.user("calon@test.id")
        payload = {"user_id": str(u.id), "name": "Toko Baru", "phone": "081200000000", "address": "Jl. Baru 1", "lat": -7.9, "lng": 112.6, "operating_hours": f.HOURS}
        r = self.c.post(f"{ADMIN}/stores", payload, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        u.refresh_from_db()
        self.assertIn("store", u.roles)
        self.assertError(self.c.post(f"{ADMIN}/stores", payload, format="json"), 409, "conflict")

    def test_admin_edit_keeps_verification_and_legal_decision(self):
        r = self.c.patch(f"{ADMIN}/umkms/{self.m.id}", {"nib": "1"}, format="json")  # unknown field is ignored
        self.assertEqual(r.status_code, 200)
        r = self.c.patch(f"{ADMIN}/umkms/{self.m.id}", {"legal": {"nib": "9120300000000", "pirt_number": "2063"}}, format="json")
        v = r.json()["legal_verification"]
        self.assertEqual(v["nib"]["status"], "verified")  # admin edits do not reset verification
        self.assertEqual(v["pirt"]["status"], "pending")
        q = self.c.get(f"{ADMIN}/legal-verifications?status=pending").json()
        self.assertEqual(q["count"], 1)
        self.assertEqual(q["results"][0]["kinds"], ["pirt"])
        r = self.c.post(f"{ADMIN}/umkms/{self.m.id}/legal-verification", {"pirt": {"status": "rejected"}}, format="json")
        self.assertIn("pirt.note", r.json()["error"]["details"])
        r = self.c.post(f"{ADMIN}/umkms/{self.m.id}/legal-verification", {"pirt": {"status": "rejected", "note": "Nomor tidak cocok."}}, format="json")
        self.assertEqual(r.json()["legal_verification"]["pirt"]["status"], "rejected")
        self.assertFalse(r.json()["legal"]["has_pirt"])
        self.assertTrue(Notification.objects.filter(user=self.m.user, type="legal_rejected").exists())
        self.assertError(self.c.post(f"{ADMIN}/umkms/{self.m.id}/legal-verification", {"halal": {"status": "verified"}}, format="json"), 400, "validation_error")


class CatalogAdminTests(AdminBase):
    def test_opportunity_close_reopen_delete(self):
        url = f"{ADMIN}/opportunities/{self.o.id}"
        self.assertEqual(self.c.post(f"{url}/close", REASON, format="json").json()["status"], "closed")
        Opportunity.objects.filter(pk=self.o.pk).update(valid_until=timezone.localdate() - timedelta(days=1))
        self.assertError(self.c.post(f"{url}/reopen", REASON, format="json"), 400, "validation_error")
        new = (timezone.localdate() + timedelta(days=10)).isoformat()
        r = self.c.post(f"{url}/reopen", {**REASON, "valid_until": new}, format="json")
        self.assertEqual((r.json()["status"], r.json()["valid_until"]), ("open", new))
        self.apply()
        self.assertError(self.c.delete(url, REASON, format="json"), 409, "has_active_relations")
        self.assertEqual(self.c.get(f"{ADMIN}/opportunities?status=open&store_id={self.s.id}").json()["count"], 1)

    def test_opportunity_store_locked_after_applications(self):
        self.apply()
        other = f.store(name="Lain")
        r = self.c.patch(f"{ADMIN}/opportunities/{self.o.id}", {"store_id": str(other.id)}, format="json")
        self.assertError(r, 409, "conflict")
        r = self.c.patch(f"{ADMIN}/opportunities/{self.o.id}", {"quota": 5, "admin_reason": "Toko minta tambah"}, format="json")
        self.assertEqual(r.json()["quota"], 5)
        log = AdminAuditLog.objects.filter(action="update", target_type="opportunity").first()
        self.assertEqual(log.changes, [{"field": "quota", "before": "2", "after": "5"}])

    def test_product_admin(self):
        self.assertEqual(self.c.get(f"{ADMIN}/products?umkm_id={self.m.id}").json()["count"], 1)
        r = self.c.patch(f"{ADMIN}/products/{self.p.id}", {"is_active": False}, format="json")
        self.assertFalse(r.json()["is_active"])
        self.c.patch(f"{ADMIN}/products/{self.p.id}", {"is_active": True}, format="json")
        self.apply()
        self.assertError(self.c.patch(f"{ADMIN}/products/{self.p.id}", {"is_active": False}, format="json"), 409, "has_active_relations")
        self.assertError(self.c.delete(f"{ADMIN}/products/{self.p.id}?hard=true", REASON, format="json"), 409, "has_active_relations")
        self.assertEqual(len(self.c.get(f"{ADMIN}/products/{self.p.id}").json()["applications"]), 1)


class ApplicationAdminTests(AdminBase):
    def test_create_on_behalf_requirements_and_skip(self):
        o = f.opportunity(self.s, [self.cat], requirements={"requires_halal": True}, title="Halal")
        body = {"umkm_id": str(self.m.id), "opportunity_id": str(o.id), "product_ids": [str(self.p.id)], **REASON}
        self.assertError(self.c.post(f"{ADMIN}/applications", body, format="json"), 400, "validation_error")
        r = self.c.post(f"{ADMIN}/applications", {**body, "skip_requirement_check": True}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(r.json()["has_override"])
        self.assertTrue(r.json()["admin_can_review"])

    def test_review_force_status_and_stock_correction(self):
        a = self.apply()
        item = a.items.first()
        r = self.c.post(f"{ADMIN}/applications/{a.id}/review", {
            **REASON, "items": [{"item_id": item.id, "decision": "approved", "approved_quantity": 10, "sell_price": 12000}],
            "delivery_slot": slot(self.o),
        }, format="json")
        self.assertEqual(r.json()["status"], "approved", r.content)
        self.assertEqual(r.json()["admin_transitions"], ["submitted", "cancelled"])
        r = self.c.post(f"{ADMIN}/applications/{a.id}/force-status", {**REASON, "to_status": "received"}, format="json")
        self.assertError(r, 409, "conflict")
        self.assertEqual(r.json()["error"]["details"]["allowed"], ["submitted", "cancelled"])
        workflow.ship(a, actor=self.m.user)
        r = self.c.post(f"{ADMIN}/applications/{a.id}/force-status", {
            **REASON, "to_status": "received", "received_items": [{"item_id": item.id, "received_quantity": 8}]
        }, format="json")
        self.assertEqual(r.json()["status"], "received", r.content)
        self.assertEqual(r.json()["items"][0]["stock_quantity"], 8)
        r = self.c.patch(f"{ADMIN}/applications/{a.id}/items/{item.id}/stock", {**REASON, "stock_quantity": 0}, format="json")
        self.assertEqual(r.json()["stock_status"], "out_of_stock")
        self.assertTrue(r.json()["stock_updated_by"]["is_admin"])
        self.assertEqual(self.c.get(f"{ADMIN}/applications?has_override=true").json()["count"], 1)
        self.assertEqual(self.c.get(f"{ADMIN}/stock?stock_status=out_of_stock").json()["summary"]["out_of_stock"], 1)
        r = self.c.post(f"{ADMIN}/applications/{a.id}/force-status", {**REASON, "to_status": "cancelled"}, format="json")
        self.assertEqual(r.json()["status"], "cancelled")
        self.o.refresh_from_db()
        self.assertEqual(self.o.quota_used, 0)

    def test_patch_corrections_and_hard_delete(self):
        a = self.apply()
        workflow.review(a, actor=self.s.user, actor_role="store", delivery_slot=slot(self.o),
                        items=[{"item_id": a.items.first().id, "decision": "approved", "approved_quantity": 5, "sell_price": 12000}])
        item = a.items.first()
        self.assertError(self.c.patch(f"{ADMIN}/applications/{a.id}", {"items": [{"item_id": item.id, "sell_price": 15000}]}, format="json"), 400, "validation_error")
        r = self.c.patch(f"{ADMIN}/applications/{a.id}", {**REASON, "items": [{"item_id": item.id, "sell_price": 15000}], "note": "Catatan baru"}, format="json")
        self.assertEqual(r.json()["items"][0]["commission_per_unit"], 2250)
        self.assertEqual(r.json()["note"], "Catatan baru")
        r = self.c.delete(f"{ADMIN}/applications/{a.id}?hard=true", REASON, format="json")
        self.assertEqual(r.json()["hard"], True)
        self.assertFalse(Application.objects.filter(pk=a.pk).exists())
        self.o.refresh_from_db()
        self.assertEqual(self.o.quota_used, 0)


class SystemAdminTests(AdminBase):
    def test_categories(self):
        r = self.c.post(f"{ADMIN}/categories", {"name": "Kopi & Teh"}, format="json")
        self.assertEqual(r.json()["slug"], "kopi-dan-teh")
        self.assertError(self.c.post(f"{ADMIN}/categories", {"name": "kopi & teh"}, format="json"), 400, "validation_error")
        self.assertError(self.c.delete(f"{ADMIN}/categories/{self.cat.id}"), 409, "has_active_relations")
        self.assertEqual(self.c.delete(f"{ADMIN}/categories/{r.json()['id']}").status_code, 200)
        self.assertFalse(self.c.patch(f"{ADMIN}/categories/{self.cat.id}", {"is_active": False}, format="json").json()["is_active"])
        self.assertEqual(f.client_for(self.m.user).get("/api/v1/categories").json(), [])

    def test_file_remove_detaches(self):
        from django.core.files.base import ContentFile

        file = UploadedFile(owner=self.m.user, purpose="product_photo", original_name="a.png", mime="image/png", size=10)
        file.file.save("a.png", ContentFile(f.png_bytes()), save=True)
        from core.models import ProductPhoto

        ProductPhoto.objects.create(product=self.p, file=file)
        r = self.c.get(f"{ADMIN}/files?purpose=product_photo").json()
        self.assertEqual(r["results"][0]["used_in"][0]["type"], "product")
        r = self.c.post(f"{ADMIN}/files/{file.id}/remove", REASON, format="json")
        self.assertTrue(r.json()["is_removed"])
        self.assertEqual(r.json()["used_in"], [])
        file.file.delete(save=False)

    def test_dashboard_notifications_broadcast_audit_export(self):
        self.apply()
        d = self.c.get(f"{ADMIN}/dashboard").json()
        self.assertEqual(d["applications"]["by_status"]["submitted"], 1)
        self.assertEqual(d["users"]["store"], 1)
        self.assertTrue(d["recent_activity"])

        r = self.c.post(f"{ADMIN}/notifications/broadcast", {"title": "Info", "body": "Pilot diperpanjang", "target": {"roles": ["umkm"]}}, format="json")
        self.assertEqual(r.json(), {"recipients_count": 1})
        self.assertError(self.c.post(f"{ADMIN}/notifications/broadcast", {"title": "x", "body": "y", "target": {}}, format="json"), 400, "validation_error")
        n = Notification.objects.filter(type="announcement").first()
        self.assertError(self.c.post(f"{ADMIN}/notifications/{n.id}/resend-email"), 409, "conflict")
        Notification.objects.filter(pk=n.pk).update(email_status="failed")
        self.assertEqual(self.c.post(f"{ADMIN}/notifications/{n.id}/resend-email").json()["email_status"], "sent")
        self.assertEqual(self.c.get(f"{ADMIN}/notifications?type=announcement").json()["count"], 1)

        r = self.c.get(f"{ADMIN}/exports/applications.csv?status=submitted")
        self.assertEqual(r.status_code, 200)
        text = r.content.decode("utf-8-sig")
        self.assertTrue(text.startswith("id,store_name,umkm_name"))
        self.assertEqual(len(text.strip().splitlines()), 2)
        self.assertError(self.c.get(f"{ADMIN}/exports/nope.csv"), 404, "not_found")

        logs = self.c.get(f"{ADMIN}/audit-logs?action=export").json()
        self.assertEqual(logs["count"], 1)
        self.assertEqual(self.c.get(f"{ADMIN}/audit-logs/{logs['results'][0]['id']}").json()["action"], "export")



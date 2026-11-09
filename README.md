# RitelHub Backend – Django REST API

Backend untuk platform UMKM × toko ritel: aplikasi pengguna (PWA, `docs/frontend-specs.md`) dan panel admin (`docs/frontend-admin-specs.md`). Semua endpoint di kedua spesifikasi sudah diimplementasikan: 38 endpoint pengguna di `/api/v1/` dan 58 endpoint admin di `/api/v1/admin/`.

## 1. Menjalankan secara lokal

Python 3.11 atau lebih baru (diuji dengan 3.14).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # lalu isi SECRET_KEY dan GOOGLE_CLIENT_ID
python manage.py migrate
python manage.py seed_demo    # opsional: data contoh (lihat bagian 6)
python manage.py runserver
```

- API: `http://127.0.0.1:8000/api/v1/`
- Dokumentasi OpenAPI (Swagger): `http://127.0.0.1:8000/api/v1/docs/`
- Django admin (alat developer, bukan panel admin produk): `http://127.0.0.1:8000/django-admin/`
- Tes: `python manage.py test core`

Dengan `DEBUG=False`, server menolak start bila `SECRET_KEY` belum diganti.

## 2. Autentikasi

| Endpoint | Isi |
| --- | --- |
| `POST /auth/google` `{id_token}` | Memverifikasi ID token Google → `{access, refresh, is_new_user, user}`. Akun undangan menjadi aktif saat login pertama. Akun disuspend → 403 `account_suspended`; akun dihapus → 403 |
| `POST /auth/token/refresh` `{refresh}` | Token baru (refresh diputar, yang lama di-blacklist). Akun disuspend/dihapus ditolak |
| `POST /auth/logout` `{refresh}` | Blacklist refresh token → 204 |
| `POST /auth/dev-login` `{email, name?}` | Login tanpa Google untuk mencoba API. Aktif bila `AUTH_DEV_LOGIN=True` dan (`DEBUG=True` atau `AUTH_DEV_LOGIN_ALLOW_PRODUCTION=True`). Siapa pun bisa masuk sebagai akun apa pun, jadi jangan aktifkan di produksi sungguhan |

Kirim `Authorization: Bearer <access>` untuk endpoint lain. Setiap request mengecek ulang status akun, jadi suspend berlaku seketika (sesi refresh juga dicabut).

## 3. Format umum

- Prefix `/api/v1`. Path boleh dengan atau tanpa garis miring akhir.
- ID berupa UUID; uang berupa integer rupiah; tanggal `YYYY-MM-DD`; waktu ISO 8601 dengan zona `Asia/Jakarta`; jam `HH:MM`.
- Daftar berpaginasi: `{count, next, previous, results}`, parameter `page` dan `page_size` (maks 100; default 20, admin 25).
- Semua error memakai satu bentuk:

```json
{"error": {"code": "validation_error", "message": "Periksa kembali isian Anda.", "details": {"items.0.sell_price": "Harga jual minimal Rp9.500."}}}
```

| `code` | HTTP | Kapan |
| --- | --- | --- |
| `validation_error` | 400 | `details` berisi pesan per field (nested memakai titik, mis. `legal.nib`); pesan tanpa field di `non_field_errors` atau `legal` |
| `unauthenticated` | 401 | Token tidak ada/kedaluwarsa |
| `permission_denied` | 403 | Peran/profil tidak sesuai, bukan admin |
| `account_suspended` | 403 | Akun disuspend |
| `superadmin_required` | 403 | Aksi khusus superadmin |
| `not_found` | 404 | Data tidak ada atau bukan milik Anda |
| `conflict` | 409 | Status sudah berubah, kuota penuh, duplikat; `details.allowed` bila ada pilihan yang diizinkan |
| `has_active_relations` | 409 | Masih terkait data aktif; `details.application_ids` berisi ID terkait |
| `rate_limited` | 429 | Terlalu banyak permintaan |

## 4. Endpoint aplikasi pengguna (`/api/v1`)

| Endpoint | Catatan |
| --- | --- |
| `GET /me` | Termasuk `onboarding`, `store_profile`, `umkm_profile`, `is_admin`, `admin_role`, `unread_notifications` |
| `PUT /me/roles` | `{"roles": ["store","umkm"]}`; melepas peran yang masih punya pengajuan aktif → 409 |
| `POST /uploads` | Multipart `file` + `purpose` (`product_photo`, `store_photo`, `umkm_logo`, `legal_document`). JPG/PNG/WEBP (+PDF untuk dokumen), maks 5 MB, tipe dicek dari isi file |
| `GET /categories` | Kategori aktif, urut sesuai admin |
| `GET\|PUT /me/store-profile` | 404 bila belum ada. `operating_hours` 7 hari, minimal 1 hari buka |
| `GET\|PUT /me/umkm-profile` | `legal` berisi nomor + `has_nib/has_pirt/has_halal` + `verification`. Nomor baru/berubah → status `pending` untuk diverifikasi admin |
| `GET\|POST /me/products`, `GET\|PATCH\|DELETE /me/products/{id}` | Filter `search`, `category` (slug), `is_active`. DELETE = arsipkan (409 bila masih di pengajuan aktif); `PATCH {"is_active": true}` mengaktifkan lagi. Isi salah satu `shelf_life_days` atau `expiry_date` |
| `GET\|POST /me/opportunities`, `GET\|PATCH /me/opportunities/{id}`, `POST …/close` | Item menyertakan `application_counts`. Jadwal harus di dalam jam operasional toko; kuota tidak boleh < `quota_used`; peluang `closed` tidak bisa diubah |
| `GET /opportunities` | Feed UMKM: `lat`, `lng` (default lokasi usaha), `radius_km`, `category` (slug, koma), `match` (`high,medium,low`), `sort` (`match`, `distance`, `newest`). Menyertakan `store.distance_km`, `match {level, score, reasons}`, `my_application` |
| `GET /opportunities/{id}` | Ditambah `my_products[]` dengan `eligible` dan `ineligible_reasons`, serta `is_available` |
| `GET\|POST /applications` | `as=store\|umkm` (wajib bila punya dua peran), `status` multi-nilai (`rejected,completed,cancelled`), `opportunity_id`, `ordering` (`-updated_at`, `delivery_slot_start`, …). Item menyertakan `counterpart`, `available_actions`, `delivery_slot {date,start,end,start_at,end_at}` |
| `GET /applications/{id}`, `GET …/timeline` | Hanya untuk UMKM pengaju atau toko pemilik peluang (selain itu 404) |
| `POST …/review` | Toko: `items[] {item_id, decision, approved_quantity, sell_price, reject_reason}`, `delivery_slot` (bila ada yang disetujui), `stock_mode`, `note` |
| `POST …/ship` `{note}`, `…/receive` `{items[] {item_id, received_quantity}, note}`, `…/complete` `{note}`, `…/cancel` `{reason}` | Sesuai `available_actions` |
| `PATCH /applications/{id}/items/{item_id}/stock` | Toko, status `received`: `{stock_quantity}` (mode angka, 0 s.d. jumlah diterima) atau `{stock_status}` (mode ada/habis) |
| `GET /stock` | `as=store\|umkm`, `stock_status`; item habis di urutan atas |
| `GET /notifications?unread=true`, `GET /notifications/unread-count`, `POST /notifications/{id}/read`, `POST /notifications/read-all` | `data.deeplink` menunjuk ke `/kerja-sama/{id}` |

Keempat penyempurnaan opsional di spesifikasi sudah tersedia: `status` multi-nilai, `ordering=delivery_slot_start`, `application_counts` di daftar peluang, dan nomor telepon pihak lawan (`counterpart.phone`).

## 5. Endpoint panel admin (`/api/v1/admin`)

Butuh akun dengan `is_admin` dan `admin_role` (`superadmin` atau `staff`). Semua aksi tulis tercatat di audit log. Aksi yang mewajibkan alasan menerima `admin_reason` (minimal 5 karakter; untuk DELETE boleh di body atau query) dan `notify_user` (default `true`).

| Area | Endpoint |
| --- | --- |
| Pengguna | `GET\|POST /users`, `GET\|PATCH\|DELETE /users/{id}` (`?hard=true` superadmin), `POST /users/{id}/suspend`, `/activate`, `/restore`. Filter `search`, `role` (`store`, `umkm`, `none`), `status`, `is_admin`, `include_deleted`, `ordering` |
| Toko & UMKM | `GET\|POST /stores`, `GET\|PATCH\|DELETE /stores/{id}`, `POST /stores/{id}/restore`; sama untuk `/umkms`; `POST /umkms/{id}/legal-verification`; `GET /legal-verifications?status=pending` |
| Peluang | `GET\|POST /opportunities`, `GET\|PATCH\|DELETE /opportunities/{id}`, `POST …/close`, `…/reopen {valid_until?}`, `…/restore` |
| Produk | `GET\|POST /products`, `GET\|PATCH\|DELETE /products/{id}`, `POST …/restore` |
| Pengajuan | `GET\|POST /applications`, `GET\|PATCH\|DELETE /applications/{id}` (`?hard=true`), `POST …/review`, `POST …/force-status {to_status, received_items?}`, `PATCH …/items/{item_id}/stock`; `GET /stock` |
| Sistem | `GET /dashboard?from=&to=`, `GET\|POST /categories`, `PATCH\|DELETE /categories/{id}`, `GET /files`, `GET /files/{id}`, `POST /files/{id}/remove`, `GET /notifications`, `POST /notifications/{id}/resend-email`, `POST /notifications/broadcast`, `GET /audit-logs`, `GET /audit-logs/{id}`, `GET /exports/{resource}.csv` |

Resource ekspor: `users`, `stores`, `umkms`, `opportunities`, `products`, `applications`, `application-items` (dengan `units_sold`), `stock`. Lebih dari 50.000 baris → 400.

Panel admin di frontend membuka "Admin & akses" (A24) memakai `GET /users?is_admin=true` dan `PATCH /users/{id}` dengan `is_admin`/`admin_role` (khusus superadmin; superadmin terakhir tidak bisa diturunkan).

## 6. Aturan bisnis penting

- **Status kerja sama**: `submitted → approved → in_delivery → received → completed`; `submitted → rejected` (semua produk ditolak); batal dari `submitted` (UMKM) atau `approved` (keduanya). Aksi yang tidak sesuai status → 409.
- **Kuota** menghitung UMKM yang disetujui. Menyetujui memakai satu kuota (penuh → status `full`, menyetujui lagi → 409); membatalkan pengajuan yang sudah disetujui mengembalikannya.
- **Komisi** dibekukan saat pengajuan dibuat; `commission_per_unit` dan `umkm_income_per_unit` dihitung server saat review.
- **Snapshot produk**: item pengajuan menyimpan nama, harga, dan masa simpan saat diajukan, sehingga riwayat tidak berubah bila produk diedit.
- **Slot antar** harus tanggal dalam 14 hari mulai besok dan cocok dengan `receiving_schedule` peluang.
- **Syarat pengajuan**: kategori, masa simpan minimal, harga jual maksimal, dan legalitas wajib dicek; kekurangan legalitas dikembalikan di `details.legal`. Legalitas dihitung ada bila nomor terisi dan tidak ditolak admin.
- **Kecocokan**: skor 0–100 dari kategori (35), jarak (20), legalitas (25), dan produk yang memenuhi syarat (20); ≥ 75 `high`, ≥ 50 `medium`.
- **Hapus** di admin bersifat soft delete (bisa dipulihkan, `include_deleted=true` untuk melihat). Menghapus akun ikut menyembunyikan profil, peluang, dan produknya; memulihkan mengembalikan yang terhapus bersamaan. Hapus permanen hanya untuk superadmin dan hanya bila tidak ada riwayat pengajuan.
- **Notifikasi** selalu dibuat di aplikasi; salinan email dikirim bila `NOTIFICATION_EMAILS=True`, dan statusnya (`sent`/`failed`/`skipped`) bisa dipantau dan dikirim ulang dari admin.

## 7. Data contoh

`python manage.py seed_demo` (atau `--reset` untuk membuat ulang) membuat kategori, superadmin `admin@ritelhub.local` (kata sandi `admin12345` untuk Django admin), staff admin, 4 toko, 5 UMKM, produk berfoto, 7 peluang, dan pengajuan di semua status. Untuk mencoba API tanpa Google, set `DEBUG=True` dan `AUTH_DEV_LOGIN=True`, lalu:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/dev-login -H "Content-Type: application/json" -d '{"email":"agus.sanan@contoh.id"}'
```

Akun contoh: `rina.wulandari@contoh.id` (toko + UMKM), `agus.sanan@contoh.id` (UMKM), `hendra.berkah@contoh.id` (toko), `admin@ritelhub.local` (superadmin), `nadia@ritelhub.local` (staff).

## 8. Struktur kode

| Path | Isi |
| --- | --- |
| `core/models.py` | Semua model (soft delete: `objects` hanya data hidup, `all_objects` termasuk terhapus) |
| `core/services/` | Logika domain: `workflow.py` (status kerja sama, kuota, stok), `matching.py` (jarak, kelayakan, kecocokan), `legal.py`, `notify.py`, `files.py`, `profiles.py`, `audit.py` |
| `core/api/` | View & URL aplikasi pengguna |
| `core/admin_api/` | View & URL panel admin, presenter admin, ekspor CSV |
| `core/presenters.py`, `core/serializers.py` | Bentuk respons dan validasi input |
| `core/exceptions.py` | Format error seragam |
| `core/management/commands/` | `seed_demo`, `purge_removed_files` (hapus isi file yang dihapus admin > 30 hari) |
| `core/tests/` | Tes API pengguna dan admin |

## 9. Sebelum produksi

- PostgreSQL (`DB_ENGINE=postgres`), HTTPS, `DEBUG=False`, `SECRET_KEY` dan domain CORS yang spesifik.
- SMTP untuk email; email saat ini dikirim langsung saat request (cukup untuk pilot). Untuk volume lebih besar, pindahkan ke antrian (Celery/RQ).
- Simpan file di object storage (mis. S3) dan jadwalkan `purge_removed_files` harian.
- Peta lokasi di frontend masih placeholder; backend sudah menerima `lat`/`lng` dan menghitung jarak.
- Refresh token sebaiknya disimpan di cookie HttpOnly bila frontend dan API satu domain.

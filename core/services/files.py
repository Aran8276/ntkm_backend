from django.conf import settings

from ..exceptions import BadRequest
from ..models import FilePurpose, UploadedFile

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
PDF_TYPES = {"application/pdf": "pdf"}

# Magic numbers: never trust the client-declared content type alone.
_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)


def sniff_mime(head: bytes):
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def allowed_types(purpose):
    if purpose == FilePurpose.LEGAL_DOCUMENT:
        return {**IMAGE_TYPES, **PDF_TYPES}
    return IMAGE_TYPES


def validate_upload(uploaded, purpose):
    if purpose not in FilePurpose.values:
        raise BadRequest("Jenis unggahan tidak dikenal.", {"purpose": "Pilih salah satu: " + ", ".join(FilePurpose.values) + "."})
    if uploaded is None:
        raise BadRequest("File wajib diunggah.", {"file": "File wajib diunggah."})
    if uploaded.size > settings.UPLOAD_MAX_BYTES:
        mb = settings.UPLOAD_MAX_BYTES // (1024 * 1024)
        raise BadRequest(f"Ukuran file maksimal {mb} MB.", {"file": f"Ukuran file maksimal {mb} MB."})
    head = uploaded.read(16)
    uploaded.seek(0)
    mime = sniff_mime(head)
    allowed = allowed_types(purpose)
    if mime not in allowed:
        label = "JPG, PNG, WEBP, atau PDF" if purpose == FilePurpose.LEGAL_DOCUMENT else "JPG, PNG, atau WEBP"
        raise BadRequest(f"Format file harus {label}.", {"file": f"Format file harus {label}."})
    return mime


def file_url(f, request=None):
    if f is None:
        return None
    if f.is_removed:
        return None  # clients show a placeholder
    url = f.file.url
    if settings.PUBLIC_BASE_URL:
        return settings.PUBLIC_BASE_URL + url
    return request.build_absolute_uri(url) if request is not None else url


def file_brief(f, request=None):
    if f is None:
        return None
    return {
        "id": str(f.id),
        "url": file_url(f, request),
        "name": f.original_name,
        "mime": f.mime,
        "size": f.size,
        "purpose": f.purpose,
        "is_removed": f.is_removed,
    }


def resolve_files(ids, owner, purpose, field, max_count=None, min_count=0, uploader=None):
    """
    Validate file ids to attach to ``owner``'s record. Files uploaded by an admin
    (``uploader``) on the owner's behalf are transferred to the owner.
    """
    if ids is None:
        ids = []
    if not isinstance(ids, list):
        raise BadRequest("Daftar file tidak valid.", {field: "Harus berupa daftar id file."})
    ids = [str(x) for x in ids]
    if len(ids) != len(set(ids)):
        raise BadRequest("File tidak boleh duplikat.", {field: "File tidak boleh duplikat."})
    if max_count is not None and len(ids) > max_count:
        raise BadRequest(f"Maksimal {max_count} file.", {field: f"Maksimal {max_count} file."})
    if len(ids) < min_count:
        msg = "Tambahkan minimal 1 foto." if min_count == 1 else f"Minimal {min_count} file."
        raise BadRequest(msg, {field: msg})
    try:
        found = {str(f.id): f for f in UploadedFile.objects.filter(id__in=ids)}
    except Exception:  # invalid UUID strings
        raise BadRequest("File tidak ditemukan.", {field: "File tidak ditemukan."})
    allowed_owners = {owner.id} | ({uploader.id} if uploader is not None else set())
    out = []
    for i in ids:
        f = found.get(i)
        if f is None or f.is_removed or f.owner_id not in allowed_owners or f.purpose != purpose:
            raise BadRequest("File tidak ditemukan atau bukan milik akun ini.", {field: "File tidak ditemukan atau bukan milik akun ini."})
        out.append(f)
    for f in out:
        if f.owner_id != owner.id:
            f.owner = owner
            f.save(update_fields=["owner"])
    return out


def resolve_file(file_id, owner, purpose, field, uploader=None):
    if file_id in (None, ""):
        return None
    return resolve_files([file_id], owner, purpose, field, uploader=uploader)[0]

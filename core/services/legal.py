from django.utils import timezone

from ..models import LegalKind, LegalVerification, UmkmProfile, VerificationStatus

KINDS = [k.value for k in LegalKind]
LABEL = {"nib": "NIB", "pirt": "PIRT", "halal": "sertifikat halal"}


def verifications(umkm):
    """kind → LegalVerification (unsaved default when missing)."""
    existing = {v.kind: v for v in umkm.legal_verifications.all()}
    return {k: existing.get(k) or LegalVerification(umkm=umkm, kind=k) for k in KINDS}


def has_legal(umkm):
    """A document counts when the number is filled in and not rejected by an admin."""
    v = verifications(umkm)
    return {k: bool(umkm.legal_number(k)) and v[k].status != VerificationStatus.REJECTED for k in KINDS}


def legal_summary(umkm):
    v = verifications(umkm)
    has = has_legal(umkm)
    return {
        "nib": umkm.nib or None,
        "pirt_number": umkm.pirt_number or None,
        "halal_number": umkm.halal_number or None,
        "has_nib": has["nib"],
        "has_pirt": has["pirt"],
        "has_halal": has["halal"],
        "verification": {k: v[k].status for k in KINDS},
    }


def sync_after_owner_edit(umkm: UmkmProfile, before: dict):
    """
    The UMKM changed its numbers: a new/changed number goes back to "pending"
    for admin review; a cleared number becomes "unverified".
    ``before`` maps kind → previous number.
    """
    now = timezone.now()
    for kind in KINDS:
        old, new = before.get(kind) or "", umkm.legal_number(kind) or ""
        if old == new:
            continue
        v, _ = LegalVerification.objects.get_or_create(umkm=umkm, kind=kind)
        if new:
            v.status = VerificationStatus.PENDING
            v.submitted_at = now
        else:
            v.status = VerificationStatus.UNVERIFIED
            v.submitted_at = None
        v.note = ""
        v.decided_by = None
        v.decided_at = None
        v.save()


def waiting_since(umkm):
    pending = [v.submitted_at for v in umkm.legal_verifications.all() if v.status == VerificationStatus.PENDING and v.submitted_at]
    return min(pending) if pending else None

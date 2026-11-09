from ..models import AdminAuditLog


def client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def stringify(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, dict)):
        import json

        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def diff(before: dict, after: dict):
    """[{field, before, after}] for keys whose value changed."""
    out = []
    for key in after:
        b, a = before.get(key), after.get(key)
        if stringify(b) != stringify(a):
            out.append({"field": key, "before": stringify(b), "after": stringify(a)})
    return out


def audit(request, action, target_type, target_id, target_label, reason="", changes=None):
    admin = request.user
    return AdminAuditLog.objects.create(
        admin=admin,
        admin_name=admin.name or admin.email,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=str(target_label)[:255],
        reason=reason or "",
        changes=changes or [],
        ip_address=client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
    )

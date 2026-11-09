def drop_slash_aliases(endpoints, **kwargs):
    """Every route is also registered with a trailing slash; keep only the canonical one in the docs."""
    return [e for e in endpoints if not e[0].endswith("/")]


try:
    from drf_spectacular.contrib.rest_framework_simplejwt import SimpleJWTScheme

    class PlatformJWTScheme(SimpleJWTScheme):
        """Show the platform JWT auth as a Bearer scheme in the OpenAPI docs."""

        target_class = "core.authentication.PlatformJWTAuthentication"
        name = "jwtAuth"
except ImportError:  # drf-spectacular not installed
    pass

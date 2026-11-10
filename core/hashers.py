"""Password hashing: Argon2id with the RFC 9106 "second recommended" parameters."""
from django.contrib.auth.hashers import Argon2PasswordHasher


class Argon2idPasswordHasher(Argon2PasswordHasher):
    """
    Argon2id (Django's Argon2 hasher always uses the ``id`` variant), tuned to
    RFC 9106 section 4 option 2: t=3 passes, m=64 MiB, p=4 lanes, 128-bit salt,
    256-bit tag. This is also well above the OWASP minimum (m=19 MiB, t=2, p=1).
    Hashes made with other parameters are re-hashed on the next successful login.
    """

    time_cost = 3
    memory_cost = 64 * 1024  # KiB
    parallelism = 4

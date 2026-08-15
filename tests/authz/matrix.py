"""Who may call each route.

One entry per (method, path). The audience set is exhaustive — anyone not named
must be refused. Later plans add, alongside this table, the test that actually
exercises each route as each audience against own/other/no scope; this file is
the single declaration those tests read.
"""

PUBLIC = "public"
STUDENT = "student"
OWNER = "owner"
REFILLER = "refiller"
ADMIN = "admin"
DEVICE = "device"

KNOWN_AUDIENCES = {PUBLIC, STUDENT, OWNER, REFILLER, ADMIN, DEVICE}

MATRIX: dict[tuple[str, str], set[str]] = {
    ("GET", "/health"): {PUBLIC},
    # ── student auth ────────────────────────────────────────────────────────
    # /refresh and /logout are PUBLIC because they authenticate with the
    # refresh cookie rather than a bearer token: reaching them without an
    # access token is the point.
    ("POST", "/v1/app/auth/register"): {PUBLIC},
    ("POST", "/v1/app/auth/login"): {PUBLIC},
    ("POST", "/v1/app/auth/guest"): {PUBLIC},
    ("POST", "/v1/app/auth/google"): {PUBLIC},
    ("POST", "/v1/app/auth/refresh"): {PUBLIC},
    ("POST", "/v1/app/auth/logout"): {PUBLIC},
    ("POST", "/v1/app/auth/verify-email"): {PUBLIC},
    # Forgot/reset are PUBLIC by necessity: someone who cannot sign in is
    # exactly who needs them. The reset token is the credential.
    ("POST", "/v1/app/auth/forgot-password"): {PUBLIC},
    ("POST", "/v1/app/auth/reset-password"): {PUBLIC},
    ("POST", "/v1/app/auth/change-password"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/auth/resend-verification"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/auth/me"): {STUDENT, OWNER, REFILLER, ADMIN},
}

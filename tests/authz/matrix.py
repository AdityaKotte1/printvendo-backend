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
    # Anyone signed in may accept an invitation addressed to them; the token is
    # what authorises it, and the service checks it matches their address.
    ("POST", "/v1/app/staff/accept-invite"): {STUDENT, OWNER, REFILLER, ADMIN},
    # ── owner ───────────────────────────────────────────────────────────────
    # ADMIN appears alongside OWNER throughout because admin is not a separate
    # router here -- it is the same routes with a wider kiosk scope.
    ("GET", "/v1/owner/kiosks"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}"): {OWNER, ADMIN},
    ("POST", "/v1/owner/kiosks/{kiosk_id}/status"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/pricing"): {OWNER, ADMIN},
    ("PUT", "/v1/owner/kiosks/{kiosk_id}/pricing"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/paper"): {OWNER, ADMIN},
    ("PUT", "/v1/owner/kiosks/{kiosk_id}/paper"): {OWNER, ADMIN},
    ("POST", "/v1/owner/kiosks/{kiosk_id}/paper/reset"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/refill-logs"): {OWNER, ADMIN},
    # The machine in the shop. Enrolling one mints a credential, so it is an
    # owner/admin action on a kiosk they already hold -- which is what makes the
    # public /v1/device/register safe.
    ("GET", "/v1/owner/kiosks/{kiosk_id}/device"): {OWNER, ADMIN},
    ("POST", "/v1/owner/kiosks/{kiosk_id}/device/enrol"): {OWNER, ADMIN},
    ("DELETE", "/v1/owner/kiosks/{kiosk_id}/device"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/staff"): {OWNER, ADMIN},
    ("POST", "/v1/owner/kiosks/{kiosk_id}/staff/invite"): {OWNER, ADMIN},
    ("DELETE", "/v1/owner/kiosks/{kiosk_id}/staff/{user_id}"): {OWNER, ADMIN},
    # ── refiller ────────────────────────────────────────────────────────────
    # Paper only. No pricing, no earnings, no student identity.
    ("GET", "/v1/refiller/kiosks"): {REFILLER, ADMIN},
    ("GET", "/v1/refiller/kiosks/{kiosk_id}"): {REFILLER, ADMIN},
    ("GET", "/v1/refiller/kiosks/{kiosk_id}/paper"): {REFILLER, ADMIN},
    ("PUT", "/v1/refiller/kiosks/{kiosk_id}/paper"): {REFILLER, ADMIN},
    ("POST", "/v1/refiller/kiosks/{kiosk_id}/paper/reset"): {REFILLER, ADMIN},
    ("POST", "/v1/refiller/kiosks/{kiosk_id}/paper/out-of-paper"): {REFILLER, ADMIN},
    ("GET", "/v1/refiller/kiosks/{kiosk_id}/refill-logs"): {REFILLER, ADMIN},
    # ── device ──────────────────────────────────────────────────────────────
    # /register is PUBLIC by necessity: a machine being installed has no token
    # yet. The enrolment code it must present is single-use, short-lived, and
    # was issued for one kiosk by somebody who already had access to it.
    ("POST", "/v1/device/register"): {PUBLIC},
    # Everything else authenticates with X-Device-Token, and that token *is* the
    # kiosk -- no device route takes a kiosk id, so there is nothing to confuse.
    ("POST", "/v1/device/heartbeat"): {DEVICE},
    ("POST", "/v1/device/tasks/next"): {DEVICE},
    ("GET", "/v1/device/tasks/{task_id}/file"): {DEVICE},
    ("POST", "/v1/device/tasks/{task_id}/status"): {DEVICE},
}

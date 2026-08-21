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
    # ── student documents ───────────────────────────────────────────────────
    # A guest counts as a student: printing without signing up is a carried
    # feature, and a guest account holds the same STUDENT role.
    ("POST", "/v1/app/documents"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/documents/photo-layout"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/documents"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("DELETE", "/v1/app/documents/{document_id}"): {STUDENT, OWNER, REFILLER, ADMIN},
    # ── student kiosks, orders and wallet ───────────────────────────────────
    # Browsing shops is not a scope question. `kiosk_scope` answers "which
    # kiosks may this person manage", and a student manages none -- so these
    # routes deliberately do not use it, and every signed-in audience sees the
    # same list of shops that can currently print.
    ("GET", "/v1/app/kiosks"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/kiosks/{kiosk_id}"): {STUDENT, OWNER, REFILLER, ADMIN},
    # An order belongs to the person who placed it. Somebody else's is 404, not
    # 403 -- including for an admin, who has no business reading a student's
    # order through the student surface.
    ("POST", "/v1/app/orders"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/orders"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/orders/{order_id}"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/orders/{order_id}/pay/wallet"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/orders/{order_id}/checkout"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/orders/{order_id}/verify"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/wallet"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/wallet/statement"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("POST", "/v1/app/wallet/topup"): {STUDENT, OWNER, REFILLER, ADMIN},
    # ── webhooks ────────────────────────────────────────────────────────────
    # PUBLIC because Razorpay holds no token of ours. The signature *is* the
    # authentication, and it is checked against the raw body before anything is
    # parsed. The owner form additionally refuses events about a payment a
    # different account collected -- an owner does hold a secret that verifies
    # here, so that second check is what stops one settling a competitor's
    # takings.
    ("POST", "/v1/webhooks/razorpay"): {PUBLIC},
    ("POST", "/v1/webhooks/razorpay/{owner_id}"): {PUBLIC},
    # ── owner earnings and orders ───────────────────────────────────────────
    # ADMIN alongside OWNER, as everywhere in this router: admin is not a second
    # surface, it is the same routes with a wider kiosk scope.
    ("GET", "/v1/owner/earnings"): {OWNER, ADMIN},
    ("GET", "/v1/owner/earnings/by-kiosk"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/orders"): {OWNER, ADMIN},
    # ── owner payment configuration ─────────────────────────────────────────
    # OWNER only, and not ADMIN: these act on *the caller's own* Razorpay
    # account, resolved from their token. An admin reaching them would be
    # configuring their own payment keys, which is not a thing an admin does --
    # reviewing somebody else's change request is an admin action and belongs
    # to the admin surface, where the owner is named explicitly.
    ("GET", "/v1/owner/payment-config"): {OWNER},
    ("PUT", "/v1/owner/payment-config/keys"): {OWNER},
    ("PUT", "/v1/owner/payment-config/webhook-secret"): {OWNER},
    ("GET", "/v1/owner/payment-config/webhook-endpoint"): {OWNER},
    ("POST", "/v1/owner/payment-config/change-request"): {OWNER},
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

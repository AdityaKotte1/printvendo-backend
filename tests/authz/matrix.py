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

# A WebSocket route has no HTTP method. It still authenticates and still serves
# data, so it is declared like everything else, under a pseudo-method.
WEBSOCKET = "WS"

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
    # Saving a shop is a note about the person doing it, so every signed-in
    # audience may, and each sees only their own. It goes through the same
    # visibility rule as looking, so a star cannot be used to discover whether
    # a shop exists.
    ("PUT", "/v1/app/kiosks/{kiosk_id}/favourite"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("DELETE", "/v1/app/kiosks/{kiosk_id}/favourite"): {STUDENT, OWNER, REFILLER, ADMIN},
    # An order belongs to the person who placed it. Somebody else's is 404, not
    # 403 -- including for an admin, who has no business reading a student's
    # order through the student surface.
    ("POST", "/v1/app/orders"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/orders"): {STUDENT, OWNER, REFILLER, ADMIN},
    ("GET", "/v1/app/orders/{order_id}"): {STUDENT, OWNER, REFILLER, ADMIN},
    # A receipt for your own order. Scoped by `_order_or_404`, so somebody
    # else's is a 404 -- including for an admin, who has no business reading a
    # student's receipt through the student surface.
    ("GET", "/v1/app/orders/{order_id}/invoice"): {STUDENT, OWNER, REFILLER, ADMIN},
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
    ("GET", "/v1/owner/earnings/daily"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/orders"): {OWNER, ADMIN},
    # The same list as a file, for accounts. Same scope, same audience, and
    # the same absence of any student: a CSV is built by hand rather than from
    # a response type, so what the type guarantees above, a test guarantees
    # there.
    ("GET", "/v1/owner/kiosks/{kiosk_id}/orders/export"): {OWNER, ADMIN},
    # Giving back money your own account collected. **OWNER alone** -- the one
    # route in this router where admin is not alongside, because this one is
    # not about kiosk scope. A shop refunds its own takings out of its own
    # Razorpay; an admin has collected nothing, so the rule could only ever
    # refuse them, and platform money goes back through
    # `/v1/admin/orders/{order_id}/refund` instead. A student must never reach
    # it either: refunding the order you placed is not a thing the payer may do.
    ("POST", "/v1/owner/kiosks/{kiosk_id}/orders/{order_id}/refund"): {OWNER},
    # ── owner payment configuration ─────────────────────────────────────────
    # OWNER only, and not ADMIN: these act on *the caller's own* Razorpay
    # account, resolved from their token. An admin reaching them would be
    # configuring their own payment keys, which is not a thing an admin does --
    # reviewing somebody else's change request is an admin action and belongs
    # to the admin surface, where the owner is named explicitly.
    # An owner's own subscription, and buying one. Admin is included because
    # admin is a wider scope rather than a separate router -- but note an admin
    # buying here buys it for *themselves*, not on somebody's behalf; granting
    # is what the admin surface is for.
    ("GET", "/v1/owner/billing"): {OWNER, ADMIN},
    ("GET", "/v1/owner/billing/quote"): {OWNER, ADMIN},
    ("POST", "/v1/owner/billing/subscription"): {OWNER, ADMIN},
    # The browser coming back from Razorpay. Same audience as buying, and
    # scoped to the caller's own subscription -- the signature is what
    # authorises the capture, and it is checked against the platform key.
    ("POST", "/v1/owner/billing/subscription/{subscription_id}/verify"): {OWNER, ADMIN},
    # The invoice for what you paid, as bytes from an authenticated route
    # rather than a URL -- the same rule as the student receipt and the
    # account-ownership proof. Scoped to the caller's own subscription, because
    # it carries a name, an address and what somebody pays for their software.
    ("GET", "/v1/owner/billing/subscription/{subscription_id}/invoice"): {OWNER, ADMIN},
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
    # Restarting the machine in a shop. OWNER and ADMIN, through one route:
    # the old backend had this twice -- once for an owner and a second copy in
    # `pi.py` for an admin -- and they drifted. Admin is a wider scope here, as
    # it is everywhere else in this router.
    ("POST", "/v1/owner/kiosks/{kiosk_id}/device/commands"): {OWNER, ADMIN},
    ("GET", "/v1/owner/kiosks/{kiosk_id}/device/commands"): {OWNER, ADMIN},
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
    # ── admin: reviewing where an owner's money goes ───────────────────────
    # ADMIN alone, and this is the one place in the API where that exclusivity
    # is the control itself. The set-once rule on payment keys is only worth
    # something if the person approving a change is not the person asking for
    # it -- an owner who could reach these routes would approve their own
    # request, and an account takeover would walk straight through it to
    # redirecting every rupee that owner's kiosks collect.
    ("GET", "/v1/admin/payment-config/change-requests"): {ADMIN},
    # The proof of account ownership, as bytes. Authenticated for the same
    # reason it is not a static URL: it is evidence about somebody's bank
    # account, and the old dashboard served these from a path anyone could
    # guess, behind an onerror handler that hid its own failure.
    ("GET", "/v1/admin/payment-config/change-requests/{request_id}/proof"): {ADMIN},
    ("POST", "/v1/admin/payment-config/change-requests/{request_id}/review"): {ADMIN},
    # The audit trail spans the whole estate: every shop's prices, staff
    # changes and payment configuration. There is no scoped version of it, so
    # there is no audience below ADMIN that could safely hold a narrower one.
    ("GET", "/v1/admin/alerts"): {ADMIN},
    ("POST", "/v1/admin/alerts/{alert_id}/resolve"): {ADMIN},
    ("GET", "/v1/admin/audit"): {ADMIN},
    # Platform takings across the whole estate. An owner reading this would see
    # every other shop's turnover rolled up, which is somebody else's
    # commercial information.
    ("GET", "/v1/admin/revenue"): {ADMIN},
    # A kiosk's life. Creating one decides what it is, and therefore whose
    # Razorpay collects at it; changing its type decides that again. An owner
    # who could reach either would point a platform kiosk's takings at
    # themselves. The stage route is the whole ladder, where the owner surface
    # has only LIVE and MAINTENANCE -- wider scope, not a bypass: the same
    # `move_to` refuses the same transitions for both.
    ("POST", "/v1/admin/kiosks"): {ADMIN},
    # The same ladder in one request. Admin only, like every other kiosk
    # creation: it decides which shop a machine belongs to and whose account
    # collects at it.
    ("POST", "/v1/admin/kiosks/provision"): {ADMIN},
    # Giving money back. Admin only: an owner refunding their own takings is a
    # separate surface and a separate decision, and a student must never be
    # able to refund the order they placed.
    ("POST", "/v1/admin/orders/{order_id}/refund"): {ADMIN},
    # One order, whole -- who paid, how the money moved, and what has already
    # been given back. **ADMIN alone, and here the audience is the control
    # rather than the scope.** The owner surface is built to be incapable of
    # carrying student identity, so an owner reading this at a shop they hold
    # would get exactly what `OwnerOrderResponse` exists to withhold.
    ("GET", "/v1/admin/orders/{order_id}"): {ADMIN},
    ("GET", "/v1/admin/kiosks/{kiosk_id}"): {ADMIN},
    ("POST", "/v1/admin/kiosks/{kiosk_id}/stage"): {ADMIN},
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/type"): {ADMIN},
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/wallet"): {ADMIN},
    # Where a shop claims to be decides which students walk to it, and a kiosk
    # placed on top of a busier one is a claim about somebody else's trade.
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/location"): {ADMIN},
    ("POST", "/v1/admin/kiosks/{kiosk_id}/owner"): {ADMIN},
    # Plans and one owner's terms. A trial is not a courtesy: a subscription
    # inside one is in force, which is half of what the payment gate requires
    # before a SOLD kiosk collects into its owner's account. An owner able to
    # grant themselves one could turn their own takings on.
    ("GET", "/v1/admin/plans"): {ADMIN},
    ("POST", "/v1/admin/plans"): {ADMIN},
    ("PATCH", "/v1/admin/plans/{plan_id}"): {ADMIN},
    ("PUT", "/v1/admin/plans/{plan_id}/discounts"): {ADMIN},
    ("GET", "/v1/admin/owners/{owner_id}/billing"): {ADMIN},
    ("GET", "/v1/admin/owners/{owner_id}/billing/quote"): {ADMIN},
    ("POST", "/v1/admin/owners/{owner_id}/billing/trial"): {ADMIN},
    ("DELETE", "/v1/admin/owners/{owner_id}/billing/trial"): {ADMIN},
    ("PUT", "/v1/admin/owners/{owner_id}/billing/price"): {ADMIN},
    ("PUT", "/v1/admin/owners/{owner_id}/billing/discounts"): {ADMIN},
    (
        "DELETE",
        "/v1/admin/owners/{owner_id}/billing/discounts/{duration_months}",
    ): {ADMIN},
    # Accounts and roles. An owner who could grant roles could make themselves
    # an admin; one who could search accounts would hold a list of every
    # student on the platform. The search takes an exact address and there is
    # no route that lists everybody, so even an admin cannot walk it.
    ("GET", "/v1/admin/accounts"): {ADMIN},
    ("GET", "/v1/admin/accounts/{account_id}"): {ADMIN},
    ("POST", "/v1/admin/accounts/{account_id}/wallet/credit"): {ADMIN},
    ("PUT", "/v1/admin/accounts/{account_id}/roles/{role}"): {ADMIN},
    ("DELETE", "/v1/admin/accounts/{account_id}/roles/{role}"): {ADMIN},
    ("POST", "/v1/admin/accounts/{account_id}/deactivate"): {ADMIN},
    ("POST", "/v1/admin/accounts/{account_id}/activate"): {ADMIN},
    # ── device ──────────────────────────────────────────────────────────────
    # /register is PUBLIC by necessity: a machine being installed has no token
    # yet. The enrolment code it must present is single-use, short-lived, and
    # was issued for one kiosk by somebody who already had access to it.
    ("POST", "/v1/device/register"): {PUBLIC},
    # Everything else authenticates with X-Device-Token, and that token *is* the
    # kiosk -- no device route takes a kiosk id, so there is nothing to confuse.
    ("POST", "/v1/device/heartbeat"): {DEVICE},
    ("POST", "/v1/device/tasks/next"): {DEVICE},
    # What an operator has asked this machine to do, and how it went. Claimed
    # over HTTP like a print task, for the same reason: the socket carries a
    # wake and never work.
    ("POST", "/v1/device/commands/next"): {DEVICE},
    ("POST", "/v1/device/commands/{command_id}/result"): {DEVICE},
    # "I cannot get this out of the printer", which closes the shop. DEVICE
    # only: it is a claim about one kiosk, and the token is which kiosk.
    ("POST", "/v1/device/printer-health"): {DEVICE},
    ("GET", "/v1/device/tasks/{task_id}/file"): {DEVICE},
    ("POST", "/v1/device/tasks/{task_id}/status"): {DEVICE},
    # The socket. Declared like any other route, under a pseudo-method, because
    # it authenticates and serves data and is therefore exactly as much an
    # authorisation decision. It is checked *before* the handshake is accepted:
    # a socket accepted first and checked afterwards is one an unauthenticated
    # client can hold open, by the thousand, for nothing.
    (WEBSOCKET, "/v1/device/ws"): {DEVICE},
}

"""Whether each mutating route leaves a trail, and why.

The backend being replaced had a working audit helper with the right
transactional behaviour, and called it from 15 of its 94 mutating routes. Nobody
decided that; it is simply what "remember to call the audit helper" converges
on. The gap was never in the helper.

So the rule here is not "audit everything" -- that produces a table full of
`document.upload` nobody will ever read, which is its own kind of useless. The
rule is that **every mutating route must appear below**, marked either AUDITED
or EXEMPT with a reason. Adding a route and not deciding fails the build.

An EXEMPT entry is a claim someone made on purpose and can be argued with in
review. A missing entry is an oversight nobody sees. That difference is the
whole mechanism.
"""

AUDITED = "audited"
EXEMPT = "exempt"

# Reasons a route may legitimately leave no trail. Named rather than free text,
# so the same argument cannot be made two slightly different ways and so the
# set of acceptable excuses is itself reviewable.
SELF_SERVICE = "the actor is acting only on their own data"
UNAUTHENTICATED = "no actor to record; the credential is the request itself"
RECORDED_ELSEWHERE = "the change is already a durable record of itself"
DEVICE_TELEMETRY = "high-frequency machine reporting; an audit row per beat is noise"

AUDIT_MATRIX: dict[tuple[str, str], tuple[str, str]] = {
    # ── auth ────────────────────────────────────────────────────────────────
    # A person managing their own account. The interesting security events --
    # a password reset, a session revoked as suspected theft -- are recorded by
    # identity in its own tables, which outlive any audit retention.
    ("POST", "/v1/app/auth/register"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/login"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/guest"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/google"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/refresh"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/auth/logout"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/auth/verify-email"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/forgot-password"): (EXEMPT, UNAUTHENTICATED),
    ("POST", "/v1/app/auth/reset-password"): (EXEMPT, UNAUTHENTICATED),
    ("POST", "/v1/app/auth/change-password"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/auth/resend-verification"): (EXEMPT, SELF_SERVICE),
    # Accepting an invitation changes who can reach a kiosk, and the person
    # doing it is not the person who owns that kiosk. That is exactly the kind
    # of access change an owner should be able to see afterwards.
    ("POST", "/v1/app/staff/accept-invite"): (AUDITED, ""),
    # ── student's own things ────────────────────────────────────────────────
    # Buying a subscription is recorded by the subscription row and the payment
    # beside it, both of which outlive any audit retention.
    ("POST", "/v1/admin/kiosks/provision"): (AUDITED, ""),
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/location"): (AUDITED, ""),
    # Money going back is exactly what somebody has to answer for later.
    ("POST", "/v1/admin/orders/{order_id}/refund"): (AUDITED, ""),
    ("POST", "/v1/owner/billing/subscription"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/owner/billing/subscription/{subscription_id}/verify"): (
        EXEMPT,
        RECORDED_ELSEWHERE,
    ),
    ("PUT", "/v1/app/kiosks/{kiosk_id}/favourite"): (EXEMPT, SELF_SERVICE),
    ("DELETE", "/v1/app/kiosks/{kiosk_id}/favourite"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/documents"): (EXEMPT, SELF_SERVICE),
    ("POST", "/v1/app/documents/photo-layout"): (EXEMPT, SELF_SERVICE),
    ("DELETE", "/v1/app/documents/{document_id}"): (EXEMPT, SELF_SERVICE),
    # An order *is* the record of itself, with a frozen quote, a state machine
    # and a payment row. An audit entry beside it would be a second, weaker
    # copy of the same facts, and the two would eventually disagree.
    ("POST", "/v1/app/orders"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/orders/{order_id}/pay/wallet"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/orders/{order_id}/checkout"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/orders/{order_id}/verify"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/app/wallet/topup"): (EXEMPT, RECORDED_ELSEWHERE),
    # ── owner: where their money goes ───────────────────────────────────────
    # Changing the account that collects every student payment at every one of
    # an owner's kiosks is the single most consequential thing a non-admin can
    # do here, and the one an account takeover would go straight for.
    ("PUT", "/v1/owner/payment-config/keys"): (AUDITED, ""),
    ("PUT", "/v1/owner/payment-config/webhook-secret"): (AUDITED, ""),
    ("POST", "/v1/owner/payment-config/change-request"): (AUDITED, ""),
    # ── owner: other people's money and other people's access ───────────────
    # Money going back is exactly what somebody has to answer for later, and an
    # owner refunding their own shop's takings is the case where that matters
    # most -- there is no settlement run in which it would otherwise surface.
    ("POST", "/v1/owner/kiosks/{kiosk_id}/orders/{order_id}/refund"): (AUDITED, ""),
    ("POST", "/v1/owner/kiosks/{kiosk_id}/status"): (AUDITED, ""),
    ("PUT", "/v1/owner/kiosks/{kiosk_id}/pricing"): (AUDITED, ""),
    ("PUT", "/v1/owner/kiosks/{kiosk_id}/paper"): (AUDITED, ""),
    ("POST", "/v1/owner/kiosks/{kiosk_id}/paper/reset"): (AUDITED, ""),
    # ── refiller ────────────────────────────────────────────────────────────
    # A refiller is staff acting on somebody else's kiosk. "Who reset the tray
    # to 500 when there were clearly 200 sheets in it" is the question this
    # exists to answer.
    ("PUT", "/v1/refiller/kiosks/{kiosk_id}/paper"): (AUDITED, ""),
    ("POST", "/v1/refiller/kiosks/{kiosk_id}/paper/reset"): (AUDITED, ""),
    ("POST", "/v1/refiller/kiosks/{kiosk_id}/paper/out-of-paper"): (AUDITED, ""),
    # Inviting somebody into a kiosk, and removing them, are access changes to
    # a business the actor may not own. Enrolling or removing a device mints or
    # destroys a credential that can pull print jobs.
    ("POST", "/v1/owner/kiosks/{kiosk_id}/staff/invite"): (AUDITED, ""),
    ("DELETE", "/v1/owner/kiosks/{kiosk_id}/staff/{user_id}"): (AUDITED, ""),
    ("POST", "/v1/owner/kiosks/{kiosk_id}/device/enrol"): (AUDITED, ""),
    # Restarting somebody's shop machine mid-trade is an action on their
    # estate, and "the printer came back on its own" and "an admin restarted
    # it" are different facts about the same afternoon.
    ("POST", "/v1/owner/kiosks/{kiosk_id}/device/commands"): (AUDITED, ""),
    ("DELETE", "/v1/owner/kiosks/{kiosk_id}/device"): (AUDITED, ""),
    # ── admin ─────────────────────────────────────────────────────────────────────
    # Deciding that an owner may repoint their takings at a different bank
    # account. Recorded against the owner rather than the request, so the whole
    # story -- asked, decided, keys changed -- is one query on one entity id.
    ("POST", "/v1/admin/payment-config/change-requests/{request_id}/review"): (
        AUDITED,
        "",
    ),
    # The alert row records who resolved it and when. An audit entry beside it
    # would be a second, weaker copy of one fact, and the two would eventually
    # disagree -- which is the shape of most of the legacy audit.
    ("POST", "/v1/admin/alerts/{alert_id}/resolve"): (EXEMPT, RECORDED_ELSEWHERE),
    # "Who put this shop live, and when" is the first question asked when money
    # turns up in the wrong account. The kiosk row holds the current stage and
    # type; only the trail holds who moved them and what they were before.
    ("POST", "/v1/admin/kiosks"): (AUDITED, ""),
    ("POST", "/v1/admin/kiosks/{kiosk_id}/stage"): (AUDITED, ""),
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/type"): (AUDITED, ""),
    ("PUT", "/v1/admin/kiosks/{kiosk_id}/wallet"): (AUDITED, ""),
    ("POST", "/v1/admin/kiosks/{kiosk_id}/owner"): (AUDITED, ""),
    # Commercial terms. A rate nobody can explain in a year's time is a rate
    # somebody will argue about, and the row holds only the current figure --
    # who granted it, when, and what it replaced live here or nowhere.
    ("POST", "/v1/admin/plans"): (AUDITED, ""),
    ("PATCH", "/v1/admin/plans/{plan_id}"): (AUDITED, ""),
    ("PUT", "/v1/admin/plans/{plan_id}/discounts"): (AUDITED, ""),
    ("POST", "/v1/admin/owners/{owner_id}/billing/trial"): (AUDITED, ""),
    ("DELETE", "/v1/admin/owners/{owner_id}/billing/trial"): (AUDITED, ""),
    ("PUT", "/v1/admin/owners/{owner_id}/billing/price"): (AUDITED, ""),
    ("PUT", "/v1/admin/owners/{owner_id}/billing/discounts"): (AUDITED, ""),
    (
        "DELETE",
        "/v1/admin/owners/{owner_id}/billing/discounts/{duration_months}",
    ): (AUDITED, ""),
    # Who made this person an admin, and who switched that account off, are the
    # first questions asked after an account does something it should not have
    # been able to. Neither the user row nor the role row records who wrote it.
    ("POST", "/v1/admin/accounts/{account_id}/wallet/credit"): (AUDITED, ""),
    ("PUT", "/v1/admin/accounts/{account_id}/roles/{role}"): (AUDITED, ""),
    ("DELETE", "/v1/admin/accounts/{account_id}/roles/{role}"): (AUDITED, ""),
    ("POST", "/v1/admin/accounts/{account_id}/deactivate"): (AUDITED, ""),
    ("POST", "/v1/admin/accounts/{account_id}/activate"): (AUDITED, ""),
    # ── device ──────────────────────────────────────────────────────────────
    # Registration exchanges an enrolment code for a lasting token. The owner
    # side of that (`/device/enrol`) is audited above, where there is an actor;
    # here the machine is acting on a code, and the KioskDevice row is itself
    # the durable record of what was minted and when.
    ("POST", "/v1/device/register"): (EXEMPT, RECORDED_ELSEWHERE),
    ("POST", "/v1/device/heartbeat"): (EXEMPT, DEVICE_TELEMETRY),
    ("POST", "/v1/device/tasks/next"): (EXEMPT, DEVICE_TELEMETRY),
    # The machine taking an instruction and saying how it went. The decision
    # that matters -- somebody asking for the restart -- is audited on the
    # owner side, where there is an actor to record; this end has only a
    # device, and a row per poll would be the noise this exemption names.
    ("POST", "/v1/device/commands/next"): (EXEMPT, DEVICE_TELEMETRY),
    ("POST", "/v1/device/commands/{command_id}/result"): (EXEMPT, DEVICE_TELEMETRY),
    # A stuck printer closing a shop is loud where it needs to be: it raises an
    # admin alert, which stands itself down when printing works again. An audit
    # row per report would be a row a minute from a jammed machine.
    ("POST", "/v1/device/printer-health"): (EXEMPT, DEVICE_TELEMETRY),
    ("POST", "/v1/device/tasks/{task_id}/status"): (EXEMPT, DEVICE_TELEMETRY),
    # ── webhooks ────────────────────────────────────────────────────────────
    # Razorpay is not an actor with an account. What arrived is recorded as a
    # Payment or a Refund row, which is the durable record; an audit entry would
    # duplicate it without adding an actor.
    ("POST", "/v1/webhooks/razorpay"): (EXEMPT, UNAUTHENTICATED),
    ("POST", "/v1/webhooks/razorpay/{owner_id}"): (EXEMPT, UNAUTHENTICATED),
}

# Admin redesign — owner app + platform console

**Date:** 2026-08-10
**Status:** approved, not yet built
**Scope:** `cloud-backend` (additive), new `printvendo-owner`, new `printvendo-admin`, one fix in `printvendo-web`

## Problem

Kiosk owners and the platform owner share a single dashboard. Admin pages are
hidden inside the same navigation with `style="display:none"` and revealed by a
JavaScript check on `is_admin`. Twelve near-duplicate HTML pages, no component
layer, and one owner's information spread across six of them: subscription on
one page, KYC on another, Razorpay config request on a third, settlements on a
fourth, their kiosks on a fifth.

Two people doing two entirely different jobs through one shell. That is the
"scattered" complaint, and no amount of restyling fixes it.

## The business, as it actually works

This was established during brainstorming and is the foundation for everything
below. It was not previously written down anywhere.

| Kiosk type | Who owns the machine | Where print money goes | What the platform earns |
|---|---|---|---|
| **PLATFORM** | Platform | Platform Razorpay | The print revenue |
| **SOLD** | Shop bought the hardware | **Owner's own Razorpay** | Subscription only |
| **SAAS** | Shop's own PC + printer, running the Windows agent | **Owner's own Razorpay** | Subscription only |

`payments.py:559` (`get_razorpay_client_for_printer`) already routes to the
owner's keys when `KioskPaymentConfig.is_configured` is true, and falls back to
platform keys otherwise. So:

- **Every SOLD and SAAS owner sets their own Razorpay keys. That is a
  requirement of going live, not an option.** Their print money then goes
  straight from the student to them and never passes through Printvendo at all.
- **There are therefore no settlements**, and none are needed: an owner's
  takings are never in the platform's hands to begin with. This is not the
  platform keeping anyone's money — it is the platform never touching it. The
  earlier model, which collected on a keyless owner's behalf and settled up
  later, is gone; it was the main source of the old system's confusion.
- It follows that an owned kiosk **must not take student money before its keys
  exist** — that is the one situation where an owner's earnings would land in
  the platform account, which is exactly what this model avoids.
  `onboarding_stage` therefore gates `LIVE` behind `KEYS`. The gate protects the
  owner; it is not a penalty.
- The existing `Settlement` model and `/owner/settlements/*` endpoints are
  **retired**. They survive only until the old dashboard is switched off, and
  neither new app may call them.
- Subscription terms are negotiated per deal: price, discount, and a number of
  free months (typically bundled with a hardware sale).

## Decisions

Twenty-one, from the brainstorming session.

1. **Two separate apps** — owner PWA and admin console. The owner app is
   phone-first but **fully usable on desktop**: the same screens reflow to a
   two-column layout with a sidebar above 1024px. Owners do paperwork at a
   counter PC, not only on a phone.
2. Owner home leads with **money**; health below
3. **Own Razorpay keys are the goal state**; platform-collected is temporary
4. **Database-backed plans** with per-deal negotiated price and free-until
5. Explicit **kiosk type** (PLATFORM / SOLD / SAAS)
6. Console home is a **unified work queue**
7. **Tracked onboarding pipeline** with named stages
8. Owner alerts by **email only** (no push, no WhatsApp)
9. Owners see **filename, never student identity**
10. **Whoever received the money issues the refund**
11. Owner navigation: Home · Kiosks · Jobs · Account
12. Console is **entity-first** — one page holds everything about an owner
13. Same ink system, **quieter** application in the console
14. Two Next.js apps sharing a copied design system
15. Backend and apps are **one project**; migrations are additive
16. **Wallet works only at platform kiosks**, hidden silently elsewhere
17. Subscription lapse → grace → **delist quietly** from the student app
18. Owners set their own prices **within platform limits**
19. **Audit log** on money- and access-affecting actions
20. Owners **invite and assign their own refillers**
21. Build order: backend → owner app → console

## Bugs found during exploration

These exist in production today and are folded into this work.

### B1 — Wallet money leak (critical)

`/wallet/hold` and `/wallet/hold/multi` never check whose Razorpay keys the
target kiosk uses. A student can spend wallet balance at a SOLD or SAAS kiosk:
the top-up money is already in the **platform's** account, the shop prints the
job, and the platform keeps money the owner earned with no way to return it —
there are no settlements. The owner works for free.

Reachable from both the old and the new student app.

### B2 — Student PII exposed to shop owners

`kiosk.py:1060` returns `user_email`, `user_phone` and `user_id` on every job in
the owner-facing feed. A shop owner can see that a named student printed a
named document.

### B3 — Subscription pricing is not configurable

`SUBSCRIPTION_PLANS` and `DURATION_DISCOUNTS` are hardcoded Python literals
(`subscription.py:61-81`). One tier, ₹1800/month, 10% at six months, 15% at
twelve. Changing a price for a negotiation means editing code and redeploying
the VPS.

## Architecture

```
printit-upgrade/
  cloud-backend/          existing — migrations + new endpoints
  printvendo-web/         existing — student PWA (wallet fix only)
  printvendo-owner/       NEW  phone-first PWA    owner.printvendo.com
  printvendo-admin/       NEW  desktop console    admin.printvendo.com
  packages/ui/            NEW  tokens + primitives
```

`packages/ui` is a plain folder — `tokens.css` plus `Button, Panel, Stamp,
Field, Sheet, Table, Money, StatusDot` — copied into each app by a small script
at build time. Deliberately not a workspace or published package: three apps do
not justify monorepo tooling, and copying lets one app diverge without
coordinating a release.

Three apps, three manifests, three deploys. The owner app is installable to a
home screen; the console is not.

## Data model

Every change is additive. Nothing is dropped or renamed, so the existing
dashboard keeps working for the entire transition.

### `printers`

| Column | Type | Notes |
|---|---|---|
| — | — | `paper_capacity` and `paper_used` already exist but **nothing can set them**; see "Paper counts" below |
| `kiosk_type` | enum | PLATFORM / SOLD / SAAS, default PLATFORM |
| `accepts_wallet` | bool | Cached: true only when the kiosk resolves to platform keys. **Defaults false.** |
| `onboarding_stage` | enum | REGISTERED → APPROVED → PRICED → KEYS → KYC → LIVE |
| `onboarding_note` | text | Free text: why it is stuck |

### `subscription_plans` (new)

`id, name, monthly_price, max_kiosks, price_floor_bw, price_ceiling_bw,
price_floor_color, price_ceiling_color, is_active`

### `plan_discounts` (new)

`plan_id, duration_months, percent` — replaces `DURATION_DISCOUNTS`.

### `subscriptions`

| Column | Notes |
|---|---|
| `plan_id` | FK to `subscription_plans` |
| `negotiated_price` | Nullable; overrides the plan price for this owner |
| `free_until` | Nullable; subscription treated as active and unbilled until this date |

### `admin_audit_log` (new)

`actor_id, action, entity_type, entity_id, before, after, note, created_at`

### Seeding and backfill

`SUBSCRIPTION_PLANS` becomes the **seed** for `subscription_plans`, so the
first run produces numbers identical to today.

`kiosk_type` is backfilled by inference — no owner → PLATFORM, owner with
configured keys → SOLD — and then **corrected by hand in the console**.
Inference is a starting guess, never treated as truth.

`accepts_wallet` is backfilled to **false** and switched on only where the
kiosk demonstrably resolves to platform keys. Wrong in the restrictive
direction costs a student one payment method; wrong in the permissive direction
leaves B1 open.

## Paper counts

`paper_capacity` and `paper_used` exist on `printers`, but no endpoint sets
either. Today the only paper operations are `reset` (used → 0, capacity → 250
default) and `out-of-paper` (used → capacity). A tray is not always 250 sheets
and a refiller does not always fill it to the top, so both numbers must be
settable directly.

- **Capacity** is set by the owner or admin on the kiosk — a real tray size.
- **Sheets remaining** is set by the refiller or the owner after a refill —
  "I loaded 120" or "there are 37 left" — not only "reset to full".
- Every change still writes a `PaperRefillLog` row with who set it, the value
  before, and the value after, so the audit trail survives.

New: `PUT /kiosk/printers/{id}/paper` accepting `{capacity?, sheets_left?}`,
and `PUT /refiller/printers/{id}/paper` accepting `{sheets_left}`. The existing
reset and out-of-paper endpoints stay as shortcuts.

## Printer status and restarts

Both already work end to end and were simply not surfaced properly.

**Restart** — `POST /pi/{printer_id}/admin/restart-service` takes a `service`
parameter, and the Pi agent already handles both values (`pi-agent.py:239`,
`service in ("cups", "pi-agent")`). So the kiosk page offers two distinct
buttons rather than one vague "restart":

- **Restart agent** — the job listener reconnected; use when the kiosk shows
  offline but the machine is on
- **Restart CUPS** — the print service; use when jobs queue but nothing comes
  out of the printer

**Status** — `set-status` already accepts ONLINE / OFFLINE / MAINTENANCE for
owners, and the admin endpoint additionally accepts ERROR / DISABLED. The kiosk
page presents these as an explicit three-way control for owners (with the two
extra states admin-only), not a single toggle:

| State | Means | Student sees |
|---|---|---|
| ONLINE | Open, accepting jobs | Listed and selectable |
| MAINTENANCE | Owner is working on it | Listed, not selectable, "under maintenance" |
| OFFLINE | Closed; also marks the kiosk inactive | Not listed |
| ERROR | Jam, toner, needs a human (admin) | Not selectable |
| DISABLED | Suspended by you (admin) | Not listed |

### Student-app gap found

`printvendo-web/lib/printerState.ts` maps anything that is not
ONLINE/BUSY/PRINTING/ERROR to `offline`, so a kiosk in **MAINTENANCE** is
labelled "Offline" to students. Behaviour is right — it correctly refuses new
jobs — but the wording is wrong. Add a `maintenance` state to the mapping and
label it "Under maintenance". Small fix, folded into this work.

## Every action stays on the kiosk page

Both the owner app and the console show the **full action set** for the kiosk
being viewed — the old `printer-detail.html` is the reference and nothing on it
is lost:

| Action | Owner | Admin | Endpoint |
|---|---|---|---|
| Edit pricing / reset to centralized | yes | yes | `PUT /kiosk/printers/{id}/pricing` |
| Set paper capacity | yes | yes | `PUT /kiosk/printers/{id}/paper` |
| Set sheets remaining | yes | yes | `PUT /kiosk/printers/{id}/paper` |
| Mark refilled (shortcut) | yes | yes | existing `paper/reset` |
| Out of paper (shortcut) | yes | yes | existing `paper/out-of-paper` |
| Clear queue | yes | yes | existing |
| Set status — Online / Maintenance / Offline | yes | yes | existing `set-status` |
| Set status — also Error / Disabled | — | yes | existing `/owner/printers/{id}/state` |
| Restart **agent service** | yes | yes | existing, `service: "pi-agent"` |
| Restart **CUPS** (print service) | yes | yes | existing, `service: "cups"` |
| Force-mark job printed | yes | yes | existing |
| Refund a job | own kiosks | platform kiosks | new owner-scoped endpoint |
| Owner email | yes | yes | existing |
| Regenerate config | yes | yes | existing |
| Retire kiosk | request | confirm | existing delete |
| Approve / revoke / assign | — | yes | existing |
| Kiosk type, onboarding stage | — | yes | new |

Destructive actions (clear queue, out of paper, retire, force-printed) sit
behind a confirmation. Retire needs a typed confirmation because money history
hangs off the kiosk.

## API

### Security fixes (first, before any UI work)

- `/wallet/hold` and `/wallet/hold/multi` reject any kiosk with
  `accepts_wallet = false`
- `/printers/` exposes `accepts_wallet`; the student app hides the Wallet
  method for those kiosks, with no explanation on the pay screen (the Wallet
  page itself explains where a balance can be spent)
- Owner job feed drops `user_email`, `user_phone`, `user_id`
- `/wallet/admin/*` switches from `get_non_guest_user` to
  `get_current_admin_user`. The in-body admin check already prevents misuse —
  this removes the misleading dependency, it is not a hole.

### Owner-scoped (new)

```
GET  PUT  /kiosk/printers/{id}/pricing      limit-checked against the plan
PUT       /kiosk/printers/{id}/paper        capacity and/or sheets remaining
PUT       /refiller/printers/{id}/paper     sheets remaining, logged
POST      /kiosk/jobs/{id}/refund           own kiosks only, own Razorpay
GET POST DELETE /kiosk/staff                refillers on own kiosks
GET       /kiosk/earnings                   per period, per kiosk
```

### Admin (new)

```
CRUD /owner/plans  /owner/plans/{id}/discounts
PUT  /owner/subscriptions/{user_id}/terms   negotiated price, free_until
GET PUT /owner/kiosks/{id}/onboarding
GET  /owner/queue                           the unified work queue
GET  /owner/audit
GET  /owner/search?q=
```

## Owner app

Four tabs: **Home · Kiosks · Jobs · Account**.

**Home** leads with money, as decided — with one deliberate exception: a kiosk
that is genuinely down pins above the earnings. "₹1,240 this week" printed over
a dead machine is a lie of omission.

**Kiosks** — paper in **sheets** not percent, with capacity and remaining both
editable; status, today's takings, pricing within the plan's limits, staff,
config download, agent restart. The full action set from the table above lives
here, with destructive ones behind confirmations.

**Desktop layout.** Above 1024px the owner app becomes two columns with a left
sidebar (Home · Kiosks · Jobs · Account) instead of a bottom bar, and the kiosk
page puts actions in a right-hand rail beside the detail. Same components, same
tokens — a layout change, not a second app.

**Jobs** — searchable history, filename but no student identity, refund action
on their own kiosks.

**Account** — subscription with kiosk usage against the cap, Razorpay setup,
KYC and bank, help. When keys are not configured this screen leads with that,
because it is the single most valuable thing an unconfigured owner can do.

## Admin console

Five sections: **Queue · Owners · Kiosks · Catalogue · System**.

**Queue is home.** The six scattered admin pages collapse into filters on one
list — approvals, KYC, Razorpay requests, expiring subscriptions, stalled
onboarding — each row actionable inline. No settlements row: there are no
settlements.

**The owner page is the centrepiece**: subscription and negotiated terms, KYC,
Razorpay status, their kiosks, revenue and audit history on one page. This is
the fix for "scattered". Revenue here is money already in the owner's own
Razorpay account — never a balance Printvendo owes them, because it never
owes them anything.

**Kiosk page** — type, onboarding stage, health, paper, jobs, pricing,
assignment, approve/revoke.

**Catalogue** — plans, discounts, price limits, paper shop items.

**System** — storage cleanup, audit log, exports.

Global search spans owners, kiosks, payments and jobs.

## Rollout

```
1  migrations (additive)      old dashboard unaffected
2  security fixes + backfill  B1 and B2 closed
3  new endpoints + tests
4  printvendo-owner           build, then point owners at it
5  printvendo-admin           build, then switch yourself
6  retire the old dashboard
```

## Testing

Backend tests cover the money-critical paths:

- wallet hold rejected at a SOLD or SAAS kiosk, accepted at PLATFORM
- price changes rejected outside the plan's floor and ceiling
- plan and discount maths producing **numbers identical to the hardcoded
  version** for every existing duration
- owner refund scoped to own kiosks — refunding another owner's job is 403
- audit rows written for every money- and access-affecting action
- owner job feed contains no `user_email`, `user_phone` or `user_id`

The existing 170 tests must keep passing.

## Risks

**The plans migration.** Money maths must produce identical output on day one.
Mitigated by seeding from the existing constants and testing old-versus-new for
every duration before the hardcoded path is removed.

**The `accepts_wallet` backfill.** Defaults to false; enabled only where keys
provably resolve to the platform. Restrictive by construction.

**Scope.** Two new apps plus backend changes is the largest piece of work in
this repo so far. Build order exists to keep something usable at every point:
the old dashboard serves whichever audience has not yet been migrated.

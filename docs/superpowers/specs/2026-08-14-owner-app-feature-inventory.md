# Owner app — what a shop can do

**Date:** 2026-08-14
**Status:** inventory for review, not yet a design
**Purpose:** settle the complete list of what an owner needs, so the gaps are
a decision rather than an oversight.

Three columns: what exists today, what the redesign spec planned, and what
neither of us listed but a shop will ask for.

---

## 1. Getting started — the first hour

A new owner's first session decides whether they trust the system. Today it
is the weakest part: they receive a login and a kiosk that someone else
registered, and the app explains none of it.

| Capability | State | Note |
|---|---|---|
| Sign in | **built** | No sign-up by design — accounts are minted by you |
| See their kiosks | **built** | |
| Connect Razorpay | **partial** | Status is shown; setup itself is an email to you |
| Bank details for KYC | **gap** | Endpoint exists (`/bank-details`), no screen |
| Upload KYC documents | **gap** | Admin can read them; owner cannot send them |
| Know what is blocking go-live | **gap** | `onboarding_stage` exists and the owner never sees it |
| Guided first-run | **gap** | Nothing tells a new owner what to do next |

**The onboarding gap is the important one.** The backend tracks
REGISTERED → APPROVED → PRICED → KEYS → KYC → LIVE, and refuses to set an
owned kiosk live until its Razorpay keys work. The owner sees none of that —
so a kiosk that cannot go live looks broken rather than incomplete. A
checklist showing the six steps, which are done, and what to do about the
next one, would remove most first-week support mail.

## 2. Running a kiosk day to day

Strongest area. The kiosk page carries the full action set.

| Capability | State |
|---|---|
| Paper: set tray size and sheets remaining | **built** |
| Paper: filled / out-of-paper shortcuts | **built** |
| Prices within the plan band | **built** |
| Status: open / maintenance / closed | **built** |
| Restart the agent, restart printing | **built** |
| Clear a stuck queue | **built** |
| Regenerate the kiosk config file | **built** |
| Force a job to "printed" | endpoint exists, **no button** |
| Rename a kiosk, edit its location | **gap** |
| Edit opening hours | **gap** — no model support |
| See the agent version, know it is outdated | **gap** — column exists |

## 3. Money

| Capability | State |
|---|---|
| Revenue, windowed and lifetime | **built** |
| Per-kiosk breakdown | **built** |
| Day-by-day chart | **built** |
| Job history with search | **built** |
| CSV export over any date range | **built** |
| Refund to wallet or original method | **built** |
| Subscription history and invoices | **built** |
| GST-compliant invoice fields | **gap** — GSTIN, HSN, place of supply |
| Payout reconciliation against Razorpay | **gap** |
| Daily or weekly email summary | **gap** |

**GST is the one that will bite.** An Indian shop with turnover above the
threshold needs invoices carrying a GSTIN, an HSN/SAC code and place of
supply. Our subscription invoice has none of these, which makes it unusable
for their accountant. This is a compliance question before it is a code
question — worth confirming with yours.

## 4. People

| Capability | State |
|---|---|
| Assign an existing refiller to a kiosk | **built** |
| Remove a refiller | **built** |
| Invite a refiller who has no account | **gap** — admin-only today |
| See what a refiller actually did | **gap** — `PaperRefillLog` exists, unsurfaced |
| A second manager login for the same shop | **gap** |

The refill log gap is cheap and worth doing: the data is already recorded,
and "who last filled this and when" is the first question when a tray is
empty.

## 5. Knowing something is wrong

Currently the owner only finds out by opening the app.

| Capability | State |
|---|---|
| Kiosk offline | email exists server-side, no owner control |
| Paper low | email exists server-side, no owner control |
| Choose which alerts to get, and where | **gap** |
| Push notifications | **gap** — VAPID exists for students only |
| Alert history | **gap** |

## 6. Suggestions neither of us listed

Ordered by how often a shop will hit them.

1. **A refill log on the kiosk page.** Data already exists. Cheapest item here.
2. **Rename and relocate a kiosk.** Shops move a machine between counters and
   currently must email you.
3. **A printed QR poster for the kiosk.** Students find a shop by walking past
   it. A generated A4 sheet with the kiosk name and a link is a print job the
   shop can run on its own machine.
4. **Agent version visible, with "update available".** The column is populated
   and nothing reads it; a stale agent is a support call waiting to happen.
5. **Busiest-hours view.** Data is in `payments.created_at`. Tells a shop when
   to staff the counter and when to refill.
6. **Paper forecast** — "about 3 days of paper left at your current rate".
   Turns the existing low-paper number into an action.
7. **Dispute trail on a refunded job.** A note field on a refund, so a later
   argument about a refund has a record.
8. **Opening hours**, so a closed shop stops taking jobs automatically instead
   of relying on the owner remembering to set Closed.

## 7. What I would not build

Recorded so the decision is deliberate rather than forgotten.

- **Owner-initiated kiosk delete.** Money history hangs off a kiosk. A request
  that you confirm is safer, and the spec already says so.
- **Owner-set plan or subscription price.** Those are your commercial terms.
- **Student contact details, ever.** Repeatedly requested by shops, and the
  reason B2 existed. The answer stays no.
- **Owner-issued wallet credit.** Wallet money is platform money; letting an
  owner mint it recreates the money leak from the other direction.

## 8. Blocked on the deferred cleanup

These cannot be built honestly until the test-versus-production data work is
done, because each would show numbers we know to be wrong:

- Payout reconciliation (needs trustworthy payment statuses)
- Paper forecast (needs real consumption, not test jobs)
- Busiest hours (same)

## Open questions

1. Does GST invoicing apply to your owners, and at what turnover?
2. Should an owner be able to invite a refiller directly, or does every
   account stay minted by you?
3. Alerts: email only, or push as well? Push means a service worker in this
   app, which it currently and deliberately avoids.
4. Opening hours: worth a schema change, or is the manual Closed switch enough?

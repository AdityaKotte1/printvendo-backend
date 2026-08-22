# printvendo-web against the new backend: what maps, what is missing

**2026-08-22.** Written after Phase 1 landed (`02107ab`). Every claim below was
checked against source on both sides; file and line references are given so the
next reader can disagree with the evidence rather than with the summary.

One method note, because it nearly produced a wrong finding: the app's calls
are **not** all in `lib/`. `pages/wallet.tsx` and `pages/profile.tsx` call
`apiJson` with literal paths of their own, and a grep of `lib/*.ts` alone
reported a missing wallet statement that the app has had all along. Scan
`pages/` and `components/` too.

`printvendo-web` currently talks to **`cloud-backend`**, the legacy API, and
makes no call to `printvendo-backend` at all (`printvendo-web/CLAUDE.md`:
"Makes zero backend changes"). So "is the app using everything the backend
offers" has two halves, and both matter:

1. what the app does today that the new backend **cannot serve**, and
2. what the new backend serves that the app **never asks for**.

---

## 1. What the app calls, and where each call lands

Sources: `printvendo-web/lib/api.ts`, `lib/queries.ts`, `lib/flowApi.ts`.

| The app calls (legacy) | New backend | Note |
|---|---|---|
| `POST /auth/register`, `/login`, `/guest`, `/google` | `POST /v1/app/auth/*` | Same shapes, new prefix. The app already refreshes on 401 through a cookie (`lib/api.ts:15-60`), which is exactly what this backend expects. |
| `GET /auth/me` | `GET /v1/app/auth/me` | Now also carries verification status. |
| `GET /printers/` | `GET /v1/app/kiosks` | **Loses location** — see §2.1. |
| pricing off the printer object | same response | `price_*` fields are on `StudentKioskResponse`. |
| `paperLeft(printer)` (`lib/queries.ts:63`) | `sheets_remaining`, `is_out_of_paper` | Better: derived from the tray, so the flag cannot disagree with the number. |
| `POST /jobs` upload | `POST /v1/app/documents` | |
| `GET /jobs/summary` | `GET /v1/app/orders` + `GET /v1/app/documents` | The legacy "job" is split into a document and an order line. |
| `DELETE /jobs/{id}` | `DELETE /v1/app/documents/{id}` | Refuses while a task is queued, which the legacy one did not. |
| `POST /wallet/hold` **+** `POST /printers/{id}/print` | `POST /v1/app/orders` → `POST /v1/app/orders/{id}/pay/wallet` | The two-call hold is the hazard the `Order` aggregate exists to make unreachable. `flowApi.queuePrint`'s second call disappears; there is nothing left to forget. |
| `POST /wallet/hold/multi` | one order with several items | |
| `POST /payments/jobs/order` | `POST /v1/app/orders/{id}/checkout` | |
| `POST /payments/verify` | `POST /v1/app/orders/{id}/verify` | |
| `GET /wallet/me` | `GET /v1/app/wallet` | |
| `POST /wallet/topup/order` (`pages/wallet.tsx:120`) | `POST /v1/app/wallet/topup` | Always the platform's Razorpay account, deliberately. |
| `GET /wallet/ledger` (`pages/wallet.tsx:68`) | `GET /v1/app/wallet/statement` | The wallet page already renders a statement, including pending top-ups. |

That is the whole print-and-pay path. **It maps.** The rewrite of `lib/api.ts`
is real work but holds no surprises.

---

## 2. What the app does that the new backend cannot serve

These are backend gaps, not app bugs. Each is a feature a student can use today
and could not use the morning after cutover.

### 2.1 A shop has no location, so "nearest" cannot work

`StudentKioskResponse` (`app/api/schemas.py:230-248`) carries id, name, wallet
acceptance, paper and prices — and no coordinates. The `Kiosk` model has
`latitude`, `longitude` and `location_description`
(`app/modules/kiosks/models.py:73-75`); the student response simply does not
expose them.

The app sorts shops by distance and shows how far each is
(`lib/queries.ts:42` `distanceMetres`, `lib/useGeolocation.ts`,
`components/flow/ShopPicker.tsx:41`), and the geolocation permission prompt is
already a module-level singleton because three screens wanted it. Without
coordinates the shop picker becomes an unordered list of names.

**Smallest honest fix:** add `latitude`, `longitude`, `location_description` to
`StudentKioskResponse`. There is no privacy argument against it — a shop's
address is public, and the type already refuses to carry anything about the
*owner*.

### 2.2 Favourite shops do not exist

`lib/api.ts` has `getFavoritePrinters`, `addFavoritePrinter`,
`removeFavoritePrinter`; `ShopPicker` renders them. The new backend has no
notion of a favourite — no table, no route (`grep -ri favo app/` is empty).

A student with twelve shops on campus and one they always use loses that.

### 2.3 The paper shop has a model and no door

`ItemKind.SHOP_ITEM` exists in `app/modules/orders/models.py:75-79`, with a
comment explaining that the old backend modelled these as print jobs with no
file. **Nothing else in the codebase mentions it**: no catalogue table, no
listing route, no way to put one in an order.

The app has a whole page for it (`pages/paper.tsx`, `listPaperProducts`,
`createPaperJob`). At cutover that page has nothing to call.

### 2.4 Push notifications: keys, no endpoints

`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` and `VAPID_SUBJECT` are in
`app/core/config.py` and are read by nothing. There is no `/push/subscribe` or
`/push/unsubscribe`, and nothing sends a web push.

The app subscribes from `pages/profile.tsx` via `lib/pushNotifications.ts`.
"Your print is ready" is the notification, and it is the reason a student keeps
the PWA installed.

### 2.5 No invoices, list or PDF

Two calls, both unanswered by the new backend:

- `GET /payments/my/invoices` — the list on `pages/profile.tsx:66`;
- `GET /payments/{id}/invoice-link` — `lib/api.ts:262`, which then follows the
  returned URL to the PDF.

(The new backend does have `billing`'s invoices, which are the platform billing
an *owner*. A different document for a different person.)

Note that the *shape* of the legacy PDF link — a URL handed to the browser — is
the pattern this backend deliberately refuses for account-ownership proofs: a
proof is bytes from an authenticated route, never a URL. Whatever replaces this
should be the same: `GET /v1/app/orders/{id}/invoice` returning the file.

---

## 3. What the backend offers that the app never asks for

### 3.1 Three pages the backend's emails point at do not exist — and mail is now real

Since `38c15be` invitations, verification and resets are actually sent. The
links are built in `app/core/notifier.py`:

- `:120` `{APP_BASE_URL}/verify-email?token=…`
- `:133` `{APP_BASE_URL}/reset-password?token=…`
- `:147` `{APP_BASE_URL}/accept-invite?token=…`

`APP_BASE_URL` defaults to `https://printvendo.com`, which is this app. None of
those three routes exists in `printvendo-web/pages/` — the app's only nod to a
forgotten password is a `mailto:` support link (`pages/login.tsx:276-280`).

**This is the highest-value item in the document.** The backend has a complete
password-reset flow, a verification flow and a consent-based staff invitation
flow, and all three currently end on a 404. `accept-invite` is the worst of the
three: it is how a shop that has just bought a kiosk gets access to it.

### 3.2 An order is readable on its own

`GET /v1/app/orders/{order_id}` exists; `pages/job.tsx` still reads a legacy
job. Worth taking during the rewrite rather than after: the order response
carries the frozen quote, the state and the per-item breakdown that the job
shape does not.

### 3.3 Change password while signed in

`POST /v1/app/auth/change-password` (which ends every other session) has no UI.
`pages/profile.tsx` has the natural place for it.

### 3.4 Photo layout

`POST /v1/app/documents/photo-layout` composes images onto an A4 sheet
server-side. `pages/photos.tsx` deliberately routes to the PDF flow instead and
`printvendo-web/CLAUDE.md` explains why (the canvas editor is a feature, not a
screen). Worth revisiting only *because* the server can now do the layout —
this may be a smaller job than it was.

### 3.5 Resend verification

`POST /v1/app/auth/resend-verification` exists. Nothing in the app offers it,
which matters more now that unverified users can still sign in: the status is on
`/me` and there is no way to act on it.

---

## 4. Suggested order

1. **The three token pages** (`/verify-email`, `/reset-password`,
   `/accept-invite`). They are small, they are pure app work, and until they
   exist the backend is sending people to a 404 today.
2. **Kiosk location on `StudentKioskResponse`.** One schema, one mapping, and
   the shop picker keeps working.
3. **`lib/api.ts` rewritten** to the new contract, with the two-call wallet hold
   collapsed into the order aggregate. This is Phase 4 of the agreed plan.
4. **Decide, out loud, about favourites, the paper shop, push and invoices.**
   Each is a feature students have today. Any that survives cutover needs
   backend work; any that does not needs saying so, because "we forgot" and "we
   dropped it" look identical afterwards.

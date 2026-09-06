# Turning colour off at a kiosk

**Date:** 2026-09-06
**Status:** approved, building

## Why

A shop's machine may be mono-only — the first Windows kiosk in the field runs a
Kyocera ECOSYS M2040dn, which is a monochrome laser and will never print in
colour. Today the student app offers colour there, prices it, and takes the
money; the agent then refuses the job, because `runner.py` will not send colour
work to a mono pool. The student has paid for something that cannot happen.

Nothing on the server knows a shop cannot do colour. That is the gap.

## What it is

**One switch per kiosk, with no reason recorded.** It means both "this machine
cannot print colour" and "colour is off today because the toner ran out". The
system does not distinguish them, deliberately: two meanings would need two
fields, and the second would be a field nobody maintains.

Set by **an owner for their own shops and an admin for any shop**, through one
route and one kiosk scope — not a second admin route. This follows the rule
already recorded in `CLAUDE.md`: *admin is a wider scope, never a bypass.*

## The field

`kiosks.offers_colour: bool`, `NOT NULL`, default `true`, `server_default="true"`.

Same table, same shape and same kind of control as `accepts_wallet`, which is
the closest existing thing — so it reads as one of the family rather than as a
new idea.

### Why a column rather than something derived

`is_out_of_paper` is derived from the tray, deliberately, so that it cannot
disagree with the number printed beside it. Colour has nothing equivalent to
derive from:

- **The colour prices cannot carry it.** `price_color_*` are nullable, but
  `effective_prices()` fills a null with the platform default. A null already
  means "charge ₹10", so overloading it to mean "no colour" would silently
  switch colour off at every kiosk migrated without colour prices.
- **The device cannot report it.** A kiosk configured with a single `printer`
  and no pools does not know whether that printer is colour, and the agent has
  no way to find out. A kiosk with no machine enrolled yet reports nothing at
  all. And neither could ever express "the toner ran out", which this switch
  also has to mean.

## The rule

`orders.place_order` refuses an order containing a colour item at a kiosk with
`offers_colour = false`, beside the existing wallet and paper checks — the
function whose docstring already says *everything that can refuse an order
refuses it here, before any money moves.*

The sentence the student sees:

> This shop is not printing in colour at the moment. Choose black and white, or
> pick another shop.

A mixed order — one colour file, one black-and-white — is **refused whole**.
Silently dropping the colour file to mono would change what somebody is paying
for without telling them.

`printvendo-web` disabling the control is a courtesy, not the rule. The app is
not the only thing that can POST an order.

## What is deliberately not done

- **Prices are untouched.** Colour prices stay set and stay settable while
  colour is off, so turning it back on restores what the shop had rather than
  making an owner retype it.
- **Paid colour orders already queued are left alone.** The money was taken;
  cancelling them would strand a student who is on their way to the shop.

## Surfaces

| Where | What |
|---|---|
| `kiosks` | the column, `set_offers_colour`, one migration |
| `orders` | the refusal in `place_order` |
| `api` | `POST /v1/owner/kiosks/{kiosk_id}/colour`, owner + admin through one scope; `offers_colour` on the student, owner and admin kiosk payloads; entries in both matrices |
| `printvendo-admin` | a toggle on the kiosk page |
| `printvendo-owner` | the same toggle |
| `printvendo-web` | mono shops greyed with a reason; the black-and-white swap; colour excluded from the price band |

## The student app, and jobs prepared before a shop is chosen

`?later=1` skips the shop step: files are uploaded, options are kept in
`localStorage` by `rememberOptions`, and the shop is chosen afterwards. So
colour is picked when no kiosk exists, and the two only meet at the shop step.

`ShopPicker` already answers this shape of question for paper:

```ts
const paper = checkPaper(printer, sheetsNeeded);
const selectable = canAcceptJobs(printer) && paper.ok;
```

A shop that cannot take *this* job is listed, greyed and labelled — never
hidden, because *hiding them would make a shop vanish from the map for a reason
nobody standing in front of it could understand.* Colour gets the same
treatment, not a new one: `checkColour(printer, needsColour)` beside
`checkPaper`, and the card reads **Black and white only**.

### The swap

A student who prepared a colour job and walks to a mono-only shop would
otherwise face every nearby shop greyed out and no way forward. So the card
offers:

> **Black and white only.** Print these in black and white here for ₹18 instead
> of ₹60?

One tap, re-priced from that shop's own black-and-white rates, options rewritten
before `place_order` is called. Explicit — never a silent downgrade.

### The price band

`estimateBand` spreads rates across nearby shops for a prepared job. A mono-only
shop holds colour prices it will never charge, so leaving it in the colour side
of the band advertises a "from ₹X" that no shop will honour. It is excluded.

## A bug found while designing this, fixed here

`lib/sheets.ts`:

```ts
export function sheetsForJob(job: Job): number | null {
  if (job.sheets != null) return job.sheets;
  ...
  return sheetsForOptions(job.page_count, {
    ...,
    pageRange: '',          // ← always empty
  });
}
```

An empty range means "all pages", so a 60-page PDF counts as 60 pages however
few were selected. Ten pages duplex is five sheets, counted as thirty: a shop
with fifty sheets left refuses the job.

It only bites when `job.sheets` is null, which is exactly a **prepared job with
no order yet** — later mode. The local `Job` type never carried `page_range` at
all, which is why it was hardcoded; `new.tsx` sets `copies`, `colour` and
`duplex` on the job it builds and not the range.

Fixed by carrying `page_range` on the job and passing it through. The price was
always right — `estimateRupees` calls `computeEffectivePages` properly — so only
the paper guard was lying.

It belongs in this piece of work because it is the same screen answering the
same question, *can this shop take this job*, and both answers were wrong for
the same kind of job.

### Known and not fixed here

A prepared job's page range lives **only in `localStorage`**. Documents carry no
print options server-side until an order exists, so clearing site data or
opening the job on another device loses the range, and both the sheets and the
price are then computed against the whole document. Fixing it means storing
prepared options server-side, which is its own piece of work.

## Tests

- `place_order` refuses a colour item at a mono kiosk and still accepts black
  and white there.
- A mixed order is refused whole.
- An owner and an admin can both flip the switch; a stranger gets 404, not 403.
- The student, owner and admin payloads carry `offers_colour`.
- Both matrices name the new route.
- **Mutation test:** delete the check in `place_order` and confirm a test goes
  red, so the guardrail is known to work rather than assumed.

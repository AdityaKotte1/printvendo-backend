# PrintVendo Backend — Printing Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Documents, print options, and the print-task queue — such that **what a student asked for is exactly what comes out of the printer, once**.

**Depends on:** foundation, identity, kiosks, payment configs, billing, gate (complete — 502 tests)

---

## What is actually wrong with the current print path

Read before designing anything. Three defects, one of which prints things twice.

### P1 — Nothing claims a job, so it can print twice

`GET /pi/{printer_id}/jobs/next` (`routers/pi.py:372`) selects the oldest
`QUEUED_ON_SERVER` printer job, returns it, and **never changes its status**.

The agent calls `fetch_next_job` from **two** places: the main loop
(`agent.py:1236`) and the prefetch worker (`agent.py:1148`). The status only
becomes `QUEUED_ON_PI` *after* the agent has decided to print
(`agent.py:1242/1252/1301`).

So between the fetch and the status update, the same task is available to be
fetched again. Two fetches in that window return **the same job**, both download
it, both print it.

The same hole reopens on restart: an agent that prints and then crashes before
its status call leaves the task `QUEUED_ON_SERVER`, and prints it again on the
next poll.

**This is the "if multiple sent it should not print the first one multiple
times" symptom.**

### P2 — The options blob is interpreted in four independent places

`Job.options` is a `Text` column holding JSON. It is parsed, with its own
`try/except` and its own defaults, in:

| Where | Decides |
|---|---|
| `jobs.py:_compute_price_rupees` | what the student is charged |
| `jobs.py:_sanitize_options_json` | what gets stored |
| `pi.py:_estimate_sheets_for_job` | how much paper is deducted |
| `agent.py:build_lp_command` | what the printer actually does |

Four readings of one blob. Nothing forces them to agree, so the price charged,
the paper counted, and the pages printed are three independent opinions. A
malformed value silently becomes a different default in each.

### P3 — The device payload looks up payments by `PAID` only

`pi.py:413` filters `Payment.status == "PAID"`, so a wallet payment that has
already become `CAPTURED` is invisible and `payment_method` is reported as
`UNKNOWN`. Same split-brain as the audit found elsewhere.

---

## The design

### D-P1 — A task is claimed atomically, or not handed out at all

One statement selects and claims:

```sql
UPDATE print_tasks SET status = 'sent_to_device', claimed_at = now()
WHERE id = (
    SELECT id FROM print_tasks
    WHERE kiosk_id = :kiosk AND status = 'queued'
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

`FOR UPDATE SKIP LOCKED` is the standard Postgres work-queue pattern: a second
concurrent caller skips the locked row and gets the next one, or nothing.
**Two fetches can never receive the same task**, however many agents, threads or
prefetch workers are running.

Crash recovery is a separate, explicit mechanism — a task stuck in
`sent_to_device` past a lease deadline is requeued by a sweeper, deliberately,
rather than by being left claimable the whole time.

### D-P2 — Options are a validated value object, interpreted once

`PrintOptions` is parsed and normalised **at order time**, stored in explicit
typed columns, and never re-parsed:

| Column | Meaning |
|---|---|
| `colour` | bool. Not "color" as a loose truthy blob. |
| `duplex` | bool |
| `copies` | int ≥ 1 |
| `page_range` | normalised string, or null for "all pages" |

From those, **one** function derives the three numbers everything else needs:

```
pages        the document's length
selected     how many pages the range picks
impressions  selected x copies          -- sides printed, what pricing uses
sheets       ceil(impressions / 2) if duplex else impressions   -- paper used
```

Pricing, paper accounting and the device payload all call that one function.
They cannot disagree, because there is nothing to disagree about.

**Three words, three meanings, never conflated** — `pages` is document length,
`impressions` is sides printed, `sheets` is physical paper. The owner app
already names them separately for the same reason.

### D-P3 — The device is told what to do, not asked to work it out

The payload the agent receives carries resolved values:

```json
{
  "task_id": "tsk_...",
  "file_url": "...",
  "copies": 2,
  "duplex": true,
  "colour": false,
  "page_range": "1,4-6",
  "expected_sheets": 6
}
```

The agent maps those to CUPS flags and nothing more. It does not parse JSON
options, does not apply defaults, and does not decide anything. `expected_sheets`
is included so the agent — and later, an operator — can see whether what came
out matches what was asked for.

### D-P4 — The page range is applied exactly once

Parsed and normalised at order time (`"12-17,1"` → `"1,12-17"`), validated
against the real page count, priced on the normalised selection, and handed to
CUPS as the same normalised string.

The stored PDF is **never** pre-trimmed. Confirmed against the current system:
conversion normalises and downsamples but does not cut pages, so the range
applies once at the printer. Trimming server-side *and* passing `page-ranges`
would apply it twice — asking for pages 5–10 would print pages 5–6 of the
already-cut file.

### D-P5 — Paper is counted from what the printer did, not what we guessed

`workload().sheets` is a **prediction**. The tray is emptied by whatever the
printer actually pulls, and the two can differ — a jam, a driver that starts a
copy on a fresh sheet when we assumed otherwise, a page CUPS decided was blank.

The backend being replaced used its estimate unconditionally, and only deducted
on `PRINTED` (`pi.py:624`). So a job that failed after three sheets deducted
**zero**, and the counter drifted from the physical tray every time anything
went wrong. That is why a kiosk can report paper remaining while being empty.

Three rules:

1. **The device reports what it used.** CUPS exposes
   `job-media-sheets-completed` for a finished job; the agent reads it and sends
   it with the status update.
2. **Actual beats predicted.** Paper is deducted from the reported figure when
   there is one, and from `workload().sheets` only when there is not — an agent
   too old to report it, or a print path that cannot tell.
3. **A failed print still consumed paper.** A task that fails deducts whatever
   the device reports it managed, rather than nothing. Half a job still empties
   half a tray.

When prediction and reality differ, the difference is recorded on the refill log
rather than silently absorbed. A kiosk whose printer consistently uses more
sheets than expected is telling you something — a duplex setting the driver is
ignoring, most likely — and that signal is worth keeping.

**This also bounds the waste.** Paper is reserved against the *predicted* sheets
when an order is placed, so a kiosk cannot accept a job it has no paper for, and
the reservation is reconciled against the actual figure once the job finishes.

### D-P6 — Duplex rounds per copy, and that is correct rather than wasteful

Two copies of a five-page double-sided document is **six** sheets, not five.
Copies do not share a sheet: the last page of copy one and the first page of
copy two must not end up on the same piece of paper, or neither copy can be
handed to anyone. CUPS starts each copy on a fresh sheet, so six is also what
the printer really pulls.

### D-P5b — Several documents means several tasks, each printed once

An order with three files produces three `PrintTask` rows. Each is claimed
independently by D-P1, so none can be duplicated and none can be skipped.
Ordering within an order is by position, so a student's files print in the order
they chose.

---

## Task list

1. **Print options** — `PrintOptions` value object, page-range parser/normaliser, and the pages/impressions/sheets calculation. Pure logic, heavily tested.
2. **Document model + migration** — uploaded file, page count, storage paths, retention state.
3. **PrintTask model + migration** — queue row, status enum, lease fields, ordering.
4. **The claim** — atomic `FOR UPDATE SKIP LOCKED` fetch, plus the lease sweeper for crashed agents.
5. **Document pipeline** — upload, PDF validation, page counting, Ghostscript normalisation under `-dSAFER` with timeouts.
6. **Device API** — `/v1/device/*`: register, heartbeat, claim next task, download file, report status.
7. **Student API** — upload, list, delete documents.
8. **Module surface, contracts, docs.**

Tasks 1-4 are where correctness lives. 5 onwards is plumbing.

---

## Done when

- Two concurrent claims on one queue never return the same task (tested with real concurrent transactions, not mocks)
- An agent that dies mid-print does not cause a reprint on restart; the task is requeued only by the lease sweeper
- Price, paper deduction, and the printed output all derive from one options calculation, and a mutation to it fails tests in all three
- `"12-17,1"` normalises to `"1,12-17"`, is priced on 7 pages, and is printed as `1,12-17`
- A range naming pages beyond the document is refused at order time, not silently clamped at the printer
- Duplex deducts sheets, not impressions
- An order of three files yields three tasks, printed once each, in the student's order
- Paper is deducted from what the device reports it used, falling back to the prediction only when the device cannot say
- A print that fails halfway deducts the sheets it actually consumed, not zero
- A kiosk cannot accept a job it has insufficient paper for, because the predicted sheets are reserved at order time
- Predicted-versus-actual differences are recorded, so a driver silently ignoring duplex becomes visible rather than becoming drift

# The screen above the counter

**Date:** 2026-09-07
**Status:** approved, not yet built
**Touches:** `printvendo-agent` (new `display/`), `printvendo-backend` (one field)

## Why

A student walking past a shop has no idea the machine takes prints from a
phone. The counter has a printer on it and nothing that says what it is for.

And when they do use it, the two questions they ask the shopkeeper are "is it
working?" and "has mine printed yet?" — both of which the agent already knows
and nothing currently shows.

So: a screen above the counter. One half teaches, one half reports.

## What it is

A fullscreen display on the shop's own PC, locked so it cannot be walked out
of, showing:

- **left half** — the four steps, looping: scan the QR, choose your files, pay,
  collect. Authored in HTML/CSS, not video.
- **right half** — this shop's live state: online or offline, paper left, and
  the job on the machine right now.

In the ink system, because it is the same product.

## Decisions, and why

### Authored steps, not video

Nothing to film, nothing to manage, no folder per shop that can be empty or
wrong, and it works on the day it is installed. A video pipeline is a second
thing to keep current at every counter; the steps change when the app changes.

### The page is HTML in Edge, not a drawn native UI

The ink system already exists as CSS — `tokens.css`, Anton, the halftone, the
hard offsets. Rebuilding it in Qt stylesheets would make **two** design systems
that drift the first time either changes, which is the failure this codebase
refuses everywhere else. WebView2/Edge is on every Windows 10 and 11, so there
is no runtime to install.

### But the lock is a separate native process

**Browser kiosk mode is not a lock.** Edge's `--kiosk` is a presentation mode:
Alt+Tab, the Windows key, Ctrl+W and Alt+F4 all leave it, and a web page cannot
install a keyboard hook to stop them.

So two processes:

| | What it is | What it does |
|---|---|---|
| **the page** | Edge, `--kiosk`, pointed at `127.0.0.1` | draws the screen |
| **the guard** | the EXE the operator runs | makes it a lock |

The guard installs a global low-level keyboard hook (`WH_KEYBOARD_LL`)
swallowing Alt+Tab, Win, Ctrl+Esc, Alt+F4 and Ctrl+Shift+Esc — global, so it
holds whichever window has focus. It keeps the Edge window topmost and
foregrounded, and relaunches Edge if it is closed.

The browser does what browsers are good at; the lock is a small native thing
doing one job.

### App-level lock, not Windows' own

Assigned Access and Shell Launcher are the real thing — the display *becomes*
the shell, so there is no desktop to escape to. They need Windows Enterprise,
Education or IoT; Shell Launcher does not exist on Home, and Assigned Access on
Pro hosts only UWP apps. The estate is ordinary shop PCs, so the app-level lock
is what actually ships everywhere.

Chosen deliberately as "stops a curious student", not "stops a determined one".

### Unlocking

- **`Ctrl+Alt+U`**, or five taps in the top-left corner for a touchscreen with
  no keyboard. Not a chord anybody presses by accident.
- The PIN is stored **hashed**, in the display's own config file — *not* in
  `agent.json`. That file holds the device token, which **is** the kiosk; this
  is a counter convenience. They must not share a blast radius.
- Three wrong tries starts a backoff that grows. A bored student otherwise has
  all afternoon.
- On success the hook is released, Edge is closed, and the desktop is there.

### Three processes, and the display cannot stop printing

The agent is separate from the guard, which is separate from Edge. A display
that crashes, or a guard that is killed, leaves the agent printing. This is why
the display is not a window the agent opens.

## Where the data comes from

The agent gains a small HTTP server bound to `127.0.0.1` — two routes, `/` for
the page and `/status` for JSON. **No second credential**: the display never
reaches the backend, and holds no token.

Everything on the status half is state the agent already has:

| Field | Where it comes from |
|---|---|
| `kiosk_name`, `queue_depth`, `sheets_remaining` | the heartbeat response, already fetched every 60s and currently discarded |
| `paper_capacity` | **new field** on `DeviceHeartbeatResponse` — the bar needs a denominator and the response already carries the numerator |
| `connected` | whether the last heartbeat succeeded |
| `printing` | the agent's own loop; it is the thing printing |

### The status document carries no filename

The agent already downloads as `prt_abc.pdf` rather than the student's own name,
because `lp` submits under the filename on disk and CUPS keeps job history. A
forty-inch screen above a counter reading *"Medical Results Ravi Kumar.pdf"* is
that same leak, larger and in public.

So the JSON has **no filename field at all**. The display cannot render one
however it is later edited — the same mechanism as `RefillerKioskResponse`
having no money field.

Sheets and state only.

## The screen

Vertical split, one monitor, landscape.

**Left — the loop.** Four steps, a few seconds each, for ever. Scan the QR;
choose your files and settings; pay; collect. Animated in CSS and SVG.

**Right — this shop.** A large online/offline stamp. The paper bar, drawn
against real capacity rather than a guessed ream. While something is printing,
that job's state and how much is queued behind it.

## When things break

- **Agent not running** → the page says so plainly, rather than sitting on a
  dead fetch. A blank screen above a counter reads as a broken machine.
- **Backend unreachable** → last known figures, greyed, marked not connected.
  Never blank, and never a stale number presented as live.
- **Edge closed** → the guard relaunches it.
- **Guard killed** → Edge is left unlocked. Accepted: the guard is small enough
  to be hard to crash, and the threat model is a curious student.

## What cannot be promised

**Ctrl+Alt+Del is reserved by the OS** and nothing in user space blocks it. The
installer disables Task Manager by policy, which closes the useful half of that
door. Holding the power button always wins.

Stated here so it is a decision on the record rather than something discovered
at a shop counter.

## Packaging

One EXE, PyInstaller, in `printvendo-agent/display/`. Installed beside the
agent by `install-windows.ps1`, with `-Pin` setting the unlock PIN.

Not started automatically at first: the operator runs it once the screen is
plugged in and positioned. A display that launched itself at boot on a PC with
no second monitor would cover the shop's own desktop.

## Testing

The status document is a pure function of the agent's state — the same shape as
`build_windows_command`, and testable without a browser, a printer or a screen.
The keyboard hook and the window behaviour are checked by hand at the shop; they
are OS behaviour, and a test that mocked them would only assert that the mock
was called.

## Not in this piece of work

- **Windows Assigned Access / Shell Launcher** for shops on an edition that
  supports it. Worth doing later; it is a different mechanism, not a stronger
  setting of this one.
- **Video or promotional content.** Authored steps only.
- **A second screen.** One monitor, split. A separate status screen is a layout
  change, not an architecture change, if it is ever wanted.

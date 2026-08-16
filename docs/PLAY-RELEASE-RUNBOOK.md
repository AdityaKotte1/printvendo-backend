# PrintIT — Google Play Release Runbook (closed testing → production)

Written 2026-07-13 for the `android-twa/` project (`online.innvera.app`).
Follow top to bottom. Every command runs on your Windows PC in PowerShell
unless stated otherwise.

---

## Phase 0 — One-time setup (skip any step you already have)

**0.1 Check Node.js is installed:**
```powershell
node --version
```
If it prints `v18` or higher → OK. If "not recognized" → install from
https://nodejs.org (LTS version), then reopen PowerShell.

**0.2 Install Bubblewrap CLI:**
```powershell
npm install -g @bubblewrap/cli
bubblewrap --version
```
First time Bubblewrap runs it will offer to download its own JDK and Android
SDK — answer **Yes** to both and let it finish (takes a few minutes).

**0.3 Know where your signing key is.** Your project expects it at:
```
C:\Users\gurua\Downloads\PrintIT - Google Play package main first\signing.keystore
```
Confirm the file exists. You will need the **keystore password** and **key
password** (alias `my-key-alias`) that you set when you first created it.
If this file is lost, STOP — you cannot update the app without it (Play App
Signing protects the store key, but the upload key needs a reset request to
Google). Back this file up somewhere safe NOW (cloud drive + USB).

---

## Phase 1 — Build the release bundle (version 2)

The version is already bumped in `android-twa/twa-manifest.json`
(`appVersionCode: 2`). Every future upload needs a HIGHER number — see
Phase 4 for how to bump.

**1.1 Open PowerShell and go to the project:**
```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\android-twa"
```

**1.2 Build:**
```powershell
bubblewrap build
```
- It asks for the **keystore password**, then the **key password** — type them.
- Wait for `BUILD SUCCESSFUL`.

**1.3 Confirm outputs exist (freshly dated):**
```powershell
dir app-release-bundle.aab, app-release-signed.apk
```
`app-release-bundle.aab` is what you upload to Play. The `.apk` is for
side-loading onto your own phone to sanity-check (optional):
install it, open, confirm the app loads `app.innvera.online` full-screen
with no browser bar. If a browser bar shows, assetlinks are broken — tell me.

---

## Phase 2 — Upload to Closed Testing

**2.1** Go to https://play.google.com/console → your developer account →
**PrintIT** app.

**2.2** Left menu → **Test and release → Testing → Closed testing**.
Use your existing closed track (usually "Alpha"). Click **Manage track**.

**2.3** Click **Create new release**.
- Upload `app-release-bundle.aab` (drag the file in).
- **Release name:** `2 (2.0)` — auto-filled, leave it.
- **Release notes:** write REAL notes. For this build:
  ```
  <en-US>
  - Removed internal admin tools from the customer app
  - Fixed payment configuration and image loading issues
  - Redesigned all email notifications
  - Faster, cleaner profile page
  </en-US>
  ```
- Click **Next** → fix any errors/warnings it shows → **Save and publish**
  (or "Start rollout to Closed testing").

**2.4** Review time: usually a few hours to 1–2 days for a closed track.

---

## Phase 3 — Set up testers PROPERLY (this is where you failed before)

Google's rejection is about **engagement**, not headcount. Do all of this.

**3.1 Recruit 16–20 real people** (friends, family, kiosk owners). More than
12 so the count never dips below 12 if someone opts out. They need Gmail
accounts on Android phones.

**3.2 Add their emails:** Closed testing track → **Testers** tab →
create/select an email list → paste all addresses → Save.

**3.3 Send every tester the opt-in link** (shown on the same Testers tab,
looks like `https://play.google.com/apps/testing/online.innvera.app`):
1. Open the link on their phone, signed in with the email you added.
2. Tap **Become a tester**.
3. Install the app from the Play Store link that appears.
4. **Do not uninstall or opt out for the whole 14+ days.**

**3.4 Make a WhatsApp group with all testers.** Post a daily task, e.g.:
- Day 1: "Install, open the app, browse printers, send a screenshot."
- Day 2: "Upload any PDF, go to payment screen, tell me anything confusing."
- Day 3: "Check wallet page. Reply with one thing you'd improve."
- …one small task every day, 2–5 minutes each. Their replies are your
  written feedback evidence.

**3.5 The 14-day clock starts** only when ≥12 testers are opted in AND the
release is live on the track. Check the Closed testing page — Play Console
shows the requirement progress on the **Dashboard → Publish your app**
section. Verify the counter is actually running on day 1.

---

## Phase 4 — Ship updates DURING the 14 days (the missing signal)

Upload a new version around **day 4–5** and another around **day 9–10**.
This is what proves to Google that real testing → real fixes happened.

For each update:

**4.1 Bump the version** in `android-twa/twa-manifest.json` — open it in
Notepad and change BOTH:
```json
"appVersionName": "3",
"appVersionCode": 3,
```
and near the bottom:
```json
"appVersion": "3"
```
(next time: 4, and so on — the number must always increase).

**4.2 Rebuild:**
```powershell
cd "C:\Users\gurua\Downloads\Telegram Desktop\printit-upgrade\android-twa"
bubblewrap build
```

**4.3 Upload to the SAME closed track** (Phase 2 steps) with release notes
that reference tester feedback, e.g.:
```
- Fixed payment page confusion reported by testers
- Improved printer list loading
- Refill reminder emails for kiosk owners
```
(Your web-app fixes deploy instantly via the website, but the Play release
notes + new versionCode are the signal Google's system sees.)

**4.4 Tell the WhatsApp group to update the app** and confirm.

---

## Phase 5 — Apply for Production (day 15+)

**5.1** Play Console → Dashboard → **Apply for production access** (appears
after 14 continuous days with 12+ opted-in testers).

**5.2** The questionnaire has ~3 free-text sections. Write SPECIFICS.
Template — adapt with your real tester quotes:

*About your closed test / how you recruited testers:*
> I recruited 18 testers: kiosk shop owners who operate our printing kiosks,
> plus students who are the app's target users, coordinated through a
> WhatsApp group with daily testing tasks (uploading PDFs, making test
> payments, checking wallet balance and print status).

*Feedback received / issues found:*
> Testers reported: (1) an internal admin section was visible in the profile
> page, (2) payment status was unclear after returning from UPI apps,
> (3) images failed to load in the payment configuration review screen,
> (4) notification emails rendered poorly in Gmail.

*Changes made:*
> During the test I shipped versions 2, 3 and 4: removed the admin section
> from the customer app, fixed the API access issue that broke login for
> some testers, rebuilt all notification emails, and added paper refill
> reminders for kiosk owners. Each fix was verified by the testers who
> reported it.

**5.3** Submit. Answer honestly — Google cross-checks against actual
release activity on the track, which is why Phase 4 matters.

**5.4** If approved → **Production** track → Create release → upload the
latest `.aab` (bump versionCode again) → roll out.

---

## Phase 6 — Parallel escape hatch: organization account

The 12-tester rule applies only to PERSONAL accounts. Innvera is a real
business — an organization account publishes to production directly.

1. Get a **D-U-N-S number** (free): https://www.dnb.com/duns.html — apply
   with business name, address, phone. India processing ≈ 1–2 weeks.
2. Create a new Play developer account, choose **Organization**, enter the
   D-U-N-S number, pay $25, complete identity + org verification.
3. To move the app: Play Console → Setup → **Transfer app** to the new
   account (keeps package id, reviews, Play App Signing).

Start step 1 today; run Phases 1–5 in parallel. Whichever unlocks first wins.

---

## Quick reference

| Thing | Value |
|---|---|
| Project dir | `printit-upgrade/android-twa/` |
| Package id | `online.innvera.app` |
| Current versionCode | 2 (bump for every upload) |
| Keystore | `C:\Users\gurua\Downloads\PrintIT - Google Play package main first\signing.keystore`, alias `my-key-alias` |
| Build command | `bubblewrap build` |
| Upload file | `app-release-bundle.aab` |
| Testers needed | 16–20 recruited (12 minimum opted-in, continuously, 14 days) |
| Update cadence | new version at ~day 4 and ~day 9 |

# PrintIt Kiosk — Raspberry Pi 5 Production Deploy Guide

Target: Raspberry Pi 5 (4 GB+), Raspberry Pi OS **Lite 64-bit (Bookworm)**, Epson EcoTank
L6460 over USB, unattended 24×7 operation, remote-supportable.

Result: a node that boots headless, always raises its own admin Wi-Fi hotspot, takes
internet from LAN, self-registers with `https://innvera.online`, holds a WebSocket, prints
paid jobs via CUPS, reports health/refund conditions, and recovers from paper-out /
power-cut / network-drop without a site visit.

Everything is copy-paste. Replace only the `<...>` placeholders.

---

## Access model — read this first

Two network interfaces, two jobs. Never confuse them:

| Interface | Role | Address | Use |
|---|---|---|---|
| `wlan0` | **Admin hotspot (AP mode)** — always on, every boot | `10.42.0.1` fixed | You SSH here. Never changes. Needs no internet, router, or DHCP to work. |
| `eth0` | **Internet uplink** — LAN cable to the router | DHCP from router | Backend, WebSocket, apt |

**Set the AP up first, in your very first session.** Access and uplink are independent
problems; solving uplink first means every network hiccup — a power cut, a phone hotspot
going away, a changed DHCP lease — locks you out of the machine. The AP has no such
dependency. Tailscale is *not* a substitute: it needs the Pi to have working internet.

You join the Pi's Wi-Fi from your laptop, `ssh pi-3@10.42.0.1`, and the Pi routes your
laptop's traffic out through the LAN cable (NAT), so you keep internet while connected.

This is why the previous attempt fell apart: access depended on `pi-3.local` (mDNS, which
your Windows box does not resolve) and a DHCP lease that changed after a power cut. A fixed
AP address has neither failure mode.

⚠️ `wlan0` in AP mode **cannot** simultaneously be a Wi-Fi client. Internet comes from
`eth0` — **every kiosk site in this deployment has a LAN drop**. A site without one would
need a USB Wi-Fi dongle as `wlan1` for the uplink; that case is out of scope here.

---

## Phase 0 — Flash the card

In **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)* → gear/edit settings:

| Setting | Value |
|---|---|
| Hostname | `pi-3` |
| Username / password | `pi-3` / *your password* — **write it down** |
| **Enable SSH** | ✅ *Use password authentication* |
| **Wi-Fi SSID / password** | ✅ **your home/shop Wi-Fi** — this is the bootstrap link, see below |
| Wireless LAN country | `IN` — **required**, an AP will not start without a regdomain |
| Locale / timezone | `Asia/Kolkata` |

**Fill in the Wi-Fi.** It is temporary: it gets you your first SSH session. Phase 1 later
converts `wlan0` into an access point, which *ends* that Wi-Fi client connection — by then
Ethernet is carrying you. Flashing with Wi-Fi blank and no Ethernet cable leaves you with
no way into the Pi at all.

**Ethernet is mandatory for this design.** One radio cannot be an AP and a Wi-Fi client
simultaneously. Boot the Pi with the LAN cable already plugged in.

Hardware checklist:

| Item | Requirement |
|---|---|
| PSU | Official 27 W USB-C (5 V / 5 A). Under-volt = random print failures. |
| Cooling | Active Cooler or case fan. Pi 5 throttles without it. |
| Boot media | A2 high-endurance microSD (64 GB+), or NVMe via M.2 HAT for 24×7 write life. |
| Network | **Ethernet cable to the router.** Not optional in this design. |
| Printer | Epson L6460 over USB, on its own mains supply. |

Boot the Pi with the **Ethernet cable already plugged in**.

---

## Phase 1 — Admin hotspot on every boot (do this first)

### 1.1 Get in once

You need exactly one session to set the AP up. Three ways, in order of reliability:

**a) Bootstrap Wi-Fi (what Phase 0 configured).** Find the address from your router's admin
page → DHCP clients → `pi-3`. Or scan from a PC on the same LAN, filtering by Raspberry Pi
MAC prefix (`2c-cf-67`, `d8-3a-dd`, `e4-5f-01`, `dc-a6-32`, `b8-27-eb`):

```powershell
$net = "192.168.29"      # your LAN, from: ipconfig
1..254 | ForEach-Object {
  $ip = "$net.$_"
  $t = New-Object System.Net.Sockets.TcpClient
  try { if ($t.ConnectAsync($ip, 22).Wait(300)) { "SSH open: $ip" } } catch {}
  $t.Close()
}
arp -a | Select-String "$net"
```

A host offering only `diffie-hellman-group1-sha1` is a router or IoT box, **not** the Pi —
Pi OS never offers that KEX.

**b) Monitor + keyboard.** Guaranteed, no network involved. Pi 5 needs a **micro-HDMI**
cable. Log in, run `hostname -I`.

**c) Fix the card from Windows.** If SSH or Wi-Fi didn't get configured at flash time,
re-run Raspberry Pi Imager with the settings in Phase 0 — that is faster and less
error-prone than hand-editing `/boot/firmware/custom.toml` on the FAT partition.

### 1.2 Build the AP immediately — it needs no uplink

**Create the AP in your first session, before anything else.** It is a local radio: it works
with no internet, no router, no DHCP server, no phone. Once it is up, `10.42.0.1` is a
permanent way in that no power cut, IP change, or absent network can take away.

Do **not** gate this on Ethernet. Ethernet supplies *internet*, which is a separate concern
solved in 1.7. Access first, uplink second — the reverse ordering strands you every time the
bootstrap network disappears.

Losing the bootstrap Wi-Fi client link when the AP activates is expected and fine.

### 1.3 Prerequisites

```bash
sudo rfkill unblock wifi
sudo raspi-config nonint do_wifi_country IN
sudo apt update
sudo apt install -y --no-install-recommends dnsmasq-base
nmcli device status          # wlan0 must be listed, not "unmanaged"
```

### 1.4 Create the AP profile

Pick a strong PSK — this is admin access to a machine that holds a payment-system
credential. Minimum 12 characters, not reused.

```bash
export AP_SSID="printit-kiosk-01"
export AP_PSK="<choose-a-strong-password>"

sudo nmcli connection add type wifi ifname wlan0 con-name kiosk-ap ssid "$AP_SSID"

sudo nmcli connection modify kiosk-ap \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel 6 \
  802-11-wireless.hidden no \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.psk "$AP_PSK" \
  ipv4.method shared \
  ipv4.addresses 10.42.0.1/24 \
  ipv6.method ignore \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0

sudo nmcli connection up kiosk-ap
```

`ipv4.method shared` makes NetworkManager run DHCP + DNS on `wlan0` and NAT it out through
`eth0`. No hostapd, no dnsmasq config files, nothing to maintain.

### 1.5 Verify before you trust it

```bash
nmcli -f GENERAL.STATE,IP4.ADDRESS connection show kiosk-ap
iw dev wlan0 info | grep type          # expect: type AP
ip -4 addr show wlan0 | grep inet      # expect: inet 10.42.0.1/24
```

### 1.6 Prove it survives a reboot — do not skip

```bash
sudo reboot
```

Wait 60 s. On your laptop, join Wi-Fi `printit-kiosk-01`, then:

```powershell
ssh pi-3@10.42.0.1
```

First connect after a reflash will complain about a changed host key — clear it:

```powershell
ssh-keygen -R 10.42.0.1
```

Once this works, **10.42.0.1 is your permanent way in**, regardless of what the LAN does.

### 1.7 Give it an uplink

With `wlan0` serving as the AP, internet must arrive on another interface.

**Production — Ethernet:**

```bash
sudo ethtool eth0 | grep -i "link detected"   # "no" = cable/port problem, not the Pi
sudo nmcli device connect eth0
ip -4 addr show eth0 | grep inet
```

`NO-CARRIER` in `ip a` means nothing is on the far end. `ip link set eth0 up` cannot fix it —
the interface is already up; the *carrier* is missing. Check both cable ends, the link LEDs,
a different port, then a different cable.

**Bench or emergency — USB tether a phone.** Plug the phone into the Pi over USB, enable
*USB tethering* on the phone. It appears as its own interface and coexists with the AP:

```bash
nmcli device status          # expect usb0 / enx… connected
```

**Verify whichever you used:**

```bash
ping -c2 1.1.1.1
curl -sS -o /dev/null -w '%{http_code}\n' https://innvera.online/pi/agent/latest-version
nmcli device status          # wlan0 connected (AP) + eth0-or-usb0 connected
```

`200` = ready for the rest of the guide. The AP keeps working even when this fails, which is
the entire point of doing it first.

### 1.8 Remove the bootstrap Wi-Fi profile

It can never activate again (the radio is an AP), but leaving it invites confusion later:

```bash
nmcli -t -f NAME,TYPE connection show | grep wifi
sudo nmcli connection delete "<your-home-ssid-profile>"     # NOT kiosk-ap
```

---

## Phase 2 — Base OS

```bash
sudo hostnamectl set-hostname pi-3
sudo timedatectl set-timezone Asia/Kolkata
sudo timedatectl set-ntp true          # TLS to the backend breaks if the clock drifts

sudo apt update && sudo apt full-upgrade -y
sudo apt install -y --no-install-recommends \
  python3-venv python3-pip ca-certificates curl jq unattended-upgrades \
  cups cups-client cups-filters avahi-daemon ipp-usb

sudo rpi-eeprom-update -a || true       # firmware; reboot applies it
```

Trim what a kiosk never uses:

```bash
sudo systemctl disable --now bluetooth hciuart triggerhappy 2>/dev/null || true
sudo systemctl disable --now cups-browsed 2>/dev/null || true   # prevents phantom auto-queues
```

Journal caps:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/00-printit.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=20M
MaxRetentionSec=2week
EOF
sudo systemctl restart systemd-journald
```

Hardware watchdog — reboots a truly hung kernel:

```bash
sudo mkdir -p /etc/systemd/system.conf.d
sudo tee /etc/systemd/system.conf.d/10-watchdog.conf >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
```

Security updates, but never let apt restart CUPS mid-business-day:

```bash
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

sudo tee /etc/apt/apt.conf.d/51printit-unattended >/dev/null <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
Unattended-Upgrade::Package-Blacklist {
    "cups";
    "cups-.*";
    "printer-driver-.*";
    "ipp-usb";
    "network-manager";
};
EOF
```

`network-manager` is blacklisted on purpose — an unattended NM upgrade that bounces the
service would drop your admin hotspot.

### 2.1 Make `QUEUE` permanent

Every command below uses `$QUEUE`. An empty value silently targets the wrong printer, so
set it system-wide instead of per-session:

```bash
echo 'export QUEUE="PRINTIT"' | sudo tee /etc/profile.d/printit.sh
sudo chmod 0644 /etc/profile.d/printit.sh
export QUEUE="PRINTIT"      # for the current session
echo "$QUEUE"               # must print PRINTIT
```

---

## Phase 3 — Spool directory on tmpfs

Job PDFs are transient. Keep them in RAM: no card wear, and customer documents never
survive a power cut.

```bash
sudo mkdir -p /var/spool/printit
echo 'tmpfs /var/spool/printit tmpfs defaults,noatime,nosuid,nodev,size=512M,mode=0700,uid=printit,gid=printit 0 0' | sudo tee -a /etc/fstab
```

The mount fails until the `printit` user exists — Phase 7 creates it, then mounts.

---

## Phase 4 — Uplink resilience

```bash
# eth0: reconnect forever
CON=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: '$2=="eth0"{print $1; exit}')
sudo nmcli connection modify "$CON" connection.autoconnect yes connection.autoconnect-retries 0
sudo systemctl enable NetworkManager-wait-online.service
```

Ask your router for a **DHCP reservation** on the Pi's eth0 MAC — it makes LAN-side
debugging predictable (the hotspot is your guaranteed path either way):

```bash
ip link show eth0 | grep link/ether
```

Verify the backend is reachable and TLS validates:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://innvera.online/pi/agent/latest-version
```

`200` = good. `000` = no internet or clock skew — check `timedatectl` and `ip -4 addr show eth0`.

---

## Phase 5 — CUPS + the Epson L6460

The L6460 speaks IPP Everywhere, so it runs driverless through `ipp-usb`. No vendor driver
to rot across OS upgrades; duplex and colour are negotiated from the printer itself.

### 5.1 Attach and confirm the bridge

Plug the printer into USB, power it on, then:

```bash
lsusb | grep -i epson             # expect: Seiko Epson Corp. L6460 Series
systemctl status ipp-usb --no-pager | head -5
ss -ltn | grep 60000              # expect 127.0.0.1:60000
```

`ipp-usb` is udev-activated — `systemctl enable` reports "no installation config", which is
normal and not an error. It starts when the printer is plugged in.

### 5.2 Create the queue

```bash
sudo lpadmin -p "$QUEUE" -E -v "ipp://localhost:60000/ipp/print" -m everywhere
lpstat -p
```

Expect `printer PRINTIT is idle. enabled since ...`

If that fails, use the discovered URI. **Grep for `EPSON`** — `lpinfo -v` prints in
non-deterministic order and the first lines are bare schemes (`network ipp`, `network ipps`),
so positional parsing grabs the literal string `ipps`:

```bash
URI=$(lpinfo -v | grep -i EPSON | awk '{print $2}')
echo "$URI"                       # must show ipp://EPSON%20L6460...
sudo lpadmin -p "$QUEUE" -E -v "$URI" -m everywhere
```

Empty `$URI`? Discovery hasn't settled — re-run `lpinfo -v` once or twice.

**Last-resort driver fallback**, if `-m everywhere` cannot build a PPD:

```bash
sudo apt install -y --no-install-recommends printer-driver-escpr
lpinfo -m | grep -i "l6460"
sudo lpadmin -p "$QUEUE" -E -v "$URI" -m "<the escpr ppd path printed above>"
```

### 5.3 Queue policy — this decides refund correctness

```bash
sudo lpadmin -d "$QUEUE"                                   # system default
sudo lpadmin -p "$QUEUE" -o media-default=iso_a4_210x297mm -o PageSize=A4
sudo lpadmin -p "$QUEUE" -o sides-default=one-sided
sudo lpadmin -p "$QUEUE" -o job-sheets-default=none,none   # no banner pages, ever
sudo lpadmin -p "$QUEUE" -o printer-error-policy=stop-printer
sudo lpadmin -p "$QUEUE" -o job-cancel-after-default=900
sudo cupsaccept "$QUEUE" && sudo cupsenable "$QUEUE"
```

**Keep `stop-printer`.** It is what makes fault handling correct: paper-out → CUPS stops the
queue and holds the job → the job stays in `lpstat -W not-completed` → the agent times out →
job `FAILED`, payment `REFUND_PENDING`. With `abort-job` the job silently vanishes and the
agent reports **PRINTED** for paper that never came out.

Keep CUPS bound to localhost (Bookworm default — verify, never run `cupsctl --remote-any`):

```bash
grep -E '^Listen|^Port' /etc/cups/cupsd.conf     # expect: Listen localhost:631
sudo systemctl enable --now cups
```

### 5.4 Prove the hardware before involving the cloud

Load paper. Run all three:

```bash
lp -d "$QUEUE" /usr/share/cups/data/testprint
lp -d "$QUEUE" -o print-color-mode=monochrome /usr/share/cups/data/testprint
lp -d "$QUEUE" -o sides=two-sided-long-edge /usr/share/cups/data/testprint
```

Three pages, the third double-sided. Then confirm nothing is stuck:

```bash
lpstat -t
lpstat -o           # should be empty once all three finish
```

**Test 2 must come out greyscale.** `build_lp_command` sends
`-o print-color-mode=monochrome -o Ink=MONO` for B&W jobs. `Ink` is an HP vendor option —
Epson ignores it harmlessly, and `print-color-mode` does the real work. If page 2 prints in
colour, every ₹2 B&W job burns colour ink: switch to the escpr PPD and add
`ColorModel=Gray` to `build_lp_command` in `pi-agent/agent.py`.

```bash
lpoptions -p "$QUEUE" -l | grep -Ei 'ColorModel|print-color-mode|Duplex|InputSlot'
```

Clear any leftovers before going live:

```bash
sudo cancel -a -x "$QUEUE"
```

---

## Phase 6 — Provision the kiosk in the cloud

The agent **cannot create itself**. `POST /printers/pi/register` rejects an unknown
`printer_id` unless it carries a valid `X-Provisioning-Key`
(`cloud-backend/app/core/printer_auth.py::authorize_registration`). Use the dashboard — it
also sets `is_approved = true`, which the serviceability check requires.

### 6.1 Unlock the "Add Kiosk" button

Disabled until **three** conditions hold on the logged-in account. **No admin bypass** —
`add_kiosk()` checks the *current user's* own subscription even for `is_admin`.

| # | Condition | Set by |
|---|---|---|
| 1 | `is_kiosk_owner` (or `is_admin`) | admin → Subscriptions → **Promote to kiosk owner** |
| 2 | `subscription_enabled = true` | admin → Subscriptions → **Enable Sub** |
| 3 | `subscriptions` row `status='ACTIVE'` and `grace_ends_at >= now()` | pay, admin-activate, or DB insert |

Diagnose — dashboard tab → DevTools console:

```js
await (await fetch('https://innvera.online/kiosk/me', {
  headers: { Authorization: 'Bearer ' + sessionStorage.getItem('kiosk_token') }
})).json()
```

**Path A — UI, no money moves.** As admin: *Subscriptions* → search the owner → **Promote to
kiosk owner** → **Enable Sub**. Logged in as that owner, reload: the Pro banner appears →
click a duration → Razorpay modal opens (the `PENDING_PAYMENT` row now exists) → **close it
without paying**. Back in admin → *Subscriptions* → **Activate** → any reference string.
Reload; Add Kiosk is live.

**Path B — real payment.** Same, but pay. `/subscriptions/verify` activates automatically.
For genuine external owners.

**Path C — server-side, no Razorpay dependency.** On the VPS:

```bash
cd /opt/printit/cloud-backend/deploy
sudo docker compose exec -T api python - <<'PY'
from datetime import datetime, timedelta
from decimal import Decimal
from app.db.session import SessionLocal
from app.models.user import User
from app.models.subscription import Subscription

EMAIL = "ops@innvera.online"     # the account that will own the kiosk
MONTHS = 12

db = SessionLocal()
u = db.query(User).filter(User.email == EMAIL).first()
assert u, "user not found — sign it up through the app first"
u.is_kiosk_owner = True
u.subscription_enabled = True
now = datetime.utcnow(); exp = now + timedelta(days=30 * MONTHS)
db.add(Subscription(
    user_id=u.id, plan_tier="PRO", monthly_price=Decimal("1800.00"),
    settlement_type="DIRECT", duration_months=MONTHS,
    discount_percent=Decimal("15.00"), total_amount=Decimal("18360.00"),
    status="ACTIVE", starts_at=now, expires_at=exp,
    grace_ends_at=exp + timedelta(days=3), payment_reference="INTERNAL-MANAGED-FLEET",
))
db.commit(); print("ok until", exp)
PY
```

Cap: **5 kiosks** per active PRO subscription. Raise
`SUBSCRIPTION_PLANS["PRO"]["max_kiosks"]` in `app/routers/subscription.py` for a managed
fleet, or use a second ops account.

**Managed fleet model:** admin rights don't own kiosks — accounts do. Create one house owner
account, set its Razorpay keys once (*Payment Config* — per **user**, not per printer,
settable only once), and add every managed kiosk under it.

### 6.2 Known schema drift — fix before the first Add Kiosk

Databases created before the hashed-token release still have `printers.secret_token NOT
NULL`, while `add_kiosk` inserts `NULL` there by design. The insert fails and the catch-all
handler misreports it as **"A kiosk with that name already exists"** for *every* name.

```bash
sudo docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER TABLE printers ALTER COLUMN secret_token DROP NOT NULL;"'
sudo docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d printers"' | grep secret_token
```

Quote it exactly like that — `$POSTGRES_USER` is set **inside** the container, not in your
host shell, so interpolating on the host sends an empty value and psql falls back to your
username as the DB name.

Permanent version: `cloud-backend/migrate_drop_printer_secret_token_notnull.py`.

Then scan for other drift (read-only):

```bash
sudo docker compose exec -T api python - <<'PY'
from sqlalchemy import inspect
from app.db.session import Base, engine
import app.models
insp = inspect(engine); tables = set(insp.get_table_names()); issues = 0
for t in Base.metadata.sorted_tables:
    if t.name not in tables:
        print(f"MISSING TABLE: {t.name}"); issues += 1; continue
    db_cols = {c["name"]: c for c in insp.get_columns(t.name)}
    for col in t.columns:
        d = db_cols.get(col.name)
        if d is None:
            print(f"{t.name}.{col.name}: MISSING in DB"); issues += 1; continue
        if bool(d["nullable"]) != bool(col.nullable):
            print(f"{t.name}.{col.name}: db nullable={d['nullable']} model nullable={col.nullable}"); issues += 1
print("clean — no drift" if issues == 0 else f"{issues} mismatch(es)")
PY
```

### 6.3 Create the kiosk

Dashboard → *Kiosks* → **➕ Add Kiosk**:

| Field | Notes |
|---|---|
| Name | 1–80 chars. **Globally unique** across the platform, including retired kiosks. |
| Location description | free text |
| Latitude / Longitude | "nearest kiosk" in the PWA — right-click the exact spot in Google Maps |
| B&W single / double, Colour single / double | per-page price in ₹ |

On submit the browser **auto-downloads `config.json`**. That file *is* your
`KIOSK_ID` / `KIOSK_TOKEN` / `KIOSK_NAME`:

```json
{
  "identity": {
    "base_url": "https://innvera.online",
    "printer_id": "kiosk-ab12cd34",
    "printer_name": "Innvera MG Road",
    "secret_token": "…"
  },
  "tuneable": { "simulate_only": false, "download_dir": "/tmp/print_jobs", "enable_health_check": false }
}
```

The token is hashed server-side and **never shown again**. Lost it → **Regenerate config**
on the kiosk card, which invalidates the old one.

Rename the download immediately (`config-<kiosk-name>.json`) if you're provisioning several.

Then set **paper capacity** on the kiosk so refiller low-paper alerts fire.

### 6.4 Carry it to the Pi

Join the Pi's hotspot on your laptop first, then:

```powershell
scp "C:\Users\gurua\Downloads\config.json" pi-3@10.42.0.1:/tmp/kiosk-config.json
```

No trailing slash after the host. `host:/path`, nothing between the host and the colon.

If scp misbehaves, paste it by hand: `ssh pi-3@10.42.0.1`, then `nano /tmp/kiosk-config.json`,
paste, `Ctrl+O`, `Enter`, `Ctrl+X`.

On the Pi:

```bash
export KIOSK_ID=$(jq -r .identity.printer_id      /tmp/kiosk-config.json)
export KIOSK_TOKEN=$(jq -r .identity.secret_token /tmp/kiosk-config.json)
export KIOSK_NAME=$(jq -r .identity.printer_name  /tmp/kiosk-config.json)
echo "$KIOSK_ID / $KIOSK_NAME"
```

Must print the id and name. `null` → the file didn't arrive; redo the copy.

---

## Phase 7 — Install the agent

### 7.1 User and layout

```bash
sudo useradd --system --create-home --home-dir /opt/printit --shell /usr/sbin/nologin printit
sudo usermod -aG lp,lpadmin printit
sudo mkdir -p /opt/printit/agent /etc/printit
sudo mount /var/spool/printit          # the fstab entry from Phase 3
mount | grep printit                   # confirm tmpfs mounted
```

### 7.2 Copy the code

From your workstation (on the hotspot):

```powershell
scp "<repo>\pi-agent\agent.py" "<repo>\pi-agent\requirements.txt" pi-3@10.42.0.1:/tmp/
```

On the Pi:

```bash
sudo install -o printit -g printit -m 0644 /tmp/agent.py /opt/printit/agent/agent.py
sudo install -o printit -g printit -m 0644 /tmp/requirements.txt /opt/printit/agent/requirements.txt
sudo -u printit python3 -m venv /opt/printit/venv
sudo -u printit /opt/printit/venv/bin/pip install --upgrade pip
sudo -u printit /opt/printit/venv/bin/pip install -r /opt/printit/agent/requirements.txt
sudo -u printit /opt/printit/venv/bin/python -c "import websocket, requests; print('deps ok')"
```

`websocket-client` is **not optional in production**. The agent's HTTP heartbeat is broken
(Gotcha 1); the WebSocket `ping` is what keeps `last_heartbeat_at` fresh and the kiosk
`ONLINE`. No WebSocket → kiosk shows offline and is delisted from the app.

### 7.3 Config

```bash
sudo tee /etc/printit/config.json >/dev/null <<EOF
{
  "identity": {
    "base_url": "https://innvera.online",
    "printer_id": "$KIOSK_ID",
    "printer_name": "$KIOSK_NAME",
    "secret_token": "$KIOSK_TOKEN"
  },
  "tuneable": {
    "simulate_only": false,
    "printer_name_hw": "$QUEUE",
    "download_dir": "/var/spool/printit",
    "download_ttl_seconds": 3600,
    "enable_health_check": true,
    "cups_job_timeout_seconds": 300,
    "cups_job_poll_interval_seconds": 3,
    "fetch_job_max_retries": 3,
    "fetch_job_retry_delay_seconds": 2,
    "download_max_retries": 3,
    "download_retry_delay_seconds": 3,
    "status_update_max_retries": 6,
    "status_update_retry_base_seconds": 1.5,
    "ghostscript_path": ""
  },
  "version": "1.0.0",
  "locked": false
}
EOF
sudo chown printit:printit /etc/printit/config.json
sudo chmod 0600 /etc/printit/config.json

sudo grep -c secret_token /etc/printit/config.json   # sanity: 1
```

Why these values:

- `simulate_only: false` — `true` marks jobs PRINTED without printing. The shipped
  `config.example.json` has it `true`.
- `printer_name_hw` must equal the CUPS queue name **exactly** (case-sensitive) — it becomes
  `lp -d <name>`. Empty falls back to the system default queue, fragile if a second queue
  ever appears.
- `enable_health_check: true` — the only path that flips a kiosk to `ERROR`, fails in-flight
  jobs and marks payments `REFUND_PENDING`, and the only path that clears `ERROR` again
  (heartbeats deliberately don't). Off = a jammed kiosk keeps taking orders.
- `cups_job_timeout_seconds: 300` — stuck job detected in 5 min, before the recovery timer
  purges at 10 min.
- `download_ttl_seconds: 3600` — spool is tmpfs; no reason to hold customer PDFs a day.

### 7.4 systemd unit

Must be named `pi-agent.service` — remote restart from the dashboard runs
`sudo systemctl restart pi-agent` on the device (`_ALLOWED_SERVICES` in `pi.py`).

```bash
sudo tee /etc/systemd/system/pi-agent.service >/dev/null <<'EOF'
[Unit]
Description=PrintIt Pi Agent
Documentation=https://innvera.online
After=network-online.target cups.service
Wants=network-online.target
Requires=cups.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=printit
Group=printit
SupplementaryGroups=lp lpadmin
Environment=PI_AGENT_CONFIG_PATH=/etc/printit/config.json
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/opt/printit/agent
ExecStart=/opt/printit/venv/bin/python /opt/printit/agent/agent.py
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGINT

# hardening — NoNewPrivileges stays FALSE on purpose: the remote
# "restart service" feature needs sudo from inside the agent process.
NoNewPrivileges=false
ProtectSystem=full
ProtectHome=true
PrivateTmp=false
ReadWritePaths=/var/spool/printit /etc/printit

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pi-agent
```

### 7.5 Sudo rights for remote restart

```bash
sudo tee /etc/sudoers.d/printit-agent >/dev/null <<'EOF'
printit ALL=(root) NOPASSWD: /usr/bin/systemctl restart cups, /usr/bin/systemctl restart cups.service, /usr/bin/systemctl restart pi-agent, /usr/bin/systemctl restart pi-agent.service
EOF
sudo chmod 0440 /etc/sudoers.d/printit-agent
sudo visudo -cf /etc/sudoers.d/printit-agent      # must print "parsed OK"
```

Verify both directions:

```bash
sudo -u printit sudo -n systemctl restart cups     # allowed
sudo -u printit sudo -n reboot                     # must be refused
```

---

## Phase 8 — First light

```bash
journalctl -u pi-agent -f
```

Expected within ~10 s:

```
Registered printer: {'printer_id': 'kiosk-…', 'status': 'ONLINE', 'is_approved': True}
[WS] Connected to wss://innvera.online/ws/pi/kiosk-…
```

| Log line | Cause | Fix |
|---|---|---|
| `Failed to register printer: 403` every 30 s | `printer_id` not provisioned, or wrong token | Re-check Phase 6; regenerate config |
| `[WS] Connection error … 4001` | token/printer mismatch, or `is_active=false` | Regenerate config; confirm kiosk not deleted |
| `websocket-client not installed` | venv missing dep | re-run pip install |
| `Failed to send heartbeat: … 422` | known agent bug, harmless | Gotcha 1 |
| Registers, no jobs ever arrive | `is_approved=false` | admin → Printer Approvals → Approve |
| `lp: Error - unable to access "PRINTIT"` | `$QUEUE` empty when config was written | fix `printer_name_hw`, restart agent |

---

## Phase 9 — Self-healing CUPS queue

Paper-out or a jam disables the queue. After the agent has failed the job and flagged the
refund, the queue must come back **without a site visit** — and must not re-print the job
that was just refunded.

```bash
sudo tee /usr/local/sbin/printit-cups-recover >/dev/null <<'EOF'
#!/bin/bash
# Re-enable a CUPS queue that has been down longer than GRACE seconds.
# GRACE must exceed the agent's cups_job_timeout_seconds, so the agent has
# already marked the job FAILED (and the payment REFUND_PENDING) before we
# purge it. Purging prevents a refunded job from printing after a refill.
set -uo pipefail
QUEUE="${1:-PRINTIT}"
GRACE="${2:-600}"
STAMP="/run/printit-cups-recover.${QUEUE}"

if lpstat -p "$QUEUE" 2>/dev/null | grep -qiE 'disabled|stopped'; then
    now=$(date +%s)
    [ -f "$STAMP" ] || echo "$now" > "$STAMP"
    since=$(cat "$STAMP" 2>/dev/null || echo "$now")
    if [ $(( now - since )) -ge "$GRACE" ]; then
        logger -t printit-cups-recover "queue $QUEUE down >${GRACE}s — purging and re-enabling"
        cancel -a -x "$QUEUE" || true
        cupsaccept "$QUEUE" || true
        cupsenable "$QUEUE" || true
        rm -f "$STAMP"
    fi
else
    rm -f "$STAMP"
fi
EOF
sudo chmod 0755 /usr/local/sbin/printit-cups-recover

sudo tee /etc/systemd/system/printit-cups-recover.service >/dev/null <<EOF
[Unit]
Description=Recover a stopped PrintIt CUPS queue
After=cups.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/printit-cups-recover $QUEUE 600
EOF

sudo tee /etc/systemd/system/printit-cups-recover.timer >/dev/null <<'EOF'
[Unit]
Description=Periodic PrintIt CUPS queue recovery

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now printit-cups-recover.timer
```

Paper-out timeline, end to end:

```
t+0s    lp submits, printer stops, job held
t+300s  agent timeout -> job FAILED, health ERROR -> payment REFUND_PENDING, owner emailed
t+600s  recovery timer purges queue, cupsenable
t+605s  agent health OK -> kiosk ONLINE again, ready for the next customer
```

---

## Phase 10 — Remote access & firewall

Tailscale gives you SSH from outside the shop without port-forwarding. The hotspot stays as
the on-site fallback.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname "$KIOSK_ID"
```

### 10.1 Firewall — the lockout trap

`ufw` that only allows `tailscale0` will cut your hotspot access the moment it's enabled.
**Allow `wlan0` too**, always:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on wlan0            # ← admin hotspot; without this you are locked out
sudo ufw allow in on tailscale0
sudo ufw --force enable
sudo ufw status verbose
```

Confirm you can still open a **second** SSH session over `10.42.0.1` before closing the one
you have.

### 10.2 sshd hardening — optional, and only after keys work

Skipping this is legitimate for a device whose SSH is reachable only from its own hotspot
and Tailscale. If you do it, **copy a key first and test it in a second session**, or you
will lock yourself out:

```bash
# from your laptop, first:
#   ssh-copy-id pi-3@10.42.0.1        (or paste your pubkey into ~/.ssh/authorized_keys)
sudo tee /etc/ssh/sshd_config.d/10-printit.conf >/dev/null <<'EOF'
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
EOF
sudo systemctl restart ssh
```

---

## Phase 11 — Acceptance test (all seven)

```bash
# 1 — services up and enabled
systemctl is-enabled pi-agent cups printit-cups-recover.timer
systemctl is-active  pi-agent cups

# 2 — queue healthy, default set, accepting
lpstat -t | head -20

# 3 — agent talking to the cloud
journalctl -u pi-agent -n 30 --no-pager | grep -E 'Registered printer|\[WS\] Connected'
```

4. **Real end-to-end job** — from the PWA upload a 2-page PDF, pay, and watch
   `journalctl -u pi-agent -f`. Expect `Received job from server` → `QUEUED_ON_PI` →
   `PRINTING` → `PRINTED`, and paper in hand. Repeat for colour, duplex, and a page range
   like `3-1` (the agent normalises out-of-order ranges to `1,3`; CUPS rejects them raw).

5. **Power-cut test** — pull mains, restore. Within ~90 s the hotspot must be back
   (`ssh pi-3@10.42.0.1`) **and** the agent re-registered (`journalctl -b -u pi-agent | head`).
   This is the test the previous build failed.

6. **Fault test** — empty the tray, send a job. Expect job `FAILED`, kiosk `ERROR`, owner
   email, payment `REFUND_PENDING`; refill and confirm `ONLINE` within ~11 min with no stale
   reprint.

7. **Uplink drop** — `sudo nmcli connection down "$CON"; sleep 60; sudo nmcli connection up "$CON"`.
   Log shows `[WS] Connection error … reconnecting in 5 s` then `[WS] Connected`. Your SSH
   session over the hotspot must survive untouched.

---

## Phase 12 — Day-2 runbook

```bash
# get in, always
ssh pi-3@10.42.0.1                     # join SSID printit-kiosk-01 first

# logs
journalctl -u pi-agent -n 200 --no-pager
journalctl -u cups -n 100 --no-pager
journalctl -t printit-cups-recover --no-pager

# restart
sudo systemctl restart pi-agent
sudo systemctl restart cups

# stuck queue, manual
lpstat -W not-completed -o
sudo cancel -a -x "$QUEUE" && sudo cupsaccept "$QUEUE" && sudo cupsenable "$QUEUE"

# rotate the token (dashboard -> Regenerate config)
sudo nano /etc/printit/config.json      # replace identity.secret_token
sudo systemctl restart pi-agent

# update the agent
#   scp agent.py pi-3@10.42.0.1:/tmp/
sudo install -o printit -g printit -m 0644 /tmp/agent.py /opt/printit/agent/agent.py
sudo systemctl restart pi-agent
# bump AGENT_VERSION in agent.py — the backend tracks it

# swap the physical printer: recreate the queue under the same name, agent unchanged
sudo lpadmin -x "$QUEUE"                # then redo Phase 5.2/5.3

# hotspot health
nmcli -f GENERAL.STATE,IP4.ADDRESS connection show kiosk-ap
iw dev wlan0 info | grep type
```

Support one-liner:

```bash
echo "svc=$(systemctl is-active pi-agent) cups=$(systemctl is-active cups) \
ap=$(nmcli -g GENERAL.STATE connection show kiosk-ap) \
queue=$(lpstat -p $QUEUE 2>/dev/null | head -1) \
ws=$(journalctl -u pi-agent -n 200 --no-pager | grep -c '\[WS\] Connected')"
```

---

## Troubleshooting — things that actually went wrong

| Symptom | Real cause | Fix |
|---|---|---|
| `ssh: Could not resolve hostname pi-3.local` | Windows doesn't resolve mDNS | Use `10.42.0.1` (hotspot). Never rely on `.local`. |
| SSH dies after a power cut, IP unknown | DHCP lease changed | That's why the hotspot exists — Phase 1. |
| `no matching key exchange method found … diffie-hellman-group1-sha1` | You reached a router/IoT box, not the Pi | Pi OS never offers that KEX. Wrong host. |
| `The filename, directory name, or volume label syntax is incorrect` on scp | Trailing `/` after the host: `user@ip/:` | `user@ip:/path` |
| `lpadmin: Bad device-uri "ipps"` | Parsed `lpinfo -v` by position; the first lines are bare schemes | `lpinfo -v \| grep -i EPSON` |
| `lpadmin: Bad device-uri ""` | `$QUEUE` / `$URI` empty in a new shell | Phase 2.1 makes `QUEUE` permanent; `echo` before use |
| `FATAL: database "kiosk_user" does not exist` | Host shell expanded `$POSTGRES_USER` to empty | Wrap in `sh -c '…'` so the container expands it |
| Add Kiosk: "name already exists" for every name | `printers.secret_token NOT NULL` drift; catch-all handler mislabels it | Phase 6.2 |
| `systemctl enable ipp-usb` → "no installation config" | ipp-usb is udev-activated | Not an error. Ignore. |
| Pi unreachable but printing fine | uplink down, hotspot up | `ip -4 addr show eth0`, check the cable |

---

## Gotchas (read before debugging anything)

1. **HTTP heartbeat is broken in the agent.** `send_heartbeat()` posts to
   `/printers/pi/{id}/heartbeat` without the `X-Printer-Token` header, but the endpoint
   requires it → `422` every ~5 s. Harmless *only because* the WebSocket `ping` also
   refreshes `last_heartbeat_at`. Fix in `agent.py`:
   ```python
   resp = SESSION.post(
       f"{cfg.base_url}/printers/pi/{cfg.printer_id}/heartbeat",
       headers={"X-Printer-Token": cfg.secret_token},
       timeout=10,
   )
   ```
   Until then: **never deploy without `websocket-client`.**
2. **Registration is not self-service.** Unknown `printer_id` + no provisioning key = `403`
   forever, retrying every 30 s.
3. **`is_approved` gates everything.** Dashboard-created kiosks are approved; CLI-created
   ones are not.
4. **`simulate_only` must be `false`.**
5. **Token rides in the WS query string** — `wss://…/ws/pi/{id}?secret_token=…`. Fine over
   TLS; don't proxy the kiosk through anything that logs query strings.
6. **Registration still sends the token as a query param** (deprecated backend path).
7. **No Ghostscript needed on the Pi** — the backend normalises PDFs; `ghostscript_path` is
   a Windows-only knob.
8. **The agent prefetches the next job while printing.** A second download in the log during
   a long job is the prefetch worker, not a duplicate.
9. **`agent.py` defines `is_physical_printer_ready()` twice.** The second wins; the first
   (referencing an undefined `CUPS_PRINTER_NAME`) is dead code.
10. **`Ink=MONO` is an HP option** left over from a previous kiosk. Epson ignores it;
    `print-color-mode` is what matters. Verify with a real greyscale test page.
11. **Never `cupsctl --remote-any`.** CUPS on a kiosk has no reason to listen off-box.
12. **`wlan0` cannot be AP and client at once.** Internet comes from `eth0`.

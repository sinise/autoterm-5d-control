# Context for a future Claude session

This file exists so a fresh Claude instance (no memory of the original
session) can pick this project up without re-deriving everything from
scratch. Read this before touching the code or asking the user to run more
hardware tests -- most of what you'd want to check has already been checked.

## What this project is

Reverse-engineering the UART protocol between an **Autoterm 5D diesel
heater** and its **comfort control panel** on a boat, using a Raspberry Pi
with a multi-port USB-serial (FTDI-style) adapter wired inline, then
building a web dashboard to control the heater directly from the Pi. The
user is the boat owner, doing this on their own hardware for home/boat
automation -- legitimate personal reverse engineering, not adversarial.

Everything in `docs/PROTOCOL.md` is the accumulated, hard-won protocol
knowledge -- read it fully before doing any new protocol analysis. Don't
re-derive the CRC or frame format; they're solved and verified.

## Current state (be accurate about this -- don't oversell)

**Working and verified against real hardware:**
- Passive dual-port monitoring, framing, CRC verification (`autoterm_monitor.py`)
- Offline log analysis (`autoterm_analyze.py`)
- Inline transparent proxy, no injection (`autoterm_proxy.py`)
- Full web dashboard with live status + command injection (`autoterm_web.py`),
  running as a systemd service (`autoterm-web.service`), auto-start on boot,
  auto-restart on failure
- **Start** (preheat with duration, thermostat mode) and **Stop** commands,
  injected from the Pi, confirmed to produce real physical ignition/shutdown
  matching natural button-press behavior
- Software auto-thermostat: hysteresis loop (stop at target+1°C, start at
  target-1°C), built into `autoterm_web.py`, tested with a fake-serial
  harness for the tricky edge cases (no repeated stop-spam during the
  multi-minute cooldown window, no action on stale readings)

**Also working and verified against the design (not yet run on real
hardware as of this writing -- test it live before trusting it unattended):**
A Home Assistant Supervisor add-on at `homeassistant-addon/autoterm/`.
It's a full replacement for `autoterm_web.py` (owns the serial ports
directly, same relay + Commander + AutoThermostat logic, code reused not
re-derived) that speaks MQTT with Home Assistant MQTT discovery instead of
serving its own HTTP dashboard. See `homeassistant-addon/autoterm/DOCS.md`.
This supersedes the openHAB plan below for this project -- the openHAB
investigation notes are kept here for reference in case that path is
revisited, but Home Assistant is the live direction now.

**openHAB path (investigated, not implemented, likely superseded by the HA
add-on above unless the user says otherwise):**
openHAB integration via MQTT, with InfluxDB persistence. Investigated but
not implemented. Specifically:
- This machine (openHABian) has openHAB 5.2.1 running and InfluxDB 1.12.4
  (the 1.x line -- database/user/password, not the 2.x org/bucket/token
  style) running. Confirmed via `systemctl is-active`.
- Mosquitto (MQTT broker) is **not installed**. `python3-paho-mqtt` is
  **not installed**. Both are available via `apt` (confirmed:
  `apt-cache search python3-paho-mqtt` finds it; mosquitto is a standard
  Debian package).
- The openHAB MQTT binding and InfluxDB persistence addon are *available*
  (bundled in `/usr/share/openhab/addons/openhab-addons-5.2.1.kar`) but
  whether they're actually *enabled* in the running instance could not be
  confirmed -- the openHAB REST API has authentication enabled and no
  credentials were available in-session. This needs the user's own openHAB
  login; don't try to bypass it.
- Blocked mid-install: `sudo apt-get install -y mosquitto mosquitto-clients
  python3-paho-mqtt` was attempted but the passwordless sudo session had
  expired (it works early in a session, apparently riding on a cached sudo
  ticket, and stops working after enough wall-clock time passes -- don't
  assume sudo is passwordless just because it worked once earlier in a
  conversation). The user was asked to run that command themselves.
- **Design already decided, not yet coded:** MQTT over HTTP-polling binding,
  because it gives a properly bidirectional openHAB Thing. Planned topic
  scheme (not yet implemented):
  - State (published, retained): `autoterm/state`, `autoterm/fault`,
    `autoterm/cabin_temp`, `autoterm/coolant_temp`, `autoterm/elapsed`,
    `autoterm/burner`, `autoterm/auto/enabled`, `autoterm/auto/target`
  - Commands (subscribed): `autoterm/cmd/start_preheat` (payload =
    minutes), `autoterm/cmd/start_thermostat`, `autoterm/cmd/stop`,
    `autoterm/cmd/auto/enabled` (ON/OFF), `autoterm/cmd/auto/target`
  - These should call the *existing* `Commander` and `AutoThermostat`
    methods already in `autoterm_web.py` -- don't reimplement command
    logic, just wire MQTT in/out of what's there.
  - Mosquitto should listen on `127.0.0.1` only (both openHAB and the
    script run on the same Pi; no need to expose the broker to the LAN).
  - After the code side: install MQTT binding + InfluxDB persistence via
    openHAB's own UI (user does this, needs their login), add a Broker
    Thing, a Generic MQTT Thing with channels matching the topics above,
    link Items, then a `.persist` strategy file for InfluxDB.

## Hardware setup

- Raspberry Pi + multi-port USB-serial adapter, ports enumerate as
  `/dev/ttyUSB0`-`/dev/ttyUSB3` (not all necessarily present/stable across
  replugs -- see gotcha below)
- 2400 baud 8N1, confirmed via a baud sweep (`tools/baud_sweep.sh`)
- Currently wired **inline** (proxy mode): the direct panel<->heater wire
  was physically cut and the Pi sits in between. Passive-tap-only wiring
  (non-invasive, zero risk) is the earlier state, described for reference
  in the README but no longer the live configuration.
- **Current confirmed port assignment for injection: panel=`/dev/ttyUSB1`,
  heater=`/dev/ttyUSB3`.** This was wrong at least twice during the
  session (see gotchas) -- re-verify by content (which port reports a rich
  heater-shaped status frame vs. a bare cabin-temp reading) if anything is
  rewired again, rather than trusting these defaults blindly.

## Gotchas that cost real time -- don't repeat them

1. **Device roles were assumed backwards for most of a day.** dev03 is the
   panel, dev04 is the heater -- not the reverse. This was caught only
   because the "wrong" model required the heater to independently know
   cabin temperature and the panel to independently know the heater's
   internal coolant temperature, both physically implausible. If a new
   protocol model requires an oddly-located sensor to explain a reading,
   question the device attribution before the sensor physics.

2. **The USB-serial adapter re-enumerated during rewiring.** Port names
   (`/dev/ttyUSB1` vs `/dev/ttyUSB3`) shifted which physical connector they
   referred to after the adapter was unplugged/replugged. This looked
   identical to a wiring mistake. Always verify port identity by *content*
   (read a few seconds, check what's arriving) rather than assuming a
   devnode stays mapped to the same physical port.

3. **A `write()` succeeding proves nothing about the far end.** Early
   injection attempts logged "sent" with no exception, and it was tempting
   to treat that as proof of delivery. It isn't -- UART TX is fire-and-forget.
   The actual proof came from watching the heater's own decoded telemetry
   change afterward, not from the absence of a software error.

4. **The stop command was found by re-examining "known" transitions with
   fresh eyes.** Early on it was assumed heater stops were mostly automatic
   timeouts; the user corrected this (they were manual button presses), which
   prompted a byte-level re-examination of those exact moments and turned up
   a clean, reused-type-code signal (`type03`, single exchange) that had been
   missed the first time because the type code wasn't "new." When something
   should be discoverable but isn't showing up, check whether the meaningful
   signal is hiding inside an already-cataloged message type rather than a
   brand new one.

5. **Passwordless sudo is not durable.** It rode on a cached ticket from
   earlier in the session and stopped working after enough time passed.
   Don't assume it's still available; check, and ask the user to run
   sudo-requiring commands themselves if it's expired.

## Working with this user

- They verify things themselves and will correct you plainly and
  specifically when you're wrong (e.g. the device-role correction, the
  "these were manual stops, not timeouts" correction) -- take corrections
  at face value and re-derive from them rather than defending prior
  conclusions.
- They're comfortable with hands-on hardware work (rewiring, multimeter
  checks) and will do it when asked, but expect the ask to be concrete and
  specific (what to check, what result would mean what).
- They value evidence over confidence: prefer "checked the log, here's
  exactly what it shows" over "this should work." Several claims in this
  project turned out wrong ("timers are stopping the heater," a guessed
  stop encoding, an assumed device mapping); the pattern that worked was
  always going back to raw captured bytes rather than reasoning further
  from an unverified model.
- They're methodical and iterative -- comfortable running one clean test at
  a time (start, wait, stop, wait) rather than batching hardware
  experiments, and explicitly asked for that pacing at one point.
- This is real fuel-burning hardware on an occupied boat. Treat live
  command injection with the caution that implies: confirm the physical
  panel remains a working fallback before testing anything new, and don't
  guess at unconfirmed commands without flagging the uncertainty clearly.

## Useful commands

```bash
systemctl status autoterm-web            # is it running
journalctl -u autoterm-web -f            # live output
cat ~/autoterm_logs/web_port.txt         # which port the dashboard bound to
ls -t ~/autoterm_logs/web_*.log | head -1  # most recent session log
```

Analysis one-liners against a log file:
```bash
python3 autoterm/autoterm_analyze.py verify <logfile>      # CRC check
python3 autoterm/autoterm_analyze.py inventory <logfile>   # frame catalog
```

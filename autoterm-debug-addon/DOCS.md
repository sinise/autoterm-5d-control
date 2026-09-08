# Autoterm 5D Debug add-on

Everything the regular [Autoterm 5D add-on](../autoterm-addon/DOCS.md) does,
plus tools for continuing the protocol reverse-engineering: optional
extended-telemetry probing, a full raw-traffic capture log, and sensors for
every field decoded so far. This is **not** needed for normal day-to-day
heater control -- install the regular add-on for that. Install this one
instead of it when you want the extra data or are helping debug the
protocol further.

Full protocol derivation lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-5d-control) -- read it,
especially "Extended diagnostic-mode telemetry", before enabling debug mode.

## Before you install

Same prerequisites as the regular add-on: this owns both UART ports
directly, so **do not** run it alongside `autoterm_web.py`/`autoterm_proxy.py`
or the regular Autoterm 5D add-on on the same ports -- only one process can
hold a serial port open. Install **either** the regular add-on **or** this
one, not both at once (they'll fail to start if you try -- the second one
to start won't be able to open the ports).

This add-on is safe to install and run with debug mode and capture logging
both left **off** (the default) -- in that mode it behaves exactly like the
regular add-on. The extra risk described below only applies once you
actually turn debug mode on.

## Debug mode: extended telemetry probing

**What it does:** periodically sends the vendor diagnostic tool's "PUBR0"
handshake frame out the heater port. On a direct PC<->heater connection,
that handshake makes the heater start streaming a much richer 58-byte
telemetry frame once per second -- fan speed (defined vs. measured),
fuel pump frequency, flame/liquid/overheat/board temperature, supply
voltage, and a named operating mode/sub-mode (Low/Middle/High/Ignition
stages/etc), instead of just the basic 18-byte status. See
`docs/PROTOCOL.md` for exactly which fields and their formulas.

**Why it's off by default, and why you should watch the panel the first
time you turn it on:** this handshake has only ever been confirmed safe on
a *direct* PC<->heater connection with the physical panel disconnected.
Whether sending it while the panel is also present on the shared bus (as
it is once this add-on is installed inline) disrupts the panel's own
status polling or display is **untested**. The relay/passthrough itself is
never affected -- only the extra handshake frame is at risk of confusing
the panel's own parsing.

**How to test it safely:** turn on the **Debug mode** switch while
physically standing at the boat's comfort panel, watching its display.
Leave the **Debug probe interval** number at a sane value (60s default) so
you're not spamming the bus. Watch for 15-30 seconds:
- If the panel's display keeps updating normally (cabin temp, state, etc,
  same as always) -- probably safe to leave on.
- If the panel's display freezes, glitches, or stops responding to its own
  buttons -- turn **Debug mode** off immediately and report what you saw.

There's also a **Send debug handshake now** button, for sending exactly one
handshake on demand instead of waiting for the periodic timer -- useful for
a single closely-watched test.

The **Extended telemetry active** binary sensor tells you whether the
heater is actually replying with the richer frame (turns on once a valid
58-byte extended frame has arrived within the last 5 seconds) -- if debug
mode is on but this stays off, the handshake isn't getting a reply, which
is itself useful information.

## Raw traffic capture log

**What it does:** the **Capture raw traffic log** switch turns on a
plain-text log of every message seen -- every valid frame, every bad-CRC
frame, and every stray (unparsed) byte -- tagged with who sent it:

- `display` -- the physical comfort panel
- `heater` -- the heater
- `rpi` -- this add-on itself (injected commands, and the debug handshake
  if debug mode is on)

Each line has a timestamp, the sender, CRC status, decoded `dev`/`type`/
`len` where applicable, and the full frame in hex -- the same convention
`autoterm_monitor.py` in the main repo uses, so it's directly comparable to
other captures in this project.

**Where the log goes:** `/config/autoterm_debug/capture_<timestamp>.log` --
deliberately `/config`, not `/share`, so it shows up right where the
**File editor** add-on (and most other file-browser add-ons) already opens
by default, with no extra navigation or config changes needed. Reachable
from outside the add-on itself via:

- The **File editor** / **Studio Code Server** add-on -- it's right there
  in the default file tree, under `autoterm_debug/`.
- The **Samba share** add-on (if installed and configured to expose
  `config`) -- browse to `\\<home-assistant-ip>\config\autoterm_debug\`
  from your PC.
- SSH into the Home Assistant host, if you have that set up.

Toggling the switch off closes the current file cleanly (with an end
marker) -- toggling it back on starts a **new** file rather than appending,
so each capture session is its own file. A capture is capped at
`capture_log_max_mb` (default 20MB, configurable) -- past that it stops
writing (logged as a warning) rather than filling up storage; toggle it off
and on again to start a fresh file if you hit the cap mid-session.

The **Capture log file** and **Capture log size** sensors show the current
file name and size without needing to go find it first.

**If you send a capture back for further analysis:** the whole point of
this feature is to make that loop easy -- grab the file, note roughly what
you did and when (e.g. "turned on Debug mode at the start, pressed Start
preheat around the 2 minute mark"), and it can be diffed against the
already-decoded fields the same way the extended-frame work was done.

## Configuration

All the regular add-on's options, plus:

| Option | Meaning |
|---|---|
| `debug_mode_default` | Whether Debug mode starts on when the add-on (re)starts. Live-togglable from Home Assistant afterward -- this is just the boot default. |
| `debug_interval_seconds_default` | Initial value of the Debug probe interval number entity. |
| `capture_log_default` | Whether the capture log starts on when the add-on (re)starts. |
| `capture_log_max_mb` | Size cap per capture file, in MB. |

Debug mode, the probe interval, and the capture log toggle are all
live-controllable from Home Assistant (switches/number entities below) and
persisted to the add-on's `/data` volume -- the `_default` options above
only matter on a fresh install or if `/data` is cleared.

## What you get

Everything the regular add-on's device has, plus:

- **Switch**: Debug mode, Capture raw traffic log
- **Number**: Debug probe interval (seconds)
- **Button**: Send debug handshake now
- **Binary sensor**: Extended telemetry active, Glow plug
- **Sensors** (all extended-frame fields, populated only while Extended
  telemetry active is on): Mode of operation (named, e.g. "High", "middle",
  "glow plug warming up"), Running time (extended), Defined revolutions,
  Measured revolutions, Fuel pump frequency, Flame temperature, Liquid
  temperature (extended), Overheat sensor temperature, Board temperature,
  Supply voltage, Fault (extended, named), Engine state, Relay state, Fan
  current, Capture log file, Capture log size

Field formulas are the vendor's own (read from its plaintext `.pfl`
profile, not reverse-engineered from scratch) and cross-checked against a
real capture -- see `docs/PROTOCOL.md`. The fault-code name table is a
partial, lower-confidence addition -- only "no fault" (code 0) was actually
observed in the reference capture; the rest of the names come from the
vendor's own string table but haven't been confirmed against a real fault.

## Troubleshooting

Same as the regular add-on (see its DOCS.md) for MQTT/entity issues. In
addition:

- **Extended sensors stay blank**: Debug mode is probably off, or the
  handshake isn't getting a reply -- check the **Extended telemetry
  active** binary sensor and the add-on log for "DEBUG sent PUBR0
  handshake" lines.
- **Capture log switch is on but no file appears**: check the add-on log
  for a "capture log started" line and the exact path logged, and confirm
  you actually have a way to browse `/config` (File editor/Studio Code
  Server add-on installed, or SSH). It should appear as `autoterm_debug/`
  right in the default file tree -- no extra navigation needed.
